"""Shared helpers for the provisioning scenarios (M2-M4).

These encode the reliable-readiness check (DESIGN.md §3) and the common create/command
steps so the scenario files stay readable. All mutations go through real IssueOps
commands or workflow dispatches; OpenStack is only read.
"""

from __future__ import annotations

from datetime import datetime

from e2e import config, openstack
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


def create(bot: GitHubClient, admin: GitHubClient, issue: int) -> datetime:
    """Post /create and wait for create-instance.yml to conclude success. Returns the
    timestamp just before /create (for email polling)."""
    since = utcnow()
    bot.comment(issue, "/create")
    run = admin.wait_for_run("create-instance.yml", since=since,
                             timeout=config.TIMEOUT_CREATE, event="issue_comment")
    assert run is not None, "create-instance.yml never ran for /create"
    assert run.get("conclusion") == "success", (
        f"/create concluded {run.get('conclusion')!r} ({run.get('html_url')})"
    )
    return since


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
