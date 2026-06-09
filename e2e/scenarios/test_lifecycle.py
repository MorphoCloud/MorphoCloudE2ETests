"""Milestone 4 — time-gated lifecycle automation (label-injection + dispatch).

Forces the cleanup/renewal pathways with short-expiry test labels (the cleanup analog of
m3.tiny) so nothing waits real time. Two suites live here, mirroring the two documented
scenarios so they map 1:1 to the Actions `suite` dropdown:

  - `individual-lifecycle` (marker `individual_lifecycle`): the renewable per-instance
    automation — lifecycle management (renew ↔ auto-delete), auto-shelve, auto-volume-delete.
  - `workshop-lifecycle`   (marker `workshop_lifecycle`): the workshop teardown the cron
    does — cascade-delete every sub-instance + close the parent.

Every test provisions its own instance(s) via the helpers in _lifecycle, so each is
self-contained. Gated behind `provision`. See DESIGN.md §2 + §6.4.
"""

from __future__ import annotations

import pytest

from e2e import config, forms, openstack
from e2e.gh import poll, utcnow
from . import _lifecycle as lc

pytestmark = pytest.mark.provision


def _provision_individual(bot, admin, os_client, issues) -> int:
    flavor = config.assert_flavor_allowed(config.E2E_FLAVOR)
    num = issues.open("individual", "lifecycle instance", forms.individual_body(flavor))
    lc.ensure_flavor_label(admin, num, flavor)
    lc.create(bot, admin, num)
    lc.assert_ready(admin, os_client, num)
    return num


@pytest.mark.individual_lifecycle
def test_individual_lifecycle_management(bot, admin, os_client, ensure_test_labels, issues):
    """Full individual-instance lifecycle management, end to end, no real waiting.

    Mirrors production: an instance carries an expiration policy; the daily
    `automatic-instance-deleting` cron enforces it; `/renew` buys the next rung of the
    expiration ladder. Forced deterministically with short-expiry labels — the cron
    measures age from the issue's `created_at`, so `expiration:0d` ('created_at + 0' =
    already past) fires immediately, while a longer rung like `expiration:1d` stays in
    the future. (A literal 1-day expiry on a fresh issue would NOT trigger without a real
    ~1-day wait; that's why the active rung is collapsed to 0d below.)

    One instance, three acts:
      1. provision + reliable readiness.
      2. policy = [expiration:0d, expiration:1d]; `/renew` bumps `renewed:1` so the active
         rung becomes `expiration:1d` (future) → an auto-delete pass SPARES it. This is
         the "renew adds a day" guarantee.
      3. collapse the policy back to a single `expiration:0d` (clear `renewed:*`) → the
         next auto-delete pass DELETES the instance + volume, sets `status:deleted`, posts
         the expiration notice (the "report"), and closes the issue.

    NOTE: the renewal *warning* email/notice is NOT exercised — it only fires for an
    instance aged into the 7-day window of an expiration > 7d, which label injection on a
    fresh instance can't simulate (the cron derives age from created_at). See DESIGN.md §12.
    """
    num = _provision_individual(bot, admin, os_client, issues)
    name = openstack.instance_name(num)

    # --- Act 2: /renew protects (renew = climb to the next rung of the ladder) ---------
    admin.ensure_label("expiration:1d", color="892368", description="expires in 1 day")
    # renewed:0 selects expiration:0d (already past -> would delete); after /renew,
    # renewed:1 selects expiration:1d (created_at + 1d -> future -> survives).
    admin.set_expiration_labels(num, ["expiration:0d", "expiration:1d"])
    since_renew = utcnow()
    lc.command(bot, admin, num, "/renew", "update-renew-label.yml")
    assert any(lbl.startswith("renewed:") for lbl in admin.labels(num)), "renew did not set renewed:N"
    # Goal #2 (user-facing message): a successful /renew must CONFIRM to the user, not just
    # react. Two rungs here ([0d,1d]) → exactly one renewal allowed → the "no more renewals
    # left, final expiration <date>" wording. Assert the acknowledgement comment is posted.
    ack = admin.wait_for_comment(num, "Renewal applied", since=since_renew)
    assert "expiration" in ack["body"].lower(), \
        f"renew confirmation must state the new expiration date: {ack['body']!r}"

    since = utcnow()
    admin.dispatch_workflow("automatic-instance-deleting.yml")
    run = admin.wait_for_run("automatic-instance-deleting.yml", since=since,
                             timeout=config.TIMEOUT_COMMAND, event="workflow_dispatch")
    assert run and run.get("conclusion") == "success"
    # Deletion deletes the volume INLINE (sets volume:deleted) before the run concludes,
    # so if renew had failed to protect, #num would carry volume:deleted now. It must not.
    labels_after = admin.labels(num)
    assert "volume:deleted" not in labels_after and "status:deleted" not in labels_after, \
        "renew did not protect the instance from the auto-delete pass"
    if os_client.available():
        assert os_client.server_exists(name), "renew should have kept the instance alive"

    # --- Act 3: collapse the ladder to expiration:0d -> cron cleans up AND reports ------
    for lbl in admin.labels(num):
        if lbl.startswith("renewed:"):
            admin.remove_label(num, lbl)
    admin.set_expiration_labels(num, [config.TEST_LABEL_EXPIRE_NOW])  # single rung, already past

    since = utcnow()
    admin.dispatch_workflow("automatic-instance-deleting.yml")
    run = admin.wait_for_run("automatic-instance-deleting.yml", since=since,
                             timeout=config.TIMEOUT_COMMAND, event="workflow_dispatch")
    assert run and run.get("conclusion") == "success"
    # cleaned up: status:deleted label, and (with OS creds) instance + volume gone.
    poll(lambda: "status:deleted" in admin.labels(num), timeout=config.TIMEOUT_COMMAND,
         desc=f"status:deleted on #{num}")
    if os_client.available():
        poll(lambda: os_client.instance_gone(num) and os_client.volume_gone(num),
             timeout=config.TIMEOUT_COMMAND, desc="instance+volume deleted")
    # reported: the cron posts an expiration/deletion notice and closes the issue.
    admin.wait_for_comment(num, "deleted", since=since)
    poll(lambda: admin.issue_state(num) == "closed", timeout=config.TIMEOUT_COMMAND,
         desc=f"#{num} closed by the auto-delete cron")


