"""Milestone 3 — workshop lifecycle (C=2).

Fan-out + readiness for a 2-instance workshop, then the organizer credential delivery.
Cleanup is the lifecycle cron's job (test_lifecycle.test_workshop_cron_cleanup), NOT
comments on sub-issues — the issues-fixture teardown force-deletes the sub-issue
instances so this test never leaks.

Readiness (no OS creds needed): each sub-issue reaches `status:active` when its
create-instance-from-workflow setup finishes; then `update-workshop` (the real
credential-delivery cron) flips the parent to `workflow:instances-created` and emails
the organizer. `update-workshop` runs on the SAME single runner that builds the
instances, so we wait for the builds first and only gently nudge it afterwards.

Gated behind `provision`. See DESIGN.md §6.3.
"""

from __future__ import annotations

import pytest

from e2e import config, forms
from e2e.gh import poll, utcnow
from . import _lifecycle as lc

pytestmark = [pytest.mark.workshop, pytest.mark.provision]

WORKSHOP_TARGET = 2
BUILD_TIMEOUT = 3600    # 2 m3.tiny instances build serially on the single runner
READY_TIMEOUT = 900     # once built, update-workshop flips the parent quickly


def test_workshop_lifecycle(bot, admin, os_client, inbox_optional, ensure_test_labels, issues):
    flavor = config.assert_flavor_allowed(config.E2E_FLAVOR)

    # --- request opened (future start: valid + create-window already open) ----------
    parent = issues.open(
        "workshop", "workshop lifecycle",
        forms.workshop_body(flavor=flavor, duration_days=1, number_of_instances=WORKSHOP_TARGET),
    )
    admin.wait_for_label(parent, lambda lbl: lbl.startswith("start:"),
                         timeout=config.TIMEOUT_WORKFLOW_RUN)
    assert "needs-fix" not in admin.labels(parent)

    # --- /approve (admin) → approval (+ organizer approval email if inbox) ----------
    since = utcnow()
    lc.command(admin, admin, parent, "/approve", "approve-workshop.yml")
    assert "request:approved" in admin.labels(parent)
    if inbox_optional is not None:
        inbox_optional.wait_for(lambda m: "approv" in (m.subject + m.text).lower(), since=since)

    # --- /unapprove then re-/approve ------------------------------------------------
    lc.command(admin, admin, parent, "/unapprove", "approve-workshop.yml")
    assert "request:approved" not in admin.labels(parent)
    lc.command(admin, admin, parent, "/approve", "approve-workshop.yml")

    # --- /create (organizer) → N sub-issues -----------------------------------------
    bot.comment(parent, "/create")

    def enough_subs():
        subs = admin.sub_issues(parent)
        return subs if len(subs) >= WORKSHOP_TARGET else None

    sub_issues = poll(enough_subs, timeout=config.TIMEOUT_CREATE,
                      desc=f"{WORKSHOP_TARGET} workshop sub-issues")
    sub_nums = [s["number"] for s in sub_issues]
    for n in sub_nums:
        issues.opened.append(n)  # teardown force-cleans these (no leak)
        admin.add_labels(n, [config.E2E_LABEL])  # tag so the standalone sweeper sees them too

    # --- backfill the remainder -----------------------------------------------------
    admin.dispatch_workflow("workshop-backfill.yml")

    # --- wait for ALL instances to be built (status:active set at end of setup) ------
    since_ready = utcnow()
    for n in sub_nums:
        lc.assert_status_label(admin, n, "active", timeout=BUILD_TIMEOUT)

    # --- nudge update-workshop → parent flips to workflow:instances-created ----------
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
