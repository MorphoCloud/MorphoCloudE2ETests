"""Milestone 4 — lifecycle, expiry, renewal & cleanup (label-injection + dispatch).

Forces the time-gated pathways with short-expiry test labels (the cleanup analog of
m3.tiny) so nothing waits real time. The destructive members double as teardown.

Gated behind `provision`. See DESIGN.md §2 + §6.4.

Status: scaffold — the label-injection + dispatch + assertion structure is wired for
each pathway. Run only after M2/M3 are green and `e2e-verify-instance.yml` exists.
Each test provisions its own instance(s) via the helpers in _lifecycle.
"""

from __future__ import annotations

import pytest

from e2e import config, forms, openstack
from e2e.gh import poll, utcnow
from . import _lifecycle as lc

pytestmark = [pytest.mark.lifecycle, pytest.mark.provision]


def _provision_individual(bot, admin, os_client, issues) -> int:
    flavor = config.assert_flavor_allowed(config.E2E_FLAVOR)
    num = issues.open("individual", "lifecycle instance", forms.individual_body(flavor))
    lc.ensure_flavor_label(admin, num, flavor)
    lc.create(bot, admin, num)
    lc.assert_ready(admin, os_client, num)
    return num


def test_renew_protects_from_autodelete(bot, admin, os_client, inbox, ensure_test_labels, issues):
    """expiration:1d → warning email; /renew → renewed:1, pushed out, NOT deleted."""
    num = _provision_individual(bot, admin, os_client, issues)
    admin.ensure_label("expiration:2d", color="892368", description="expires in 2 days")
    admin.set_expiration_labels(num, ["expiration:1d", "expiration:2d"])

    since = utcnow()
    admin.dispatch_workflow("automatic-instance-deleting.yml")
    run = admin.wait_for_run("automatic-instance-deleting.yml", since=since,
                             timeout=config.TIMEOUT_COMMAND, event="workflow_dispatch")
    assert run and run.get("conclusion") == "success"
    # warning email + renewal-notice label, instance NOT deleted
    inbox.wait_for(lambda m: "expire" in (m.subject + m.text).lower(), since=since)
    assert any(lbl.startswith("renewal-notice:") for lbl in admin.labels(num))
    if os_client.available():
        assert os_client.server_exists(openstack.instance_name(num))

    # /renew pushes it out and clears the notice; re-run → still not deleted
    lc.command(bot, admin, num, "/renew", "update-renew-label.yml")
    assert any(lbl.startswith("renewed:") for lbl in admin.labels(num))
    since = utcnow()
    admin.dispatch_workflow("automatic-instance-deleting.yml")
    admin.wait_for_run("automatic-instance-deleting.yml", since=since,
                       timeout=config.TIMEOUT_COMMAND, event="workflow_dispatch")
    if os_client.available():
        assert os_client.server_exists(openstack.instance_name(num)), "renew must prevent deletion"


def test_auto_shelve(bot, admin, os_client, ensure_test_labels, issues):
    """timeout:0hrs → automatic-instance-shelving shelves the running instance."""
    num = _provision_individual(bot, admin, os_client, issues)
    admin.add_labels(num, [config.TEST_LABEL_SHELVE_NOW])
    since = utcnow()
    admin.dispatch_workflow("automatic-instance-shelving.yml")
    admin.wait_for_run("automatic-instance-shelving.yml", since=since,
                       timeout=config.TIMEOUT_COMMAND, event="workflow_dispatch")
    lc.assert_status_label(admin, num, "shelved")
    if os_client.available():
        poll(lambda: os_client.server_status(openstack.instance_name(num)) == "SHELVED_OFFLOADED",
             timeout=config.TIMEOUT_COMMAND, desc="SHELVED_OFFLOADED")
    # teardown for this instance happens via the sweeper / a follow-up /delete_all


def test_auto_delete(bot, admin, os_client, ensure_test_labels, issues):
    """expiration:0d → automatic-instance-deleting deletes instance + volume."""
    num = _provision_individual(bot, admin, os_client, issues)
    admin.set_expiration_labels(num, [config.TEST_LABEL_EXPIRE_NOW])
    since = utcnow()
    admin.dispatch_workflow("automatic-instance-deleting.yml")
    admin.wait_for_run("automatic-instance-deleting.yml", since=since,
                       timeout=config.TIMEOUT_COMMAND, event="workflow_dispatch")
    if os_client.available():
        poll(lambda: os_client.instance_gone(num) and os_client.volume_gone(num),
             timeout=config.TIMEOUT_COMMAND, desc="instance+volume deleted")


def test_auto_volume_delete(bot, admin, os_client, ensure_test_labels, issues):
    """volume:expiration-pending + graceperiod=0 → automatic-volume-deleting deletes it."""
    num = _provision_individual(bot, admin, os_client, issues)
    # detach the instance first so the volume is deletable, then mark it pending
    lc.command(bot, admin, num, "/delete_instance", "control-instance.yml")
    admin.ensure_label("volume:expiration-pending", color="0e8cdb")
    admin.add_labels(num, ["volume:expiration-pending"])
    since = utcnow()
    admin.dispatch_workflow("automatic-volume-deleting.yml",
                            inputs={"expiration_graceperiod_days": "0"})
    admin.wait_for_run("automatic-volume-deleting.yml", since=since,
                       timeout=config.TIMEOUT_COMMAND, event="workflow_dispatch")
    if os_client.available():
        poll(lambda: os_client.volume_gone(num), timeout=config.TIMEOUT_COMMAND,
             desc="volume deleted")


def test_workshop_cron_cleanup(admin, os_client, ensure_test_labels):
    """The real workshop teardown: inject expiration:0d on each sub-issue, dispatch
    automatic-instance-deleting once → it deletes all instances+volumes, closes all
    sub-issues, and closes the parent. We TRIGGER + assert; we never comment on subs.

    Precondition: a live workshop (run test_workshop_lifecycle first, or pass the parent
    via E2E_WORKSHOP_PARENT). Skipped if no live workshop is available.
    """
    import os
    parent_env = os.environ.get("E2E_WORKSHOP_PARENT")
    if not parent_env:
        pytest.skip("set E2E_WORKSHOP_PARENT to a live workshop parent issue number")
    parent = int(parent_env)

    subs = admin.sub_issues(parent)
    assert subs, f"workshop #{parent} has no sub-issues"
    for s in subs:
        admin.set_expiration_labels(s["number"], [config.TEST_LABEL_EXPIRE_NOW])

    since = utcnow()
    admin.dispatch_workflow("automatic-instance-deleting.yml")
    admin.wait_for_run("automatic-instance-deleting.yml", since=since,
                       timeout=config.TIMEOUT_CREATE, event="workflow_dispatch")

    # every sub-issue closed, every resource gone, parent closed
    for s in subs:
        n = s["number"]
        poll(lambda n=n: admin.issue_state(n) == "closed",
             timeout=config.TIMEOUT_CREATE, desc=f"sub-issue #{n} closed")
        if os_client.available():
            assert os_client.instance_gone(n) and os_client.volume_gone(n)
    poll(lambda: admin.issue_state(parent) == "closed",
         timeout=config.TIMEOUT_CREATE, desc=f"parent #{parent} closed")
