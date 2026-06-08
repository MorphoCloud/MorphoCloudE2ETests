"""Leak guard + cleanup — strictly scoped to issues carrying the `e2e-test` label.

The sweeper NEVER deletes by raw OpenStack name pattern (that could hit manual staging
resources). It only acts on issues the harness itself tagged `[E2E]` / `e2e-test`, and
it cleans up the *real* way — by dispatching the production delete workflows
(`control-instance-from-workflow` + `delete-volume-from-workflow`) with the admin token,
then verifying via read-only OpenStack that the resources are gone.

Roles:
  * leak_guard()  — Phase B: abort the run if too many [E2E] issues already exist
                    (a previous run leaked; a human should look).
  * sweep()       — Phase E / `nox -s e2e-sweep`: force-clean [E2E] leftovers. Idempotent.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field

from . import config, openstack
from .gh import GitHubClient


class LeakGuardError(RuntimeError):
    pass


@dataclass
class SweepResult:
    deleted_instances: list[int] = field(default_factory=list)
    deleted_volumes: list[int] = field(default_factory=list)
    closed_issues: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)

    def actioned(self) -> bool:
        return bool(self.deleted_instances or self.deleted_volumes or self.closed_issues)

    def summary(self) -> str:
        return (
            f"instances_deleted={self.deleted_instances} "
            f"volumes_deleted={self.deleted_volumes} "
            f"issues_closed={self.closed_issues} skipped={self.skipped}"
        )


class Sweeper:
    def __init__(self, admin: GitHubClient, os_client: openstack.OpenStackClient | None = None):
        self.admin = admin
        self.os = os_client or openstack.OpenStackClient()

    # -- discovery ---------------------------------------------------------------------

    def _e2e_issues(self, state: str = "open") -> list[dict]:
        return self.admin.list_issues(labels=config.E2E_LABEL, state=state, limit=100)

    def count_pending(self) -> int:
        return len(self._e2e_issues(state="open"))

    # -- guard -------------------------------------------------------------------------

    def leak_guard(self) -> None:
        n = self.count_pending()
        if n > config.MAX_PENDING_E2E:
            raise LeakGuardError(
                f"{n} open [E2E] issues already exist (limit {config.MAX_PENDING_E2E}). "
                "A previous run likely leaked — inspect/clean before running again "
                "(`nox -s e2e-sweep`)."
            )

    # -- cleanup -----------------------------------------------------------------------

    def _has_resources(self, issue: int) -> bool:
        if not self.os.available():
            return True  # can't tell precisely; act best-effort
        return not (self.os.instance_gone(issue) and self.os.volume_gone(issue))

    def force_clean_issue(self, issue: int, result: SweepResult) -> None:
        """Dispatch the production delete workflows for one issue, then close it."""
        has_instance = (not self.os.available()) or not self.os.instance_gone(issue)
        has_volume = (not self.os.available()) or not self.os.volume_gone(issue)

        if has_instance:
            self.admin.dispatch_workflow(
                "control-instance-from-workflow.yml",
                inputs={
                    "issue_number": str(issue),
                    "command_name": "delete",
                    "unapprove_after_delete": "true",
                },
            )
            result.deleted_instances.append(issue)
        if has_volume:
            self.admin.dispatch_workflow(
                "delete-volume-from-workflow.yml",
                inputs={"issue_number": str(issue), "skip_approval_check": "true"},
            )
            result.deleted_volumes.append(issue)

    def verify_gone(self, issue: int, *, timeout: float = config.TIMEOUT_COMMAND) -> bool:
        if not self.os.available():
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.os.instance_gone(issue) and self.os.volume_gone(issue):
                return True
            time.sleep(config.POLL_INTERVAL)
        return False

    def sweep(self, *, min_age_hours: float = 0.0, close_issues: bool = True,
              verify: bool = True) -> SweepResult:
        """Force-clean every [E2E] leftover. Idempotent; safe to run any time.

        min_age_hours > 0 protects an in-flight run when invoked manually.
        """
        result = SweepResult()
        now = time.time()
        for issue in self._e2e_issues(state="open"):
            num = issue["number"]
            if min_age_hours > 0:
                created = _epoch(issue.get("created_at"))
                if created and (now - created) < min_age_hours * 3600:
                    result.skipped.append(num)
                    continue
            if not self._has_resources(num):
                if close_issues and issue["state"] == "open":
                    self.admin.close_issue(num, "Closed by E2E sweeper (no resources).")
                    result.closed_issues.append(num)
                continue
            self.force_clean_issue(num, result)

        if verify:
            for num in set(result.deleted_instances) | set(result.deleted_volumes):
                self.verify_gone(num)
        if close_issues:
            for issue in self._e2e_issues(state="open"):
                num = issue["number"]
                if num in result.skipped:
                    continue
                self.admin.close_issue(num, "Closed by E2E sweeper.")
                if num not in result.closed_issues:
                    result.closed_issues.append(num)
        return result


def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    from datetime import datetime
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").timestamp()
    except ValueError:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Force-clean [E2E] leftovers.")
    parser.add_argument("--min-age-hours", type=float, default=config.SWEEP_MIN_AGE_HOURS,
                        help="Only clean issues older than this (protects a live run).")
    parser.add_argument("--no-close", action="store_true", help="Delete resources but leave issues open.")
    args = parser.parse_args(argv)

    token = config.admin_token()
    if not token:
        print("ERROR: no admin token (set E2E_ADMIN_PAT or `gh auth login`).", file=sys.stderr)
        return 2
    admin = GitHubClient(token, label="admin")
    sweeper = Sweeper(admin)
    result = sweeper.sweep(min_age_hours=args.min_age_hours, close_issues=not args.no_close)
    print(f"Sweep complete: {result.summary()}")
    if not config.openstack_configured():
        print("NOTE: OpenStack not configured — cleanup dispatched best-effort, not verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