@pytest.mark.individual_lifecycle
def test_auto_shelve(bot, admin, os_client, ensure_test_labels, issues):
    """timeout:0hrs → automatic-instance-shelving shelves the running instance."""
    num = _provision_individual(bot, admin, os_client, issues)
    admin.add_labels(num, [config.TEST_LABEL_SHELVE_NOW])
    since = utcnow()
    admin.dispatch_workflow("automatic-instance-shelving.yml")
    admin.wait_for_run("automatic-instance-shelving.yml", since=since,
                       timeout=config.TIMEOUT_COMMAND, event="workflow_dispatch")
    lc.assert_status_label(admin, num, "shelved", "shelved_offloaded")
    if os_client.available():
        poll(lambda: os_client.server_status(openstack.instance_name(num)) == "SHELVED_OFFLOADED",
             timeout=config.TIMEOUT_COMMAND, desc="SHELVED_OFFLOADED")
    # teardown for this instance happens via the sweeper / a follow-up /delete_all


@pytest.mark.individual_lifecycle
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
    run = admin.wait_for_run("automatic-volume-deleting.yml", since=since,
                             timeout=config.TIMEOUT_COMMAND, event="workflow_dispatch")
    assert run and run.get("conclusion") == "success"
    poll(lambda: "volume:deleted" in admin.labels(num), timeout=config.TIMEOUT_COMMAND,
         desc=f"volume:deleted on #{num}")
    if os_client.available():
        poll(lambda: os_client.volume_gone(num), timeout=config.TIMEOUT_COMMAND,
             desc="volume deleted")


@pytest.mark.workshop_lifecycle
def test_workshop_lifecycle_cleanup(bot, admin, os_client, ensure_test_labels, issues):
    """The workshop teardown the cron performs: inject expiration:0d on each sub-issue,
    dispatch automatic-instance-deleting once → it deletes every instance+volume, closes
    every sub-issue, and closes the parent. We TRIGGER + assert; we never comment on subs.

    Self-contained: stands up its own 2-instance workshop (the shared helpers), unless
    E2E_WORKSHOP_PARENT points at an existing live workshop parent — then it reuses that
    and skips the build (useful for chaining after the `workshop` suite).
    """
    import os
    parent_env = os.environ.get("E2E_WORKSHOP_PARENT")
    if parent_env:
        parent = int(parent_env)
        subs = admin.sub_issues(parent)
        assert subs, f"workshop #{parent} has no sub-issues"
        sub_nums = [s["number"] for s in subs]
    else:
        flavor = config.assert_flavor_allowed(config.E2E_FLAVOR)
        parent = lc.open_and_approve_workshop(bot, admin, issues, flavor=flavor, target=2)
        sub_nums = lc.create_and_build_workshop(bot, admin, issues, parent, target=2)

    for n in sub_nums:
        admin.set_expiration_labels(n, [config.TEST_LABEL_EXPIRE_NOW])

    since = utcnow()
    admin.dispatch_workflow("automatic-instance-deleting.yml")
    admin.wait_for_run("automatic-instance-deleting.yml", since=since,
                       timeout=config.TIMEOUT_CREATE, event="workflow_dispatch")

    # every sub-issue closed, every resource gone, parent closed
    for n in sub_nums:
        poll(lambda n=n: admin.issue_state(n) == "closed",
             timeout=config.TIMEOUT_CREATE, desc=f"sub-issue #{n} closed")
        if os_client.available():
            assert os_client.instance_gone(n) and os_client.volume_gone(n)
    poll(lambda: admin.issue_state(parent) == "closed",
         timeout=config.TIMEOUT_CREATE, desc=f"parent #{parent} closed")
