# MorphoCloudE2ETests

Autonomous end-to-end tests for `MorphoCloudWorkflow` (MWF). The harness drives
`MorphoCloud/Test-Instances` — a private clone of the production `Instances` repo — the
same way a real user and a real workshop organizer would: it opens GitHub issues, posts
the IssueOps slash-commands (`/create`, `/shelve`, `/renew`, …), dispatches the cron
workflows, and then **asserts the workflows actually did the right thing** — by checking
workflow conclusions, issue labels, the comments MWF posts back, the live OpenStack
state, and (optionally) the real credential email that lands in the bot's inbox.

It exists to catch breakage from MWF code changes *before* it reaches users. Provisioning
runs on the smallest flavor that still boots cloud-init (`m3.tiny`) on Test-Instances'
own self-hosted runner, so a full run costs a few cents of allocation, never user-facing
instances.

See **[DESIGN.md](DESIGN.md)** for the design + per-workflow coverage audit, and
**[e2e/docs/setup_bot.md](e2e/docs/setup_bot.md)** for one-time credential setup.

## What it actually tests

Four suites, smallest/cheapest first. Each bullet is one real assertion against
Test-Instances.

### `cheap` — validation & gating, **no instances created** (~2–3 min)
The fast safety net: every check that doesn't need a VM.
- `/frobnicate` on an instance issue → `validate-command-instance` **fails** and MWF posts
  an "Unrecognized Commands" comment.
- `/delete_all` on a workshop issue → `validate-command-workshop` **rejects** it
  (also pins down that `/delete_all` is *not* a workshop command — workshop cleanup is the
  cron's job, not a comment).
- a real user `@`-mentions `@MorphoCloud/morphocloud-admins` → `on-admin-mention` runs
  (the admin-alert email path).
- dispatch `labels.yml` → the repo's label definitions sync (sample labels present), and a
  captured baseline of the full label set flags any future drift.
- workshop request with **duration > 5 days** → rejected at request-open (`needs-fix` +
  issue closed).
- workshop request with an **unparseable start date** → `resolve-workshop-schedule`
  rejects it (`needs-fix` + closed).
- a **non-member's** `/create` → refused with the onboarding message (opt-in; needs a 2nd
  account not in `MorphoCloudUsers`).

### `individual` — one real `m3.tiny`, full user lifecycle (~20 min)
- open an individual request → exactly **one** `flavor:` label is applied.
- `/create` → **reliable readiness**, a 3-layer check: OpenStack reports
  `exoSetup.status==complete`, the data volume is `in-use`, and an **in-guest SSH probe**
  confirms `/media/volume/MyData` is mounted, `/home/exouser` symlinks onto the volume,
  and Slicer is installed. (ACTIVE alone is not trusted.)
- *(if IMAP configured)* the credential email actually arrives, with a connection URL and
  passphrase.
- `/shelve` → `status:shelved` + OpenStack `SHELVED_OFFLOADED`.
- `/unshelve` → `status:active` and the **same floating IP is reused**.
- `/email` → a second credential email is sent.
- dispatch the reconcilers `update-request-status-label` and `collect-instance-uptime`.
- `/create` **again** → "already created" (idempotency — what workshop-backfill depends
  on); the running instance is **not** destroyed.
- `/delete_instance` → instance gone, **volume kept**.
- `/delete_volume` → volume gone.

### `workshop` — two real `m3.tiny`, organizer fan-out (~40 min)
- open a workshop request → schedule validated (gets a `start:` label, no `needs-fix`).
- admin `/approve` → `request:approved` (+ approval email if IMAP); then `/unapprove`
  and re-`/approve` work.
- organizer `/create` → **2 sub-issues fan out**.
- `workshop-backfill` builds the remainder; every sub-issue reaches `status:active`.
- `update-workshop` (the real credential-delivery cron) flips the parent to
  `workflow:instances-created`.
- each built instance passes the same in-guest readiness probe.
- *(if IMAP)* the organizer credential email contains a markdown table with **one row per
  instance**.

### `lifecycle` — time-gated automation, forced with short-expiry labels (~70 min)
Real cleanup/renewal pathways are normally days/weeks out; the harness injects
short-expiry labels (the cleanup analog of `m3.tiny`) so they fire immediately. The cron
derives an instance's age from its issue's `created_at`, so `expiration:0d` is "already
past" (deletes now) while a longer rung stays in the future.
- **individual instance lifecycle management** *(one instance, full story)*: provision →
  set the policy `[expiration:0d, expiration:1d]` and `/renew` (climbs to the `1d` rung)
  → an `automatic-instance-deleting` pass **spares** it (renew bought a day) → collapse
  the policy back to `expiration:0d` → the next pass **deletes** the instance **and**
  volume, sets `status:deleted`, **posts the expiration notice**, and closes the issue.
