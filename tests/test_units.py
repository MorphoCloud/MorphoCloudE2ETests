"""Offline unit tests — no network, no secrets, no Test-Instances.

These validate the harness's own logic (parsers, denylist, the commit-scope guard,
polling) so the building blocks are trustworthy before the live suite runs.

Run: `pytest tests/`
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from e2e import config, forms, openstack
from e2e.gh import poll
from e2e.mailbox import MailMessage
from e2e.sweeper import SweepResult, _epoch

REPO_ROOT = Path(__file__).resolve().parent.parent


# -- flavor denylist -------------------------------------------------------------------

@pytest.mark.parametrize("flavor", ["m3.tiny", "m3.quad", "m3.small", "m3.medium"])
def test_flavor_allowed(flavor):
    assert config.assert_flavor_allowed(flavor) == flavor


@pytest.mark.parametrize("flavor", ["g3.large", "g3.xl", "g4.xl", "r3.large", "r3.xl", "m3.xl"])
def test_flavor_denied(flavor):
    with pytest.raises(config.UnsafeFlavorError):
        config.assert_flavor_allowed(flavor)


# -- issue-form bodies -----------------------------------------------------------------

def test_individual_body_has_parseable_flavor():
    body = forms.individual_body("m3.tiny")
    assert "### Cloud Computing Instance Flavor" in body
    # extract-issue-fields takes split(" - ")[0]; the line must start with the flavor.
    flavor_line = body.split("### Cloud Computing Instance Flavor", 1)[1].strip().splitlines()[0]
    assert flavor_line.split(" - ")[0] == "m3.tiny"


def test_workshop_body_fields_and_default_date():
    body = forms.workshop_body(duration_days=2, number_of_instances=3)
    for heading in ("Cloud Computing Instance Flavor", "Duration (days)",
                    "Number of Instances", "Workshop start date", "Workshop start time",
                    "Timezone", "Description"):
        assert f"### {heading}" in body
    # default start date is today (UTC) so start-12h is already past -> /create allowed
    import datetime as dt
    assert dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d") in body


# -- openstack naming ------------------------------------------------------------------

def test_instance_and_volume_names():
    # Test-Instances prefix is "morpho-cloud-test"
    assert openstack.instance_name(42).endswith("instance-42")
    assert openstack.volume_name(42) == "My-Data-42"


# -- mailbox structural parsers --------------------------------------------------------

def test_connection_url_and_passphrase():
    msg = MailMessage(
        subject="Your MorphoCloud instance",
        from_addr="no-reply@morphocloud.org", to_addrs="x@example.org",
        text="Connect here: https://guac.example.org/#/abc?token=zzz\nPassphrase: hunter2\n",
    )
    assert msg.connection_url() == "https://guac.example.org/#/abc?token=zzz"
    assert msg.passphrase() == "hunter2"


def test_credential_csv_row_count():
    csv = b"instance,ssh,url,passphrase\nA,exouser@1.1.1.1,https://a,p1\nB,exouser@2.2.2.2,https://b,p2\n"
    msg = MailMessage("Workshop credentials", "f", "t", text="see attached",
                      attachments={"credentials.csv": csv})
    rows = msg.credential_csv_rows()
    assert len(rows) == 2, rows  # header stripped


# -- poll() core -----------------------------------------------------------------------

def test_poll_returns_when_truthy():
    state = {"n": 0}

    def fn():
        state["n"] += 1
        return "ready" if state["n"] >= 3 else None

    assert poll(fn, timeout=2, interval=0.01, desc="x") == "ready"


def test_poll_times_out():
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        poll(lambda: None, timeout=0.2, interval=0.05, desc="never")
    assert time.monotonic() - start >= 0.2


# -- sweeper helpers -------------------------------------------------------------------

def test_sweepresult_and_epoch():
    r = SweepResult()
    assert not r.actioned()
    r.deleted_instances.append(7)
    assert r.actioned() and "7" in r.summary()
    assert _epoch("2026-06-08T00:00:00Z") == pytest.approx(
        __import__("datetime").datetime(2026, 6, 8).timestamp(), abs=86400
    )
    assert _epoch(None) is None
    assert _epoch("garbage") is None


# -- commit-scope guard (verify_vendorize.sh) ------------------------------------------

def _git(repo: Path, *args: str):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": __import__("os").environ["PATH"]})


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "seed").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "seed")
    return repo


def _run_guard(repo: Path) -> subprocess.CompletedProcess:
    script = REPO_ROOT / "e2e" / "scripts" / "verify_vendorize.sh"
    return subprocess.run(["bash", str(script), "HEAD"], cwd=repo,
                          capture_output=True, text=True)


def test_verify_vendorize_allows_vendored_paths(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / ".github").mkdir()
    (repo / ".github" / "workflows").mkdir()
    (repo / ".github" / "workflows" / "x.yml").write_text("name: x")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "h.sh").write_text("#!/bin/sh")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "vendorize")
    result = _run_guard(repo)
    assert result.returncode == 0, result.stderr


def test_verify_vendorize_rejects_foreign_paths(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "README.md").write_text("oops")          # not a vendored path
    (repo / ".github").mkdir()
    (repo / ".github" / "workflows").mkdir()
    (repo / ".github" / "workflows" / "x.yml").write_text("name: x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "mixed")
    result = _run_guard(repo)
    assert result.returncode == 1
    assert "README.md" in result.stderr
