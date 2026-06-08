"""Pytest fixtures + Stage 0 for the MorphoCloud E2E harness.

Identity fixtures hold exactly one token each (bot / admin / extra_user). Tests that
need a credential that isn't configured are skipped, so a partially-provisioned
environment still runs whatever it can.

Run ordering (DESIGN.md §4a) is enforced here: a session-autouse Stage 0 vendorizes
MWF → Test-Instances first (gating), then a leak guard aborts if prior runs leaked.
"""

from __future__ import annotations

import warnings

import pytest

from e2e import config, openstack
from e2e.gh import GitHubClient
from e2e.sweeper import Sweeper


# --------------------------------------------------------------------------------------
# Provisioning guard — tests marked `provision` create real (cheap) instances. They are
# skipped unless E2E_PROVISION=1, so the cheap suite can never burn credits and nothing
# provisions before Milestone 0 has chosen a working E2E_FLAVOR.
# --------------------------------------------------------------------------------------

def pytest_collection_modifyitems(config, items):  # `config` = pytest Config (hook arg name)
    from e2e import config as e2ecfg
    if e2ecfg.PROVISION_ENABLED:
        return
    skip = pytest.mark.skip(
        reason="provisioning test — set E2E_PROVISION=1 (and E2E_FLAVOR from Milestone 0)"
    )
    for item in items:
        if "provision" in item.keywords:
            item.add_marker(skip)


# --------------------------------------------------------------------------------------
# Identity clients.
#
# Stage 0 (vendorize) and the leak guard are autouse fixtures scoped to the integration
# scenarios — they live in e2e/scenarios/conftest.py so that offline unit tests under
# tests/ don't trigger a vendorize/push or require an admin token.
# --------------------------------------------------------------------------------------

@pytest.fixture(scope="session")
def bot() -> GitHubClient:
    if not config.BOT_PAT:
        pytest.skip("E2E_BOT_PAT not set (see e2e/docs/setup_bot.md)")
    return GitHubClient(config.BOT_PAT, label="bot")


@pytest.fixture(scope="session")
def admin() -> GitHubClient:
    token = config.admin_token()
    if not token:
        pytest.skip("No admin token (set E2E_ADMIN_PAT or `gh auth login`)")
    return GitHubClient(token, label="admin")


@pytest.fixture(scope="session")
def extra_user() -> GitHubClient:
    if not config.EXTRA_USER_PAT:
        pytest.skip("E2E_EXTRA_USER_PAT not set — non-member negative is opt-in")
    return GitHubClient(config.EXTRA_USER_PAT, label="extra_user")


@pytest.fixture(scope="session")
def os_client() -> openstack.OpenStackClient:
    return openstack.OpenStackClient()


@pytest.fixture()
def inbox():
    if not config.imap_configured():
        pytest.skip("IMAP not configured (E2E_IMAP_*)")
    from e2e.mailbox import Mailbox
    with Mailbox() as mb:
        yield mb


# --------------------------------------------------------------------------------------
# Test-only labels — created on demand (idempotent), AFTER Stage 0 / any labels.yml sync.
# Tests that need m3.tiny / expiration:0d / timeout:0hrs depend on this explicitly.
# --------------------------------------------------------------------------------------

@pytest.fixture()
def ensure_test_labels(admin) -> None:
    admin.ensure_label(config.E2E_LABEL, color="5319e7", description="MorphoCloud E2E test issue")
    admin.ensure_label(config.TEST_LABEL_FLAVOR, color="d93f0b", description="E2E tiny flavor")
    admin.ensure_label(config.TEST_LABEL_EXPIRE_NOW, color="892368", description="E2E: already expired")
    admin.ensure_label(config.TEST_LABEL_SHELVE_NOW, color="5319e7", description="E2E: shelve immediately")


# --------------------------------------------------------------------------------------
# Issue factory — opens [E2E]-tagged issues and closes them at test end.
#
# For CHEAP tests this is sufficient teardown (no OpenStack resources). For provisioning
# tests, cleanup is the Phase D pathways + the session sweeper; the factory still closes
# the issue so the sweeper/leak-guard counts stay clean.
# --------------------------------------------------------------------------------------

class IssueFactory:
    def __init__(self, client: GitHubClient, admin: GitHubClient):
        self.client = client
        self.admin = admin
        self.opened: list[int] = []

    def open(self, kind: str, title: str, body: str) -> int:
        if kind == "individual":
            labels = (config.E2E_LABEL, *config.INDIVIDUAL_REQUEST_LABELS)
        elif kind == "workshop":
            labels = (config.E2E_LABEL, *config.WORKSHOP_REQUEST_LABELS)
        else:
            labels = (config.E2E_LABEL,)
        full_title = f"{config.TITLE_PREFIX} {title}"
        issue = self.client.open_issue(full_title, body, labels=labels)
        self.opened.append(issue.number)
        return issue.number

    def close_all(self) -> None:
        for num in self.opened:
            try:
                if self.admin.issue_state(num) == "open":
                    self.admin.close_issue(num, "Closed by E2E harness (test teardown).")
            except Exception as exc:  # best-effort teardown
                warnings.warn(f"failed to close E2E issue #{num}: {exc}", stacklevel=2)


@pytest.fixture()
def issues(bot, admin):
    factory = IssueFactory(bot, admin)
    yield factory
    factory.close_all()


@pytest.fixture()
def sweeper(admin, os_client) -> Sweeper:
    return Sweeper(admin, os_client)
