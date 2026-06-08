"""Stage 0 — vendorize MorphoCloudWorkflow → Test-Instances (mandatory, gating).

Every run syncs Test-Instances to the MWF ref under test BEFORE any scenario, so the
harness tests *current* code. If this fails, the run aborts (better no result than a
green result against stale workflows). Vendorize is done the one sanctioned way:
`pipx run nox -s vendorize -- <Test-Instances>/ --commit` (never venv/system nox).

Steps: vendorize+commit (with a stable e2e author identity) → commit-scope guard
(verify_vendorize.sh) → lint changed workflows (actionlint/pre-commit, best-effort) →
push. No-op (already in sync) is fine.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import config

_SCRIPT = Path(__file__).resolve().parent / "scripts" / "verify_vendorize.sh"


class Stage0Error(RuntimeError):
    pass


@dataclass
class Stage0Result:
    mwf_sha: str
    changed: bool
    pushed: bool


def _run(cmd: list[str], *, cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, env=env)
    if proc.returncode != 0:
        raise Stage0Error(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    return proc


def _git(cwd: Path, *args: str) -> str:
    return _run(["git", *args], cwd=cwd).stdout.strip()


def _changed_files(repo: Path, commit: str = "HEAD") -> list[str]:
    out = _run(["git", "show", "--name-only", "--pretty=format:", commit], cwd=repo).stdout
    return [ln for ln in out.splitlines() if ln.strip()]


def _lint_changed(repo: Path, files: list[str]) -> None:
    """Best-effort actionlint + pre-commit on changed workflow files. Warn if tools absent."""
    workflows = [f for f in files if f.startswith(".github/workflows/") and f.endswith((".yml", ".yaml"))]
    if not workflows:
        return
    if shutil.which("actionlint"):
        proc = subprocess.run(["actionlint", *workflows], cwd=str(repo),
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise Stage0Error(f"actionlint failed on vendorized workflows:\n{proc.stdout}\n{proc.stderr}")
    else:
        print("Stage 0: actionlint not installed — skipping workflow lint (MWF ci.yml covers main).")
    if shutil.which("pre-commit") and (repo / ".pre-commit-config.yaml").exists():
        proc = subprocess.run(
            ["pre-commit", "run", "--files", *files,
             "check-github-workflows", "check-github-actions"],
            cwd=str(repo), capture_output=True, text=True,
        )
        # pre-commit returns non-zero on hook failure; surface it.
        if proc.returncode != 0:
            raise Stage0Error(f"pre-commit checks failed:\n{proc.stdout}\n{proc.stderr}")


def run_stage0(*, push: bool = True) -> Stage0Result:
    mwf = config.MWF_DIR
    target = config.TEST_INSTANCES_DIR
    if not mwf.exists():
        raise Stage0Error(f"MWF checkout not found at {mwf} (set E2E_MWF_DIR)")
    if not target.exists():
        raise Stage0Error(f"Test-Instances checkout not found at {target} (set E2E_TEST_INSTANCES_DIR)")

    mwf_sha = _git(mwf, "rev-parse", "HEAD")
    before = _git(target, "rev-parse", "HEAD")

    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME=config.VENDORIZE_GIT_AUTHOR_NAME,
        GIT_AUTHOR_EMAIL=config.VENDORIZE_GIT_AUTHOR_EMAIL,
        GIT_COMMITTER_NAME=config.VENDORIZE_GIT_AUTHOR_NAME,
        GIT_COMMITTER_EMAIL=config.VENDORIZE_GIT_AUTHOR_EMAIL,
    )

    # The one sanctioned vendorize path (see feedback_morphocloud_dev_protocols).
    _run(
        ["pipx", "run", "nox", "-s", "vendorize", "--", f"{target}/", "--commit"],
        cwd=mwf, env=env,
    )

    after = _git(target, "rev-parse", "HEAD")
    changed = after != before
    if not changed:
        print(f"Stage 0: Test-Instances already in sync with MWF@{mwf_sha[:9]} (no-op).")
        return Stage0Result(mwf_sha=mwf_sha, changed=False, pushed=False)

    # Commit-scope guard.
    _run(["bash", str(_SCRIPT), "HEAD"], cwd=target)
    # Lint the changed workflows.
    _lint_changed(target, _changed_files(target))

    pushed = False
    if push:
        _run(["git", "push", "origin", "HEAD:main"], cwd=target)
        pushed = True
        print(f"Stage 0: vendorized MWF@{mwf_sha[:9]} → Test-Instances ({after[:9]}), pushed.")
    return Stage0Result(mwf_sha=mwf_sha, changed=True, pushed=pushed)
