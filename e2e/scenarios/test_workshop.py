"""Milestone 3 — workshop lifecycle (C=2).

Provision + readiness for a 2-instance workshop, then verify the organizer credential
delivery. Cleanup (Phase D) is the lifecycle cron's job and lives in test_lifecycle.py
(`automatic-instance-deleting`), NOT comments on sub-issues.

Gated behind `provision`. See DESIGN.md §6.3.

Status: scaffold — the create/approve/backfill steps are wired; per-instance readiness
and the credential-CSV assertion are marked TODO where they depend on
`e2e-verify-instance.yml` being present on Test-Instances and on a baseline-confirmed
email shape.
"""

from __future__ import annotations

import pytest

from e2e import config, forms
from e2e.gh import poll, utcnow
from . import _lifecycle as lc

pytestmark = [pytest.mark.workshop, pytest.mark.provision]

WORKSHOP_TARGET = 2


def test_workshop_lifecycle(bot, admin, os_client, inbox, ensure_test_labels, issues):
    flavor = config.assert_flavor_allowed(config.E2E_FLAVOR)

    # --- request opened (start = today so start-12h is in the past) -----------------
    parent = issues.open(
        "workshop", "workshop lifecycle",
        forms.workshop_body(flavor=flavor, duration_days=1, number_of_instances=WORKSHOP_TARGET),
    )
    # schedule validation stamps start:<epoch> + workshop-target:N, no needs-fix
    admin.wait_for_label(parent, lambda lbl: lbl.startswith("start:"),
                         timeout=config.TIMEOUT_WORKFLOW_RUN)
    assert "needs-fix" not in admin.labels(parent)

    # --- /approve (admin) → approval + organizer approval email ---------------------
    since = utcnow()
    lc.command(admin, admin, parent, "/approve", "approve-workshop.yml")
    assert "request:approved" in admin.labels(parent)
    inbox.wait_for(lambda m: "approv" in m.subject.lower() or "approv" in m.text.lower(),
                   since=since)

    # --- /unapprove then re-/approve ------------------------------------------------
    lc.command(admin, admin, parent, "/unapprove", "approve-workshop.yml")
    assert "request:approved" not in admin.labels(parent)
    lc.command(admin, admin, parent, "/approve", "approve-workshop.yml")

    # --- /create (organizer) → N sub-issues + first BATCH builds --------------------
    bot.comment(parent, "/create")
    sub_issues = poll(
        lambda: admin.sub_issues(parent) if len(admin.sub_issues(parent)) >= WORKSHOP_TARGET else None,
        timeout=config.TIMEOUT_CREATE, desc=f"{WORKSHOP_TARGET} workshop sub-issues",
    )
    sub_nums = [s["number"] for s in sub_issues]
    for n in sub_nums:
        issues.opened.append(n)  # ensure teardown/sweeper tracks them

    # --- backfill the remainder -----------------------------------------------------
    admin.dispatch_workflow("workshop-backfill.yml")

    # --- per-instance readiness -----------------------------------------------------
    for n in sub_nums:
        lc.assert_ready(admin, os_client, n)

    # --- update-workshop → readiness labels + organizer credential CSV --------------
    since = utcnow()
    admin.dispatch_workflow("update-workshop.yml")
    run = admin.wait_for_run("update-workshop.yml", since=since,
                             timeout=config.TIMEOUT_CREATE, event="workflow_dispatch")
    assert run and run.get("conclusion") == "success"
    msg = inbox.wait_for(lambda m: "csv" in (m.subject + m.text).lower() or m.attachments,
                         since=since, timeout=config.TIMEOUT_CREATE)
    assert len(msg.credential_csv_rows()) == WORKSHOP_TARGET, "organizer CSV row count != target"

    # Cleanup is exercised in test_lifecycle.test_workshop_cron_cleanup (the real path).
