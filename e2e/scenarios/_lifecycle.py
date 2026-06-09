"""Shared helpers for the provisioning scenarios (M2-M4).

These encode the reliable-readiness check (DESIGN.md §3) and the common create/command
steps so the scenario files stay readable. All mutations go through real IssueOps
commands or workflow dispatches; OpenStack is only read.
"""

from __future__ import annotations

from datetime import datetime

from e2e import config, forms, openstack
from e2e.gh import GitHubClient, poll, utcnow


def ensure_flavor_label(admin: GitHubClient, issue: int, flavor: str = config.E2E_FLAVOR) -> None:
    """Guarantee the issue carries exactly the test flavor label (request-labeler may have
    skipped it if the label didn't exist when the issue opened)."""
    label = f"flavor:{flavor}"
    admin.ensure_label(label, color="d93f0b", description="E2E tiny flavor")
    for existing in admin.labels(issue):
        if existing.startswith("flavor:") and existing != label:
            admin.remove_label(issue, existing)
    if label not in admin.labels(issue):
        admin.add_labels(issue, [label])


def create(bot: GitHubClient, admin: GitHubClient, issue: int,
           attempts: int = 3) -> datetime:
    """Post /create and wait for create-instance.yml to conclude success. Returns the
    timestamp just before the successful /create (for email polling).

    create-instance.yml only runs when the issue carries `request-type:instance` at the
    instant the comment event fires. The label is set at issue creation, but GitHub's
    issue_comment event payload occasionally lags (a stale label snapshot from a replica),
    so the run concludes 'skipped' instead of 'success'. That's a harness/event race, not
    a product failure — re-post /create when it happens. A genuine create-instance failure
    (any conclusion other than 'skipped') is surfaced immediately, never retried."""
    conclusion = url = None
    for attempt in range(1, attempts + 1):
        since = utcnow()
        bot.comment(issue, "/create")
        run = admin.wait_for_run("create-instance.yml", since=since,
                                 timeout=config.TIMEOUT_CREATE, event="issue_comment")
        assert run is not None, "create-instance.yml never ran for /create"
        conclusion, url = run.get("conclusion"), run.get("html_url")
        if conclusion == "success":
            return since
        if conclusion != "skipped":
            break  # a real create-instance failure — don't mask it behind retries
        # 'skipped' == the request-type:instance label race; wait_for_run already burned
        # time (so the replica has settled), then re-post /create.
    raise AssertionError(
        f"/create concluded {conclusion!r} after {attempt} attempt(s) ({url})"
    )


def assert_ready(admin: GitHubClient, os_client: openstack.OpenStackClient, issue: int) -> None:
    """The reliable readiness check — three independent layers, all must hold (§3)."""
    name = openstack.instance_name(issue)
    vol = openstack.volume_name(issue)

    if os_client.available():
        # Layer 1: authoritative system marker (NOT Nova ACTIVE).
        poll(lambda: os_client.exo_setup_status(name) == "complete",
             timeout=config.TIMEOUT_CREATE, desc=f"exoSetup=complete on {name}")
        # Layer 2: volume attached + in-use.
        assert os_client.volume_in_use(vol), f"{vol} is not in-use/attached"

    # Layer 3: independent in-guest probe on the runner (mount/home/Slicer).
    since = utcnow()
    admin.dispatch_workflow("e2e-verify-instance.yml", inputs={"issue_number": str(issue)})
    run = admin.wait_for_run("e2e-verify-instance.yml", since=since,
                             timeout=config.TIMEOUT_COMMAND, event="workflow_dispatch")
    assert run is not None, "e2e-verify-instance.yml never ran (is it committed to Test-Instances?)"
    assert run.get("conclusion") == "success", (
        f"in-guest readiness probe failed ({run.get('html_url')})"
    )


def command(bot: GitHubClient, admin: GitHubClient, issue: int, cmd: str,
            workflow: str) -> None:
    """Post an IssueOps command and assert its workflow concludes success."""
    since = utcnow()
    bot.comment(issue, cmd)
    run = admin.wait_for_run(workflow, since=since, timeout=config.TIMEOUT_COMMAND,
                             event="issue_comment")
    assert run is not None, f"{cmd} did not trigger {workflow}"
    assert run.get("conclusion") == "success", (
        f"{cmd} concluded {run.get('conclusion')!r} ({run.get('html_url')})"
    )


def assert_status_label(admin: GitHubClient, issue: int, *statuses: str,
                        timeout: float = config.TIMEOUT_COMMAND) -> None:
    """Wait until the issue carries any one of the given status:* labels.

    Accepts several because OpenStack distinguishes states the harness treats as
    equivalent — e.g. a shelved instance reports SHELVED then SHELVED_OFFLOADED, so
    callers pass ("shelved", "shelved_offloaded").
    """
    wanted = {f"status:{s}" for s in statuses}
    poll(lambda: bool(wanted & set(admin.labels(issue))),
         timeout=timeout, desc=f"status in {sorted(statuses)} on #{issue}")


# --------------------------------------------------------------------------------------
# Workshop provisioning — shared by the workshop build scenario (test_workshop.py) and
# the workshop teardown scenario (test_lifecycle.py) so there is ONE definition of how a
# workshop is stood up. Split in two so the build scenario can interleave its extra
# /unapprove + approval-email assertions between the two halves.
# --------------------------------------------------------------------------------------

def open_and_approve_workshop(bot: GitHubClient, admin: GitHubClient, issues, *,
                              flavor: str = config.E2E_FLAVOR, target: int = 2) -> int:
    """Open a valid workshop request (future start so the create-window is already open),
    wait for schedule validation, then admin `/approve`. Returns the parent issue number."""
    parent = issues.open(
        "workshop", "workshop lifecycle",
        forms.workshop_body(flavor=flavor, duration_days=1, number_of_instances=target),
    )
    admin.wait_for_label(parent, lambda lbl: lbl.startswith("start:"),
                         timeout=config.TIMEOUT_WORKFLOW_RUN)
    assert "needs-fix" not in admin.labels(parent)
    command(admin, admin, parent, "/approve", "approve-workshop.yml")
    assert "request:approved" in admin.labels(parent)
    return parent


def create_and_build_workshop(bot: GitHubClient, admin: GitHubClient, issues, parent: int,
                              *, target: int = 2, build_timeout: int = 3600) -> list[int]:
    """Organizer `/create` → N sub-issues fan out → backfill → every sub-issue reaches
    `status:active`. Sub-issues are registered with the issues fixture and tagged so
    teardown + the sweeper force-clean them (no leak). Returns the sub-issue numbers."""
    bot.comment(parent, "/create")

    def enough_subs():
        subs = admin.sub_issues(parent)
        return subs if len(subs) >= target else None

    sub_issues = poll(enough_subs, timeout=config.TIMEOUT_CREATE,
                      desc=f"{target} workshop sub-issues")
    sub_nums = [s["number"] for s in sub_issues]
    for n in sub_nums:
        issues.opened.append(n)              # teardown force-cleans these
        admin.add_labels(n, [config.E2E_LABEL])  # tag so the standalone sweeper sees them too

    admin.dispatch_workflow("workshop-backfill.yml")
    # 2 m3.tiny instances build serially on the single runner.
    for n in sub_nums:
        assert_status_label(admin, n, "active", timeout=build_timeout)
    return sub_nums
