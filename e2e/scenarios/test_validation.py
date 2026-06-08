"""Milestone 1 — cheap validation suite (C=0, no provisioning).

These tests open issues and post comments / dispatch workflows, but never create an
OpenStack instance. They're fast, free, and catch a large class of validation/gating
regressions. They prove the harness plumbing (open issue → comment → poll → assert →
teardown) before any provisioning test.

Covers: validate-command-instance, validate-command-workshop, on-admin-mention,
labels.yml, and the request-open workshop schedule validation
(on-instance-request-opened) — see DESIGN.md §6.1.
"""

from __future__ import annotations

import pytest

from e2e import config, forms
from e2e.gh import GitHubClient, poll, utcnow

pytestmark = pytest.mark.cheap


def _assert_run(admin: GitHubClient, filename: str, *, since, expected: str,
                event: str | None = None, timeout: float = config.TIMEOUT_WORKFLOW_RUN):
    run = admin.wait_for_run(filename, since=since, timeout=timeout, event=event)
    assert run is not None, f"{filename} did not run (no run created since {since.isoformat()})"
    assert run.get("status") == "completed", f"{filename} did not complete: {run.get('status')}"
    assert run.get("conclusion") == expected, (
        f"{filename} concluded {run.get('conclusion')!r}, expected {expected!r} "
        f"({run.get('html_url')})"
    )
    return run


# --------------------------------------------------------------------------------------
# Command validators — unrecognized commands are rejected.
# --------------------------------------------------------------------------------------

def test_unknown_instance_command(bot, admin, issues):
    """`/frobnicate` on an instance issue → validate-command-instance fails + comment."""
    num = issues.open("individual", "unknown instance command",
                      forms.individual_body())
    since = utcnow()
    bot.comment(num, "/frobnicate")
    comment = bot.wait_for_comment(num, "Unrecognized Commands", since=since)
    assert "not recognized" in comment["body"]
    _assert_run(admin, "validate-command-instance.yml", since=since,
                expected="failure", event="issue_comment")


def test_unknown_workshop_command(bot, admin, issues):
    """`/delete_all` on a workshop issue → validate-command-workshop rejects it.

    Doubles as confirming /delete_all is NOT a valid workshop command (cleanup there is
    the lifecycle cron's job, not a comment).
    """
    num = issues.open("workshop", "unknown workshop command", forms.workshop_body())
    since = utcnow()
    bot.comment(num, "/delete_all")
    comment = bot.wait_for_comment(num, "Unrecognized Commands", since=since)
    assert "not recognized" in comment["body"]
    _assert_run(admin, "validate-command-workshop.yml", since=since,
                expected="failure", event="issue_comment")


# --------------------------------------------------------------------------------------
# on-admin-mention — a non-bot mention of the admins team emails the admins.
# --------------------------------------------------------------------------------------

def test_admin_mention(bot, admin, issues):
    """Bot (a real user) mentions @MorphoCloud/morphocloud-admins → on-admin-mention runs."""
    num = issues.open("other", "admin mention", "E2E admin-mention test.")
    since = utcnow()
    bot.comment(num, "Pinging @MorphoCloud/morphocloud-admins for an E2E mention test.")
    _assert_run(admin, "on-admin-mention.yml", since=since,
                expected="success", event="issue_comment")


# --------------------------------------------------------------------------------------
# labels.yml — workflow_dispatch syncs the repo's label definitions.
# --------------------------------------------------------------------------------------

def test_labels_sync(admin):
    """Dispatch labels.yml and confirm a sample of definitions exist on the repo."""
    since = utcnow()
    admin.dispatch_workflow("labels.yml")
    _assert_run(admin, "labels.yml", since=since, expected="success",
                event="workflow_dispatch")
    for label in ("flavor:g3.large", "expiration:60d", "status:active", "timeout:4hrs"):
        assert admin.repo_label_exists(label), f"labels.yml did not sync {label!r}"


# --------------------------------------------------------------------------------------
# Workshop request-time schedule validation (on-instance-request-opened, workshop branch).
# Invalid requests get `needs-fix` and the issue is closed — no instance involved.
# --------------------------------------------------------------------------------------

def _assert_needs_fix_and_closed(client: GitHubClient, num: int):
    def closed_with_needsfix():
        labels = client.labels(num)
        state = client.issue_state(num)
        return ("needs-fix" in labels) and state == "closed"
    poll(closed_with_needsfix, timeout=config.TIMEOUT_WORKFLOW_RUN,
         desc=f"#{num} to be needs-fix + closed")


def test_workshop_duration_too_long(bot, issues):
    """Duration > 5 days → rejected at request open (needs-fix + closed)."""
    num = issues.open("workshop", "duration too long",
                      forms.workshop_body(duration_days=9))
    _assert_needs_fix_and_closed(bot, num)


def test_workshop_malformed_date(bot, issues):
    """Unparseable start date → resolve-workshop-schedule rejects (needs-fix + closed)."""
    num = issues.open("workshop", "malformed date",
                      forms.workshop_body(start_date="not-a-date"))
    _assert_needs_fix_and_closed(bot, num)


# --------------------------------------------------------------------------------------
# Non-member /create — opt-in (needs a 2nd account NOT in MorphoCloudUsers).
# --------------------------------------------------------------------------------------

def test_non_member_create_rejected(extra_user, admin):
    """A non-member's /create is refused with the onboarding message."""
    body = forms.individual_body()
    issue = extra_user.open_issue(
        f"{config.TITLE_PREFIX} non-member create",
        body, labels=(config.E2E_LABEL, *config.INDIVIDUAL_REQUEST_LABELS),
    )
    try:
        since = utcnow()
        extra_user.comment(issue.number, "/create")
        extra_user.wait_for_comment(issue.number, "not a registered MorphoCloud user",
                                    since=since)
    finally:
        if admin.issue_state(issue.number) == "open":
            admin.close_issue(issue.number, "Closed by E2E harness (test teardown).")
