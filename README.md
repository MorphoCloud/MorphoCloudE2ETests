# MorphoCloudE2ETests

Autonomous end-to-end testing of the `MorphoCloudWorkflow` user workflows
(individual + workshop), run against `MorphoCloud/Test-Instances` on a cheap
`m3.tiny`-class flavor to catch breakage from code changes.

See **[DESIGN.md](DESIGN.md)** for the full design, scenario matrix, and coverage audit,
and **[e2e/docs/setup_bot.md](e2e/docs/setup_bot.md)** for one-time credential setup.

## How it works

The harness drives Test-Instances exactly like a real user — opens issues, posts
IssueOps commands via the GitHub API — and *observes* the outcome (workflow
conclusions, labels, comments, OpenStack state, the real credential email). All
provisioning happens on Test-Instances' own self-hosted runner; the harness only
orchestrates and asserts. Every run first **vendorizes current MWF → Test-Instances**
(Stage 0, mandatory) so it always tests current code.

## Install

```bash
pip install -e .          # pytest + requests; IMAP via stdlib, OpenStack via `openstack` CLI
```

Configure credentials in a git-ignored `.env` (local) or Actions secrets (CI) — see the
table in [e2e/docs/setup_bot.md](e2e/docs/setup_bot.md).

## Run

```bash
# Milestone 1 — cheap validation, NO provisioning (C=0). Start here.
nox -s e2e-cheap
#   or:  pytest -m cheap

# Provisioning suites (Milestones 2-4) — create real (cheap) instances.
# OFF unless E2E_PROVISION=1, and only after Milestone 0 has set E2E_FLAVOR.
E2E_PROVISION=1 pytest -m individual
E2E_PROVISION=1 pytest -m workshop
E2E_PROVISION=1 pytest -m lifecycle

# Force-clean any [E2E] leftovers (idempotent; safe any time)
nox -s e2e-sweep
```

In CI: **Actions → E2E → Run workflow** (`.github/workflows/e2e.yml`), pick a suite.

## Status (implementation)

- ✅ Harness foundation: config, GitHub/OpenStack/IMAP clients, sweeper, Stage 0
  vendorize (+ commit-scope guard + lint), fixtures, workflow entry point.
- ✅ **Milestone 1** — cheap validation suite (7 tests), runnable once the bot account +
  PATs exist.
- 🚧 **Milestones 2-4** — individual / workshop / lifecycle scenarios are implemented and
  collected but **gated behind `E2E_PROVISION=1`**; they need:
  - **Milestone 0** done first: confirm the smallest flavor whose cloud-init completes,
    set `E2E_FLAVOR` (see DESIGN.md §4).
  - `e2e-verify-instance.yml` committed to Test-Instances (in-guest readiness probe).

See DESIGN.md §11 for the next-actions checklist.
