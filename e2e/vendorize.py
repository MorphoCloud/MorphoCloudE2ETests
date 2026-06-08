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
    """Best-effort actionlint on changed workflow files (skipped if not installed).

    Deliberately does NOT shell out to pre-commit: running pre-commit here is both
    redundant (MWF's own ci.yml runs it on `main`, which is the vendorize source) and
    risky — its hooks (e.g. prettier) create cruft (node_modules/.cache) in the target
    that the next vendorize's `git add -A` would commit.
    """
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
    # `--commit` runs `git commit`, which exits non-zero with "nothing to commit" when
    # Test-Instances is already in sync. Tolerate exactly that no-op (working tree stays
    # clean, HEAD unchanged); fail on any other vendorize error.
    vend = subprocess.run(
        ["pipx", "run", "nox", "-s", "vendorize", "--", f"{target}/", "--commit"],
        cwd=str(mwf), env=env, capture_output=True, text=True,
    )
    after = _git(target, "rev-parse", "HEAD")
    if vend.returncode != 0:
        dirty = _git(target, "status", "--porcelain")
        if dirty or after != before:
            raise Stage0Error(
                f"vendorize failed ({vend.returncode}):\nstdout:\n{vend.stdout}\n"
                f"stderr:\n{vend.stderr}"
            )
        print("Stage 0: vendorize is a no-op (Test-Instances already in sync).")
    changed = after != before

    # Push whenever local main is AHEAD of origin/main — not only when *this* run made a
    # commit. This recovers the case where a prior run committed but failed to push (e.g.
    # the guard rejected it), so the fix-and-rerun doesn't silently leave origin stale.
    _git(target, "fetch", "origin", "main", "-q")
    unpushed = [s for s in _git(target, "rev-list", "origin/main..HEAD").splitlines() if s]
    if not unpushed:
        print(f"Stage 0: Test-Instances already in sync with MWF@{mwf_sha[:9]} (nothing to push).")
        return Stage0Result(mwf_sha=mwf_sha, changed=changed, pushed=False)

    # Commit-scope guard on every unpushed commit, then lint the net changed files.
    for sha in unpushed:
        _run(["bash", str(_SCRIPT), sha], cwd=target)
    net_changed = [ln for ln in _run(
        ["git", "diff", "--name-only", "origin/main..HEAD"], cwd=target).stdout.splitlines() if ln]
    _lint_changed(target, net_changed)

    pushed = False
    if push:
        _run(["git", "push", "origin", "HEAD:main"], cwd=target)
        pushed = True
        print(f"Stage 0: synced Test-Instances → MWF@{mwf_sha[:9]} ({after[:9]}), "
              f"pushed {len(unpushed)} commit(s).")
    return Stage0Result(mwf_sha=mwf_sha, changed=changed, pushed=pushed)
