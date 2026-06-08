"""Central configuration for the E2E harness — constants + environment.

All secrets and per-environment values come from the environment (or a local `.env`
loaded here). Nothing secret is committed. See e2e/docs/setup_bot.md for what each
value is and how to provision it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

# --------------------------------------------------------------------------------------
# .env loading (local dev convenience; CI uses real env / Actions secrets)
# --------------------------------------------------------------------------------------

def _load_dotenv() -> None:
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip an inline `# comment` from UNQUOTED values (secrets are file-based, so
        # .env only ever holds non-secret config — safe to treat ' #' as a comment).
        if value and value[0] not in ("'", '"'):
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        value = value.strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv()


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    return val if val not in (None, "") else default


def _secret(name: str) -> str | None:
    """A secret from env `NAME`, or read from the file named by `NAME_FILE`.

    The `_FILE` form keeps real secrets out of `.env` / process env — `.env` just holds
    a path (e.g. E2E_BOT_PAT_FILE=~/.ssh/GH-morphocloud-e2e-token).
    """
    val = _env(name)
    if val:
        return val
    path = _env(name + "_FILE")
    if path:
        p = Path(path).expanduser()
        if p.exists():
            return p.read_text().strip()
    return None


# --------------------------------------------------------------------------------------
# Target repo / org — the harness ONLY ever touches Test-Instances.
# --------------------------------------------------------------------------------------

ORG = "MorphoCloud"
REPO = _env("E2E_REPO", "MorphoCloud/Test-Instances")
WORKFLOW_REPO = "MorphoCloud/MorphoCloudWorkflow"

# Repo variable on Test-Instances (confirmed via `gh variable list`).
INSTANCE_NAME_PREFIX = _env("E2E_INSTANCE_NAME_PREFIX", "morpho-cloud-test")

ADMINS_TEAM_SLUG = "morphocloud-admins"
USERS_TEAM_SLUG = "MorphoCloudUsers"
WORKSHOP_ORGANIZERS_TEAM_SLUG = "MorphoCloudWorkshopOrganizers"

# --------------------------------------------------------------------------------------
# Flavor — cheap by construction; a denylist makes a fat flavor impossible.
# E2E_FLAVOR is set by Milestone 0 (smallest flavor whose cloud-init completes).
# --------------------------------------------------------------------------------------

E2E_FLAVOR = _env("E2E_FLAVOR", "m3.tiny")

# Any flavor matching one of these is refused — the harness must never burn GPU/large
# instances. Matched against the bare flavor name (case-insensitive).
FLAVOR_DENYLIST = (
    re.compile(r"^g\d", re.I),       # g3.*, g4.*  — GPU
    re.compile(r"^r3\.", re.I),      # r3.large, r3.xl — large memory
    re.compile(r"^m3\.xl", re.I),    # m3.xl — big general purpose
)


class UnsafeFlavorError(RuntimeError):
    pass


def assert_flavor_allowed(flavor: str) -> str:
    """Refuse any flavor on the denylist. Returns the flavor if it is allowed."""
    for pat in FLAVOR_DENYLIST:
        if pat.search(flavor):
            raise UnsafeFlavorError(
                f"Refusing flavor {flavor!r}: matches the E2E denylist. "
                "The harness only runs tiny/cheap flavors."
            )
    return flavor


# --------------------------------------------------------------------------------------
# Tagging — every harness issue is unmistakable, so humans and the sweeper can tell
# E2E churn from manual staging.
# --------------------------------------------------------------------------------------

TITLE_PREFIX = "[E2E]"
E2E_LABEL = "e2e-test"

# Test-only labels created by the harness AFTER Stage 0 (so a labels-sync can't prune
# them mid-run). These do not live in MWF labels.yml, so they never reach production.
TEST_LABEL_FLAVOR = f"flavor:{E2E_FLAVOR}"
TEST_LABEL_EXPIRE_NOW = "expiration:0d"     # created_at + 0 -> already past -> auto-delete
TEST_LABEL_SHELVE_NOW = "timeout:0hrs"      # uptime > 0 -> auto-shelve
TEST_ONLY_LABELS = (E2E_LABEL, TEST_LABEL_FLAVOR, TEST_LABEL_EXPIRE_NOW, TEST_LABEL_SHELVE_NOW)

# Issue-form default labels the GitHub template applies; we add them by hand because we
# create issues via the API (not through the web template).
INDIVIDUAL_REQUEST_LABELS = ("request-type:instance", "request-creator:user", "instance-request")
WORKSHOP_REQUEST_LABELS = ("request-type:workshop", "request-creator:user")

# --------------------------------------------------------------------------------------
# Credentials (from env / Actions secrets). None -> the relevant tests skip.
# --------------------------------------------------------------------------------------

BOT_PAT = _secret("E2E_BOT_PAT")              # bot user PAT (NOT a GitHub App token)
ADMIN_PAT = _secret("E2E_ADMIN_PAT")          # admin (morphocloud-admins) PAT; falls back to gh
EXTRA_USER_PAT = _secret("E2E_EXTRA_USER_PAT")  # optional 2nd account, NOT in MorphoCloudUsers

# OpenStack (read-only) — selects the clouds.yaml entry; unset -> OS assertions skip.
OS_CLOUD = _env("E2E_OS_CLOUD")            # e.g. BIO240357_IU

# IMAP inbox the bot's verified email points to.
IMAP_HOST = _env("E2E_IMAP_HOST")
IMAP_PORT = int(_env("E2E_IMAP_PORT", "993"))
IMAP_USER = _env("E2E_IMAP_USER")
IMAP_PASSWORD = _secret("E2E_IMAP_PASSWORD")
IMAP_MAILBOX = _env("E2E_IMAP_MAILBOX", "INBOX")

BOT_GITHUB_USERNAME = _env("E2E_BOT_USERNAME", "mc-e2e-bot")

# --------------------------------------------------------------------------------------
# Stage 0 (vendorize) settings.
# --------------------------------------------------------------------------------------

# Local path to the MorphoCloudWorkflow checkout used as the vendorize source.
MWF_DIR = Path(_env("E2E_MWF_DIR", str(Path.home() / "Desktop/Projects/MorphoCloudWorkflow")))
TEST_INSTANCES_DIR = Path(
    _env("E2E_TEST_INSTANCES_DIR", str(Path.home() / "Desktop/Projects/Test-Instances"))
)
# Mandatory; only skipped for harness-plumbing development (loud warning).
SKIP_VENDORIZE = _env("E2E_SKIP_VENDORIZE", "0") == "1"
VENDORIZE_GIT_AUTHOR_NAME = _env("E2E_GIT_AUTHOR_NAME", "morphocloud-e2e")
VENDORIZE_GIT_AUTHOR_EMAIL = _env("E2E_GIT_AUTHOR_EMAIL", "no-reply@morphocloud.org")
# Paths the vendorize commit is allowed to touch (commit-scope guard). Must match the
# `paths` list in MorphoCloudWorkflow/noxfile.py `vendorize` session.
VENDORIZE_ALLOWED_PREFIXES = (".github/", "scripts/", "cloud-config", ".pre-commit-config.yaml",
                              "issue-commands.md", "course-issue-commands.md",
                              "workshop-issue-commands.md")

# --------------------------------------------------------------------------------------
# Guardrails / timeouts (seconds).
# --------------------------------------------------------------------------------------

MAX_PENDING_E2E = int(_env("E2E_MAX_PENDING", "3"))   # abort run if more leftovers exist
SWEEP_MIN_AGE_HOURS = float(_env("E2E_SWEEP_MIN_AGE_HOURS", "2"))

# Provisioning tests (Milestones 2-4) actually create OpenStack instances. They are
# OFF unless explicitly enabled, so a cheap-suite run can never burn credits, and so
# they can't run before Milestone 0 has picked a working E2E_FLAVOR.
PROVISION_ENABLED = _env("E2E_PROVISION", "0") == "1"

TIMEOUT_WORKFLOW_RUN = int(_env("E2E_TIMEOUT_RUN", "180"))      # cheap workflow conclusion
TIMEOUT_CREATE = int(_env("E2E_TIMEOUT_CREATE", "1800"))        # /create incl. 20m cloud-init
TIMEOUT_COMMAND = int(_env("E2E_TIMEOUT_COMMAND", "600"))       # shelve/unshelve/delete
TIMEOUT_LABEL = int(_env("E2E_TIMEOUT_LABEL", "120"))
TIMEOUT_COMMENT = int(_env("E2E_TIMEOUT_COMMENT", "120"))
TIMEOUT_EMAIL = int(_env("E2E_TIMEOUT_EMAIL", "300"))
POLL_INTERVAL = float(_env("E2E_POLL_INTERVAL", "5"))

# Where the captured baseline lives (--capture-baseline writes here).
BASELINE_PATH = Path(__file__).resolve().parent / "baseline" / "expected.json"


def admin_token() -> str | None:
    """Admin token: explicit E2E_ADMIN_PAT, else the local `gh` auth token (muratmaga)."""
    if ADMIN_PAT:
        return ADMIN_PAT
    gh = shutil.which("gh")
    if not gh:
        return None
    try:
        out = subprocess.run(
            [gh, "auth", "token"], capture_output=True, text=True, timeout=15, check=True
        )
        return out.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


def imap_configured() -> bool:
    return bool(IMAP_HOST and IMAP_USER and IMAP_PASSWORD)


def openstack_configured() -> bool:
    return bool(OS_CLOUD) and shutil.which("openstack") is not None
