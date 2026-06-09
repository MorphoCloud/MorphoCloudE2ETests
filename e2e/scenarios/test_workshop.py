"""Milestone 3 — workshop lifecycle (C=2).

Fan-out + readiness for a 2-instance workshop, then the organizer credential delivery.
Cleanup is the lifecycle cron's job — the `workshop-lifecycle` suite
(test_lifecycle.test_workshop_lifecycle_cleanup), NOT comments on sub-issues. Here the
issues-fixture teardown force-deletes the sub-issue instances so this test never leaks.

Readiness (no OS creds needed): each sub-issue reaches `status:active` when its
create-instance-from-workflow setup finishes; then `update-workshop` (the real
credential-delivery cron) flips the parent to `workflow:instances-created` and emails
the organizer. `update-workshop` runs on the SAME single runner that builds the
instances, so we wait for the builds first and only gently nudge it afterwards.

Gated behind `provision`. See DESIGN.md §6.3.
"""

from __future__ import annotations

import pytest

from e2e import config
from e2e.gh import poll, utcnow
from . import _lifecycle as lc

pytestmark = [pytest.mark.workshop, pytest.mark.provision]

WORKSHOP_TARGET = 2
READY_TIMEOUT = 900     # once built, update-workshop flips the parent quickly


def test_workshop_lifecycle(bot, admin, os_client, inbox_optional, ensure_test_labels, issues):
    flavor = config.assert_flavor_allowed(config.E2E_FLAVOR)

    # --- request opened + /approve (+ organizer approval email if inbox) -------------
    since = utcnow()
    parent = lc.open_and_approve_workshop(bot, admin, issues, flavor=flavor, target=WORKSHOP_TARGET)
    if inbox_optional is not None:
        inbox_optional.wait_for(lambda m: "approv" in (m.subject + m.text).lower(), since=since)

    # --- /unapprove then re-/approve ------------------------------------------------
    lc.command(admin, admin, parent, "/unapprove", "approve-workshop.yml")
    assert "request:approved" not in admin.labels(parent)
    lc.command(admin, admin, parent, "/approve", "approve-workshop.yml")

    # --- /create (organizer) → N sub-issues → backfill → all built (status:active) ---
    sub_nums = lc.create_and_build_workshop(bot, admin, issues, parent, target=WORKSHOP_TARGET)

    # --- nudge update-workshop → parent flips to workflow:instances-created ----------
    since_ready = utcnow()
    # Instances are built, so this needs only a couple of dispatches; don't spam it (it
    # shares the runner). Each poll tick re-dispatches and then checks the label.
    def workshop_ready():
        if "workflow:instances-created" in admin.labels(parent):
            return True
        admin.dispatch_workflow("update-workshop.yml")
        return None

    poll(workshop_ready, timeout=READY_TIMEOUT, interval=120,
         desc="parent workflow:instances-created")

    # --- per-instance in-guest readiness probe (safe now: instances are built) -------
    for n in sub_nums:
        lc.assert_ready(admin, os_client, n)

    # --- organizer credential email (markdown table, one row per instance) -----------
    if inbox_optional is not None:
        msg = inbox_optional.wait_for(
            lambda m: "instances ready" in m.subject.lower()
            or "instances are active" in m.text.lower(),
            since=since_ready, timeout=config.TIMEOUT_CREATE)
        rows = [ln for ln in msg.text.splitlines()
                if ln.strip().startswith("|") and "---" not in ln and "Instance" not in ln]
        assert len(rows) == WORKSHOP_TARGET, \
            f"credential table has {len(rows)} rows, expected {WORKSHOP_TARGET}"