- **auto-shelve:** inject `timeout:0hrs` → `automatic-instance-shelving` shelves it.
- **auto-volume-delete:** detach the instance, mark `volume:expiration-pending`, dispatch
  with `graceperiod=0` → `automatic-volume-deleting` deletes the volume.
- **workshop cron cleanup** *(opt-in via `E2E_WORKSHOP_PARENT`)*: inject `expiration:0d` on
  each sub-issue → a single `automatic-instance-deleting` pass deletes every
  instance+volume and closes all sub-issues **and** the parent. The harness only triggers
  and asserts — it never comments on sub-issues.

> **Not covered:** the renewal *warning* email — it only fires for an instance aged into
> the 7-day window of an expiration > 7 days, which label injection on a fresh instance
> can't simulate. Full per-workflow audit: DESIGN.md §12.

## How it works

The harness only orchestrates and observes; all provisioning happens on Test-Instances'
self-hosted runner. Two invariants make it trustworthy:

- **Stage 0 (mandatory):** every run first vendorizes the chosen MWF ref → Test-Instances,
  so it always tests *current* code, never a stale clone.
- **No leaks:** each test force-cleans the instances and issues it created on teardown, and
  the run ends with a sweeper that deletes any `[E2E]`-tagged leftovers.

## Install

```bash
pip install -e .          # pytest + requests; IMAP via stdlib, OpenStack via `openstack` CLI
```

Configure credentials in a git-ignored `.env` (local) or Actions secrets (CI) — see the
table in [e2e/docs/setup_bot.md](e2e/docs/setup_bot.md).

## Run

### From the Actions UI (recommended — runs in CI, no local session to interrupt)

**Actions → E2E → Run workflow**, pick a **suite** (the dropdown value is in `code`):

| suite | what it does | instances | ~time |
|-------|--------------|-----------|-------|
| `cheap` | validation & gating (7 checks above) | none | 2–3 min |
| `individual` | full single-user lifecycle: create→readiness→shelve→…→delete | 1× `m3.tiny` | ~20 min |
| `workshop` | organizer fan-out: approve→create→backfill→credential delivery | 2× `m3.tiny` | ~40 min |
| `lifecycle` | renew-protects, auto-shelve, auto-delete, auto-volume-delete | 1× `m3.tiny` each | ~70 min |
| `all` | every suite | up to 2× `m3.tiny` | ~2 h |
| `sweep_only` (toggle) | just force-clean `[E2E]` leftovers | none | 1 min |

Results render in the run's **Summary** (pass/fail/skip table + collapsible failure
details) and as a `report.xml` artifact. Requires the `E2E_BOT_PAT` + `E2E_ADMIN_PAT` repo
secrets (see [setup_bot.md](e2e/docs/setup_bot.md)); the IMAP / OpenStack secrets are
optional and deepen the asserts (credential-email + direct OpenStack-state checks).

### Locally

```bash
pip install -e .
nox -s units                          # offline unit tests (no network/secrets)
nox -s e2e-cheap                       # the cheap suite (needs bot + admin tokens)
E2E_PROVISION=1 pytest -m individual   # or: workshop / lifecycle (real m3.tiny)
nox -s e2e-sweep                       # force-clean [E2E] leftovers (idempotent)
```

> The pytest **marker** for the validation suite is literally `cheap` (`pytest -m cheap`);
> the table above describes what that suite contains.

## Status — all milestones green ✅

Validated **live** against Test-Instances on `m3.tiny`:

- **Milestone 0** — `m3.tiny` cloud-init completes ✅
- **M1** `cheap` validation ✅ · **M2** `individual` lifecycle ✅ · **M3** `workshop` ✅ ·
  **M4** `lifecycle` (individual lifecycle management, auto-shelve, volume-delete) ✅
- Offline unit suite (`nox -s units`) ✅ · `--capture-baseline` mode ✅ · in-guest
  readiness probe (`e2e/assets/e2e-verify-instance.yml`, deployed to Test-Instances) ✅

Along the way the harness **found + fixed three production workshop bugs** (now on MWF
`main` + prod `Instances`): premature `workshop-backfill` "all up" comments,
`update-workshop` never firing (so the summary comment + credential CSV never happened),
and `update-workshop` double-emailing organizers.

Bot identity: **`amm554`** (fine-grained Test-Instances PAT). Full per-workflow coverage
audit: DESIGN.md §12.
