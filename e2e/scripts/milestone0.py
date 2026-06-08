"""Milestone 0 — flavor smoke test (DESIGN.md §4).

Opens an individual instance request on Test-Instances as the bot, `/create`s it, and
reports whether `create-instance.yml` (which includes the 20-min cloud-init wait in
`setup-instance`) completes — i.e. whether the prod desktop image's cloud-init finishes
on the given flavor. Always tears the instance down with `/delete_all`.

Usage:  python e2e/scripts/milestone0.py [flavor]     (default: E2E_FLAVOR)

Needs only the bot + admin tokens (no OpenStack/inbox creds). Does NOT run Stage 0
vendorize (assumes Test-Instances already synced).
"""

from __future__ import annotations

import sys

from e2e import config, forms
from e2e.gh import GitHubClient, utcnow


def _ensure_flavor_label(admin: GitHubClient, issue: int, flavor: str) -> None:
    label = f"flavor:{flavor}"
    admin.ensure_label(label, color="d93f0b", description="E2E tiny flavor")
    for existing in admin.labels(issue):
        if existing.startswith("flavor:") and existing != label:
            admin.remove_label(issue, existing)
    if label not in admin.labels(issue):
        admin.add_labels(issue, [label])


def _failed_steps(admin: GitHubClient, run_id: int) -> list[str]:
    try:
        jobs = admin._req("GET", f"/repos/{config.REPO}/actions/runs/{run_id}/jobs").json()
    except Exception:
        return []
    out = []
    for job in jobs.get("jobs", []):
        for step in job.get("steps", []):
            if step.get("conclusion") == "failure":
                out.append(f"{job['name']} → {step['name']}")
    return out


def main() -> int:
    flavor = sys.argv[1] if len(sys.argv) > 1 else config.E2E_FLAVOR
    config.assert_flavor_allowed(flavor)
    if not config.BOT_PAT:
        print("ERROR: E2E_BOT_PAT not set", file=sys.stderr)
        return 2
    bot = GitHubClient(config.BOT_PAT, label="bot")
    admin = GitHubClient(config.admin_token(), label="admin")

    for lbl in (config.E2E_LABEL, *config.INDIVIDUAL_REQUEST_LABELS):
        admin.ensure_label(lbl)

    issue = bot.open_issue(
        f"{config.TITLE_PREFIX} Milestone 0 smoke ({flavor})",
        forms.individual_body(flavor),
        labels=(config.E2E_LABEL, *config.INDIVIDUAL_REQUEST_LABELS),
    )
    num = issue.number
    print(f"opened #{num}: {issue.html_url}", flush=True)
    verdict = "UNKNOWN"
    try:
        _ensure_flavor_label(admin, num, flavor)
        since = utcnow()
        bot.comment(num, "/create")
        print("/create posted; waiting up to 2400s for create-instance.yml "
              "(incl. ~20m cloud-init)...", flush=True)
        run = admin.wait_for_run("create-instance.yml", since=since, timeout=2400,
                                 event="issue_comment")
        if run is None:
            verdict = "create-instance.yml NEVER RAN (check the runner / workflow gating)"
        elif run.get("status") != "completed":
            verdict = f"TIMED OUT (status={run.get('status')}) — {run.get('html_url')}"
        elif run.get("conclusion") == "success":
            verdict = f"SUCCESS — {flavor} cloud-init completes. {run.get('html_url')}"
        else:
            steps = _failed_steps(admin, run["id"])
            verdict = (f"FAILED ({run.get('conclusion')}) — {run.get('html_url')}\n"
                       f"  failed steps: {steps or '(could not fetch)'}")
        print(f"\n=== MILESTONE 0 VERDICT [{flavor}] ===\n{verdict}\n", flush=True)
    finally:
        print(f"tearing down #{num} with /delete_all ...", flush=True)
        since_del = utcnow()
        bot.comment(num, "/delete_all")
        try:
            drun = admin.wait_for_run("delete-instance-and-volume.yml", since=since_del,
                                      timeout=900, event="issue_comment")
            print(f"/delete_all conclusion: {drun.get('conclusion') if drun else 'no run'}",
                  flush=True)
        except Exception as exc:
            print(f"WARNING: could not confirm /delete_all ({exc}). Sweep if needed.", flush=True)
        try:
            if admin.issue_state(num) == "open":
                admin.close_issue(num, "Milestone 0 smoke complete.")
        except Exception:
            pass
        print(f"done (issue #{num}).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
