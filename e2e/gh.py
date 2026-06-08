"""Thin GitHub REST client for the harness — explicit per-identity tokens.

Two identities are used: the bot (`mc-e2e-bot`, opens issues + posts user commands so
`issue.user.login == bot`) and an admin (for `/approve` and `workflow_dispatch`). Each
GitHubClient holds exactly one token, so identity is never ambiguous.

Everything is observation-driven: open an issue, comment a command, then poll the
observable outcome (labels, comments, workflow-run conclusions). The wait_* helpers
assume **serial execution** (concurrency = 1, per DESIGN.md §7) — they pick the newest
run for a workflow created after a captured timestamp.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import requests

from . import config

API = "https://api.github.com"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class GitHubError(RuntimeError):
    pass


def poll(
    fn: Callable[[], Any],
    *,
    timeout: float,
    interval: float = config.POLL_INTERVAL,
    desc: str = "condition",
) -> Any:
    """Call fn() until it returns truthy or timeout. Returns the truthy value or raises."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise TimeoutError(f"Timed out after {timeout:.0f}s waiting for {desc} (last={last!r})")


@dataclass
class Issue:
    number: int
    title: str
    html_url: str
    user_login: str
    raw: dict[str, Any]


class GitHubClient:
    def __init__(self, token: str, repo: str = config.REPO, *, label: str = "client"):
        if not token:
            raise GitHubError(f"GitHubClient({label}) created without a token")
        self.repo = repo
        self.label = label
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "morphocloud-e2e",
            }
        )

    # -- low-level ---------------------------------------------------------------------

    def _req(self, method: str, path: str, **kw) -> requests.Response:
        url = path if path.startswith("http") else f"{API}{path}"
        resp = self.session.request(method, url, timeout=30, **kw)
        if resp.status_code >= 300:
            raise GitHubError(
                f"{method} {url} -> {resp.status_code}: {resp.text[:400]}"
            )
        return resp

    def whoami(self) -> str:
        return self._req("GET", "/user").json()["login"]

    # -- issues ------------------------------------------------------------------------

    def open_issue(self, title: str, body: str, labels: Iterable[str] = ()) -> Issue:
        data = {"title": title, "body": body}
        if labels:
            data["labels"] = list(labels)
        j = self._req("POST", f"/repos/{self.repo}/issues", json=data).json()
        return Issue(j["number"], j["title"], j["html_url"], j["user"]["login"], j)

    def comment(self, issue: int, body: str) -> dict[str, Any]:
        return self._req(
            "POST", f"/repos/{self.repo}/issues/{issue}/comments", json={"body": body}
        ).json()

    def get_issue(self, issue: int) -> dict[str, Any]:
        return self._req("GET", f"/repos/{self.repo}/issues/{issue}").json()

    def issue_state(self, issue: int) -> str:
        return self.get_issue(issue)["state"]

    def labels(self, issue: int) -> list[str]:
        j = self._req("GET", f"/repos/{self.repo}/issues/{issue}/labels").json()
        return [lbl["name"] for lbl in j]

    def add_labels(self, issue: int, labels: Iterable[str]) -> None:
        self._req(
            "POST", f"/repos/{self.repo}/issues/{issue}/labels",
            json={"labels": list(labels)},
        )

    def remove_label(self, issue: int, label: str) -> None:
        # 404 is fine (label not present).
        try:
            self._req("DELETE", f"/repos/{self.repo}/issues/{issue}/labels/{label}")
        except GitHubError as exc:
            if "404" not in str(exc):
                raise

    def set_expiration_labels(self, issue: int, expirations: Iterable[str]) -> None:
        """Replace all expiration:* labels with the given set (e.g. ('expiration:1d',))."""
        for lbl in self.labels(issue):
            if lbl.startswith("expiration:"):
                self.remove_label(issue, lbl)
        self.add_labels(issue, expirations)

    def close_issue(self, issue: int, comment: str | None = None) -> None:
        if comment:
            self.comment(issue, comment)
        self._req("PATCH", f"/repos/{self.repo}/issues/{issue}", json={"state": "closed"})

    def list_comments(self, issue: int) -> list[dict[str, Any]]:
        return self._req(
            "GET", f"/repos/{self.repo}/issues/{issue}/comments?per_page=100"
        ).json()

    def list_issues(self, *, labels: str | None = None, state: str = "open",
                    limit: int = 100) -> list[dict[str, Any]]:
        path = f"/repos/{self.repo}/issues?state={state}&per_page={min(limit, 100)}"
        if labels:
            path += f"&labels={labels}"
        return [i for i in self._req("GET", path).json() if "pull_request" not in i]

    def sub_issues(self, parent: int) -> list[dict[str, Any]]:
        try:
            return self._req(
                "GET", f"/repos/{self.repo}/issues/{parent}/sub_issues?per_page=100"
            ).json()
        except GitHubError:
            return []

    # -- labels.yml definitions (repo labels) ------------------------------------------

    def repo_label_exists(self, name: str) -> bool:
        try:
            self._req("GET", f"/repos/{self.repo}/labels/{name}")
            return True
        except GitHubError:
            return False

    def list_repo_labels(self) -> list[str]:
        return [lbl["name"] for lbl in
                self._req("GET", f"/repos/{self.repo}/labels?per_page=100").json()]

    def ensure_label(self, name: str, *, color: str = "ededed", description: str = "") -> None:
        """Idempotently create a repo label (no-op if it already exists)."""
        if self.repo_label_exists(name):
            return
        try:
            self._req(
                "POST", f"/repos/{self.repo}/labels",
                json={"name": name, "color": color, "description": description},
            )
        except GitHubError as exc:
            if "422" not in str(exc):  # 422 = already exists (race)
                raise

    # -- workflow dispatch + runs ------------------------------------------------------

    def dispatch_workflow(self, filename: str, *, ref: str = "main",
                          inputs: dict[str, Any] | None = None) -> None:
        self._req(
            "POST", f"/repos/{self.repo}/actions/workflows/{filename}/dispatches",
            json={"ref": ref, "inputs": inputs or {}},
        )

    def _runs(self, filename: str, *, since: datetime, event: str | None = None) -> list[dict]:
        path = (
            f"/repos/{self.repo}/actions/workflows/{filename}/runs"
            f"?created=>={_iso(since)}&per_page=30"
        )
        if event:
            path += f"&event={event}"
        return self._req("GET", path).json().get("workflow_runs", [])

    def wait_for_run(self, filename: str, *, since: datetime, timeout: float,
                     event: str | None = None) -> dict[str, Any] | None:
        """Wait for the newest run of `filename` created at/after `since` to complete.

        Returns the completed run dict (inspect ['conclusion']), or None if no run
        appeared within `timeout` (e.g. the trigger's `if:` gated it out).
        """
        def newest():
            runs = self._runs(filename, since=since, event=event)
            return runs[0] if runs else None

        deadline = time.monotonic() + timeout
        run = None
        while time.monotonic() < deadline:
            run = newest()
            if run and run.get("status") == "completed":
                return run
            time.sleep(config.POLL_INTERVAL)
        return run  # may be None (never started) or an unfinished run (timed out)

    # -- waiters on observable side effects --------------------------------------------

    def wait_for_label(self, issue: int, predicate: Callable[[str], bool], *,
                       timeout: float = config.TIMEOUT_LABEL) -> str:
        return poll(
            lambda: next((lbl for lbl in self.labels(issue) if predicate(lbl)), None),
            timeout=timeout, desc=f"label matching predicate on #{issue}",
        )

    def wait_for_comment(self, issue: int, substring: str, *, since: datetime,
                         timeout: float = config.TIMEOUT_COMMENT) -> dict[str, Any]:
        since_iso = _iso(since)

        def match():
            for c in self.list_comments(issue):
                if c["created_at"] >= since_iso and substring in c["body"]:
                    return c
            return None

        return poll(match, timeout=timeout, desc=f"comment containing {substring!r} on #{issue}")
