# MorphoCloud End-to-End Test Harness — Design

**Status:** IMPLEMENTED — Milestones 1–4 all pass live against Test-Instances (2026-06-09);
merged to `master`, runnable from the Actions UI. This doc is the design rationale + coverage
audit (§12); the README is the operator guide.
**Author:** drafted for muratmaga, 2026-06-08
**Goal:** Autonomously exercise *all* user-facing workflows in `MorphoCloudWorkflow`
(individual + workshop) against a real (but cheap) JS2 provision, so that breakage
introduced by code changes is caught before it reaches production (`Instances`).

---

## 1. Decisions locked (from the design Q&A)

| # | Decision | Choice | Consequence |
|---|----------|--------|-------------|
| 1 | **Where it runs** | Reuse **`MorphoCloud/Test-Instances`** | No new runner/allocation to stand up. Harness vendorizes current MWF `main` into Test-Instances, then drives it. Shares the `BIO240357_IU` allocation; must not collide with manual staging use (mitigated by `[E2E]` tagging + a sweeper). |
| 2 | **Create fidelity** | **Orchestration-level, with a *reliable* readiness check** | We skip the Slicer desktop (never observed to fail on a successfully provisioned box), but **"reach ACTIVE" is explicitly NOT the success signal** — a VM can be ACTIVE and even SSH-able while the data volume failed to mount. Success = the system's own authoritative `exoSetup={"status":"complete"}` marker **plus an independent guest-invariant probe** (volume mounted, home relocated onto it, Slicer present). See §3 "Reliable readiness." Enables a tiny flavor; if the prod image can't reach `complete` on `m3.tiny`, fall back to the smallest flavor that does (§4 Milestone 0). |
| 3 | **Trigger** | **On-demand only** | A `workflow_dispatch` workflow in this repo + a local `nox`/`pytest` entry point. No schedule. You run it before/after risky MWF changes. |
| 4 | **Actor + email** | **Dedicated test account + real inbox assertion** | A throwaway GitHub user (`mc-e2e-bot` or similar) is the issue *creator*, lives in `MorphoCloudUsers` + `MorphoCloudWorkshopOrganizers` + the join intake sheet, and its verified address is a controlled inbox the harness polls over IMAP to confirm the credential email actually arrived. |

---

## 2. What "all user workflows" means here

Mapped to the MWF entry points (see `MorphoCloudAppsScripts/SYSTEM-OVERVIEW.md` →
Workflows). The harness covers every command path a user or organizer can take.

### Individual instance lifecycle
`on-instance-request-opened` → `/create` → `/shelve` → `/unshelve` → `/renew` →
`/email` → `/delete_instance` / `/delete_volume` / `/delete_all`.

### Workshop lifecycle
`on-instance-request-opened` (workshop branch: schedule validation + labels) →
`/approve` / `/unapprove` → `/create` (sub-issue fan-out + first `BATCH` builds) →
`workshop-backfill` (trickle remainder) → `update-workshop` (readiness detect +
credential email to organizer) → **full cleanup of all N instances + volumes**
(see below — this is a tested pathway, not a best-effort afterthought).

### Cleanup, expiry & renewal — first-class, NOT deferred
These are core to the suite (an earlier draft wrongly parked them as "Phase 2 someday";
they run on every full pass — see §4 run ordering). All are exercised **without waiting
real time**, using short-expiry test labels that are the cleanup analog of `m3.tiny`:

- **`/renew`** — and its interaction with auto-delete: a renewed instance must move out
  of the deletion window and *not* get deleted.
- **`automatic-instance-shelving`** — forced via a `timeout:0hrs` label (uptime > 0 →
  shelve-eligible immediately).
- **`close-expired-issues`** (+ renewal warning via `send-renewal-email`) — the
  warning path forced via `expiration:1d` (lands inside the 7-day window); actual
  deletion forced via `expiration:0d` (created_at + 0 → already past).
- **`automatic-volume-deleting`** — forced via the workflow's own
  `expiration_graceperiod_days=0` dispatch input on a `volume:expiration-pending` volume.
- **Workshop teardown** is driven by **`close-expired-issues.yml`** (the
  window-anchored lifecycle cron), **not** by comments on sub-issues — `/delete_all`
  isn't even a valid workshop command. That workflow deletes each instance+volume,
  **closes each sub-issue, and closes the parent** once all sub-issues are closed
  ([close-expired-issues.yml:150-200](MorphoCloudWorkflow/.github/workflows/close-expired-issues.yml#L150-L200)).
  The test **triggers** that workflow (after injecting `expiration:0d` on each
  sub-issue) and asserts it did the teardown — it does not reimplement deletion. The
  dedicated harness **`test-workshop-deletion.yml`** (`workflow_dispatch`,
  `issue_number`, `dry_run`) runs the same logic per sub-issue for debugging. Leak-free
  workshop cleanup (all N gone, parent closed) is a named acceptance criterion.
- **`update-request-status-label`, `collect-instance-uptime`** — dispatched, asserted.

All are `workflow_dispatch`-able (the runner-1 crontab already fires them that way), so
the harness triggers them on demand after injecting the appropriate label — no schedule,
no 60-day wait. Expiry/renewal mechanics verified in
`close-expired-issues.yml` + `update-renew-label.yml` (2026-06-08).

### Validation / negative paths (Phase 1 — cheap, no provisioning)
Unknown command, non-member `/create`, quota exceeded, workshop `>5 days`, workshop
before `start − 12h`, malformed workshop date → `needs-fix` + issue closed. These are
the **highest value-per-credit** tests: fast, free, and they catch a large class of
regressions in the validation/gating layer.

### Out of scope (initial)
Course (`MC-*`) workflows — explicitly deferred per the brief ("individual and
workshop"). Course coverage is a clean Phase 3 add (same harness, a disposable
`MC-E2E` repo or Test-Instances course issues).

---

## 3. Architecture

```
                 workflow_dispatch (this repo)            local: nox -s e2e
                          │                                      │
                          ▼                                      ▼
                 ┌─────────────────────────────────────────────────────┐
                 │  e2e harness (pytest, runs on ubuntu-latest / laptop) │
                 │                                                       │
                 │  0. ALWAYS: vendorize MWF → Test-Instances + push     │
                 │     (mandatory, gating — abort run if it fails)        │
                 │  1. pre-run sweeper (leak guard, abort if too many)   │
                 │  2. for each scenario:                                │
                 │       open issue AS test-bot  ─────────────┐          │
                 │       post command(s)                      │ GitHub   │
                 │       poll: workflow run conclusion,        │  API     │
                 │             issue labels, progress comment  │          │
                 │       assert OpenStack state (optional)  ───┼ OS CLI   │
                 │       assert credential email arrived    ───┼ IMAP     │
                 │       finally: /delete_all + verify gone   ◄┘          │
                 │  3. post-run sweeper                                   │
                 │  4. report (JUnit XML → job summary)                  │
                 └─────────────────────────────────────────────────────┘
                          │ drives via issues/comments
                          ▼
        ┌──────────────────────────────────────────────┐
        │  MorphoCloud/Test-Instances (vendorized MWF)  │
        │  self-hosted runner → JS2 BIO240357_IU        │
        │  real provisioning on m3.tiny (cheap flavor)  │
        └──────────────────────────────────────────────┘
```

**Key point:** the harness itself does *no* provisioning. It orchestrates via the
GitHub API (open issue, comment a command) and then **observes** — exactly as a real
user would. All OpenStack work happens on Test-Instances' existing self-hosted runner.
The harness needs only: GitHub tokens, the test-inbox IMAP creds, and (optionally) a
**read-only** OpenStack credential for stronger assertions.

### Stage 0 — Vendorize is mandatory and gates the whole run

The point of the harness is to test **current MWF code**, so every run *must* first
sync Test-Instances to MWF before any scenario. This is not optional and not an input —
it is the first stage, and if it fails the run aborts (better no result than a green
result against stale workflows).

```bash
# Stage 0, run before pytest (in the e2e.yml workflow step AND as a session-scoped
# autouse fixture for local runs):
cd ~/Desktop/Projects/MorphoCloudWorkflow
pipx run nox -s vendorize -- ~/Desktop/Projects/Test-Instances/ --commit   # MUST be pipx run nox
cd ~/Desktop/Projects/Test-Instances
# verify the vendorize commit only touched vendored paths before trusting it:
git show --stat HEAD   # expect only .github/**, scripts/**, cloud-config, *-commands.md
git push origin main
```

Details that make this safe and correct (per `feedback_morphocloud_dev_protocols`):

- **`pipx run nox -s vendorize ... --commit`** is the only sanctioned vendorize path —
  never a venv/system nox, never a manual copy. `--commit` produces the standardized
  `fix: Update to MorphoCloud/MorphoCloudWorkflow@<SHA>` commit.
- **Vendorizes the local MWF working state**, so running the harness from a feature
  branch tests *that branch's* code (ideal for "check my change before it ships"),
  while running from `main` tests main. The harness records the MWF SHA it vendorized
  in the report.
- **No-op is fine:** if Test-Instances is already in sync, vendorize produces no commit;
  the harness detects "nothing to commit / push" and proceeds — it does not error.
- **Commit-scope guard:** before pushing, assert the commit touches only vendored paths
  (`.github/**`, `scripts/**`, `cloud-config`, `*-commands.md`). Anything outside that
  aborts the run for human review.
- **Lint the vendorized workflows before orchestrating** — run `actionlint` (and the
  relevant `pre-commit` hooks: `check-github-workflows`, `check-github-actions`) over the
  **changed** workflow/action files, and fail fast on error. This mainly protects the
  `mwf_ref = <feature-branch>` case, where the branch may not have passed MWF's own
  `ci.yml`; for `mwf_ref = main` it largely overlaps `ci.yml`, so scope it to the changed
  files rather than re-linting the whole tree.
- **Stable commit identity:** set `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` (and committer)
  to a recognizable e2e identity on the vendorize commit, so the auto-vendorize history is
  attributable and never picks up an ambient/wrong author.
- **No settle delay needed:** GitHub reads workflow files from the default branch at
  event time, so once the push lands the next issue/comment already uses the new
  workflows. (A few seconds' pause before the first scenario is prudent, not required.)
- **Target is Test-Instances only** — the harness has no path that can vendorize/push
  to production `Instances`.

### Reliable readiness — what "fully provisioned" means (and what it does NOT)

> An instance can be Nova-`ACTIVE`, and even SSH-able, while still broken — e.g. the
> data volume never mounted. So **`ACTIVE` is not the success signal, and neither is
> "the create workflow concluded success" on its own** (a future code change could drop
> a check the workflow currently makes). The harness verifies the *outcome* the way an
> independent observer would, in three layers, all of which must hold:

1. **Authoritative system marker — `exoSetup={"status":"complete"}`** on the OpenStack
   server (read via the read-only OS credential). This property is set *only* at the
   very end of `setup-instance`, after the volume is attached, Slicer copied, home
   relocated, and the box rebooted — all succeeded. This is the same marker the
   production system (status crons, Guacamole) treats as "ready," and it is distinct
   from Nova `ACTIVE`. Primary gate.
2. **Volume actually attached at the cloud layer** — `openstack volume show
   My-Data-<n>` reports `status: in-use` and an attachment to the instance ID. Catches
   "volume created but never attached."
3. **Independent in-guest invariant probe** — the real "disk mounted" proof, which can
   only be seen *inside* the VM. A small test-only **`e2e-verify-instance.yml`** runs
   on the Test-Instances self-hosted runner (so it uses the runner's already-authorized
   SSH key — no extra credential), takes the issue number, finds the IP, SSHes in, and
   asserts:
   - `mountpoint -q /media/volume/MyData` — the data volume is actually mounted;
   - `/home/exouser` is a symlink to `/media/volume/MyData/home/exouser` — home was
     relocated onto the volume;
   - `test -d /media/volume/MyData/Slicer` — Slicer is present on the volume;
   - `df` shows the volume at roughly its expected size.
   It exits non-zero with a clear message on any failure; the harness dispatches it and
   the **run conclusion is the readiness verdict**. (This file lives only in
   Test-Instances. Vendorize "copies, does not prune," so a target-only file survives —
   *confirm that for `nox -s vendorize`; if it prunes, add the filename to the noxfile
   exclude list.* As a fallback, the harness can SSH directly using `murat-key`, which
   the image also authorizes, if its private key is available to the harness host.)

> The same layered idea applies to **shelve/unshelve**: assert OpenStack
> `SHELVED_OFFLOADED` / back-to-`ACTIVE` **and** that unshelve reused the same floating
> IP (the URL must survive the cycle), not merely that the command's workflow passed.

**Why pytest as the harness framework:** fixtures give us guaranteed teardown
(finalizers run even on assertion failure → no leaked instances), markers give us
suite selection (`-m individual`, `-m workshop`, `-m cheap`), parametrization covers
the flavor/negative matrices, and JUnit XML drops straight into the Actions job
summary. The `workflow_dispatch` job is a thin wrapper around `pytest`.

---

## 4. Phased rollout

### Milestone 0 — Flavor smoke test (do this first, by hand)
Before writing any harness code, answer the one question that invalidates everything
else: **does the prod desktop image complete cloud-init on `m3.tiny`?**

1. On Test-Instances, create the `flavor:m3.tiny` label (`gh label create flavor:m3.tiny --repo MorphoCloud/Test-Instances`).
2. Open an individual request, set its flavor label to `flavor:m3.tiny`, `/create`.
3. Watch `setup-instance`: it polls `openstack console log show` for
   `{"status":"complete"}` for up to 1200s, then SSHes in. Two outcomes:
   - **Completes** → `m3.tiny` is our flavor. Done.
   - **Times out / OOMs** (likely on 1 vCPU / ~3 GB) → bump to the next size and retry
     (`m3.small` → `m3.quad` → `m3.medium`). Pin the smallest that goes green as
     `E2E_FLAVOR`. Document the result here.
4. `/delete_all`, confirm instance + volume + FIP released.

> The harness is built around a single `E2E_FLAVOR` constant. Milestone 0 sets it.

### Milestone 1 — Cheap validation suite (no provisioning)
Fast, free, high coverage. No instance is ever created. Ship this first — it already
catches a big fraction of "someone broke the validation/gating layer" regressions.

### Milestone 2 — Individual full lifecycle (one instance, `E2E_FLAVOR`)
The core happy path end to end, with guaranteed teardown.

### Milestone 3 — Workshop lifecycle (target N=2)
Fan-out + backfill + readiness email. N=2 is enough to exercise multi-instance,
the `BATCH` trickle, and `update-workshop`, while staying cheap.

### Milestone 4 — Lifecycle, expiry, renewal & cleanup (label-injection + dispatch)
`/renew` ↔ auto-delete interaction, auto-shelve (`timeout:0hrs`), auto-delete warning
(`expiration:1d`) and deletion (`expiration:0d`), auto-volume-delete (`graceperiod=0`),
status reconcile, uptime. **The destructive members of this set double as the run's
teardown** (see ordering below).

### Milestone 5 (later) — optional direct-desktop reachability probe.

---

## 4a. Run ordering — provision everything first, clean up last

> Requirement (from review): *test all the provisioning/lifecycle scenarios first, and
> only if they pass, run the cleanup pathways.* So cleanup is not a per-scenario
> `finally` that tears each instance down the moment it's made — it is the **final,
> ordered phase**, and the destructive workflows are themselves the things under test.
> This both (a) exercises real cleanup on real instances and (b) avoids re-provisioning.

A full run executes in strict order; a phase only starts if the previous one passed:

```
Stage 0  Vendorize MWF → Test-Instances (gating; abort on failure)
Phase A  Cheap validation suite (no provisioning)                    [Milestone 1]
Phase B  Pre-run leak guard / sweeper (abort if prior leaks exist)
Phase C  PROVISION + NON-destructive lifecycle — keep instances ALIVE:
           • individual: /create → reliable readiness check → /shelve →
             /unshelve (same IP) → /email (2nd inbox mail) → status reconcile → uptime
           • workshop:   request → /approve(+/unapprove) → /create → backfill →
             update-workshop readiness + organizer credential email → per-instance
             readiness check
         (No deletes yet. All boxes stay up through Phase C.)
Phase D  CLEANUP pathways — ONLY if Phase C fully passed. These ARE the teardown:
           • /renew ↔ auto-delete interaction (renew protects from deletion)
           • auto-shelve via timeout:0hrs
           • auto-delete: warning via expiration:1d, then deletion via expiration:0d
           • individual explicit: /delete_instance → /delete_volume (and a /delete_all)
           • auto-volume-delete via expiration_graceperiod_days=0
           • workshop: inject expiration:0d on each sub-issue, dispatch
             close-expired-issues once → it deletes all instances+volumes,
             closes all sub-issues, closes parent. Assert that outcome (no comments).
         Assert end state: zero E2E instances/volumes/orphan FIPs remain.
Phase E  Post-run sweeper (safety net) — force-delete anything still standing.
```

**If Phase C fails:** skip Phase D's *as-tests* execution and jump straight to the
**Phase E sweeper**, which force-deletes every `[E2E]`-named resource so a failed run
never leaks. (Cleanup-as-test needs healthy instances; cleanup-as-safety-net runs
unconditionally.) The sweeper is idempotent and also runs at start (Phase B).

This is the ordering pytest encodes via markers + `--maxfail` gating between phases
(e.g. Phase D items `depends_on` Phase C passing); the Phase E sweeper is a
session-finalizer that always runs.

---

## 5. Prerequisites / one-time setup checklist

These are the things that must exist before the harness can run green. Most are
one-time. **Items marked ⚠️ need a decision or a credential from you.**

- [ ] **`flavor:m3.tiny` label** (or whatever Milestone 0 selects) on
  `MorphoCloud/Test-Instances`. Created out-of-band — deliberately **not** added to
  MWF `labels.yml`, so it never appears on the production `Instances` repo. The
  harness re-asserts it each run with `gh label create --force`.
- [ ] **Short-expiry test labels** — the cleanup analog of `m3.tiny`, so the lifecycle
  pathways are testable in seconds instead of 60 days:
  - `expiration:1d`, `expiration:2d`, `renewed:1`, `renewed:2` **already exist** in
    `labels.yml` (so they're on Test-Instances already) — used for the warning + renew
    tests. (Auto-delete reads `expiration:Nd` as `created_at + N days`; `renewed:N`
    selects which expiration applies — verified in `close-expired-issues.yml`.)
  - `expiration:0d` and `timeout:0hrs` are **test-only** (not in `labels.yml`); the
    harness creates them with `gh label create --force` **after Stage 0** (so a
    labels-sync triggered by vendorize can't prune them mid-run). `expiration:0d` →
    `created_at + 0` is already past → auto-delete deletes on the next dispatch;
    `timeout:0hrs` → any uptime > 0 → auto-shelve shelves immediately.
- [ ] **`e2e-verify-instance.yml`** committed to Test-Instances (the in-guest readiness
  probe of §3). `runs-on: self-hosted`, `workflow_dispatch` with an `issue_number`
  input; SSHes to the instance with the runner's key and asserts mount/home/Slicer.
  Test-only — kept out of MWF; confirm `nox -s vendorize` doesn't prune it (else add to
  the noxfile exclude list).
- [ ] ⚠️ **Dedicated test GitHub account** (e.g. `mc-e2e-bot`) — full repeatable setup in
  **`e2e/docs/setup_bot.md`**. Needs:
  - Member of **`MorphoCloudUsers`** (gates `/create` on individual — [create-instance.yml:82-105](MorphoCloudWorkflow/.github/workflows/create-instance.yml#L82-L105)).
  - Member of **`MorphoCloudWorkshopOrganizers`** (gates workshop `/create`).
  - **Triage** (or write) collaborator on **Test-Instances** so it can open issues +
    comment and pass the `github/command` permission gate.
  - A **PAT** (repo + read:org scope) stored as a harness secret, used to open issues
    and post commands *as the bot* so `issue.user.login == mc-e2e-bot` (this is what
    routes the credential email to the test inbox). **Must be a user PAT, not a GitHub
    App token** — an App token makes the issue creator the app bot (breaking the
    email-routing + per-user auth subject) and types its comments as `Bot` (which
    `on-admin-mention` deliberately ignores).
- [ ] ⚠️ **Test inbox** the bot's email points to, reachable over **IMAP** (a Gmail
  with an app password, or a `+e2e` alias on an existing mailbox). The harness polls
  it to assert the credential email arrived. Provide host/user/app-password as secrets.
  **Verify it works before Milestone 2:** send a probe email and confirm `mailbox.py`
  receives it over IMAP — a silent inbox/app-password problem otherwise surfaces as a
  confusing email-assert failure.
- [ ] **Join intake sheet row** for the test account: `github_username = mc-e2e-bot`,
  `email = <test inbox>`, `email_verified = TRUE`. This makes
  `/lookup?github_username=mc-e2e-bot` resolve (used by both the individual
  `send-email` and the workshop `workshop-send-email` actions). Either register via
  `join.morphocloud.org` (ORCID → verify) or add the row directly.
- [ ] **Admin actor for `/approve`** — workshop approval is admin-only. The harness
  uses the existing `muratmaga` `gh` token (already authed here, in
  `morphocloud-admins`) to post `/approve`. No new credential.
- [ ] ⚠️ **Read-only OpenStack credential** for `BIO240357_IU` — now **needed, not
  optional**: the reliable readiness check (§3, layers 1–2) reads the `exoSetup`
  server property and the volume's `in-use`/attachment state directly, and the cleanup
  asserts (instance/volume/FIP gone) verify against OpenStack rather than trusting
  labels. A `clouds.yaml` entry with a read-only application credential is enough.
  (Layer 3, the in-guest probe, instead runs on the runner via `e2e-verify-instance.yml`.)
- [ ] **Confirm Test-Instances is on single-runner fallback** (verified: it has no
  `MORPHOCLOUD_ACQUIRE_RUNNER`/`CONTROL_RUNNER` vars, so create runs acquire+setup on
  one runner). The harness assumes serial execution — concurrency = 1.

> **Secrets the harness needs** (as this repo's Actions secrets + a local `.env` for
> dev): `E2E_BOT_PAT`, `E2E_IMAP_HOST` / `E2E_IMAP_USER` / `E2E_IMAP_PASSWORD`,
> optional `E2E_OS_CLOUDS_YAML`. The admin `gh` token is provided by the workflow's
> own `GITHUB_TOKEN` only if `muratmaga`-level admin rights are available to it;
> otherwise pass an admin PAT as `E2E_ADMIN_PAT`. (The org-secret-on-private-repo
> limitation from SYSTEM-OVERVIEW does **not** bite us here — this is one repo's own
> secrets, set directly.)

---

## 6. Scenario matrix + assertions

Notation: **C** = create cost (instances provisioned). Assertions are the *observable*
signals a healthy run produces. Exact label/comment strings should be **calibrated
from one known-good baseline run** (§8) rather than hardcoded from this doc, since the
templates evolve.

### 6.1 Cheap / validation (Milestone 1) — C=0

| Scenario | Trigger | Assert |
|----------|---------|--------|
| Unknown **instance** command | comment `/frobnicate` on an instance issue | `validate-command-instance` run **fails**; "Unrecognized Commands ❌" comment with the command list appended. |
| Unknown **workshop** command | comment `/frobnicate` (or `/delete_all`) on a workshop issue | `validate-command-workshop` run **fails**; unrecognized-command comment. *(Closes gap: also confirms `/delete_all` is rejected on workshop issues — see §6.4.)* |
| Admin team mention | bot (a non-Bot user) comments `@MorphoCloud/morphocloud-admins` on any issue | `on-admin-mention` run **success** (SMTP send step ran → admin var `maga@uw.edu`). Assert run success; assert inbox delivery only if the admin-email var is pointed at a pollable inbox. |
| `labels.yml` sync | `gh workflow run labels.yml` | run **success**; a sample of definitions exist on the repo (`flavor:g3.large`, `expiration:60d`, `status:active`). **Order:** run this *before* the harness (re)creates the test-only labels (`flavor:m3.tiny`, `expiration:0d`, `timeout:0hrs`), since a prune-on-sync would remove them — confirm whether `labels.yml` prunes; if it does, recreate test labels after. |
| Workshop > 5 days | open workshop request, Duration=`9` | request-open validation rejects: `needs-fix` label, issue closed, rejection comment, **no** "✅ validated", **no** admin email. |
| Workshop before `start − 12h` | approved workshop, `/create` with start far in future | `create-workshop` rejects with the "too early, window opens at …" comment; no sub-issues created. |
| Malformed workshop date | open workshop, date=`garbage` | `resolve-workshop-schedule` rejects → `needs-fix` + issue closed + "open a new one". |
| Non-member `/create` | issue opened by the env-toggled **`extra_user`** fixture (a 2nd account *not* in `MorphoCloudUsers`); else auto-skipped | "@X is not a registered MorphoCloud user" comment; run fails. *Runs only when `extra_user` creds are provided, else `pytest.skip`.* |

### 6.2 Individual — provision + non-destructive lifecycle (Phase C) — C=1

Keep the instance **alive** through this whole block; deletes happen in §6.4.

| Step | Trigger (actor) | Assert |
|------|-----------------|--------|
| Request opened | open `01-individual-instance-request` body w/ `E2E_FLAVOR` (bot) | `on-instance-request-opened` succeeds; labels `request-type:instance`, `flavor:<E2E_FLAVOR>`, `expiration:*`, initial commands comment + validation comment posted; admin-notify ran. |
| Ensure flavor label | harness applies `flavor:<E2E_FLAVOR>` if labeler didn't | issue carries exactly one `flavor:*` label = `E2E_FLAVOR`. |
| `/create` | comment `/create` (bot or admin) | `create-instance` run **success**; progress comment ends all-✅. |
| **Reliable readiness** (the key check) | — | **Layer 1:** OS `exoSetup.status == complete`. **Layer 2:** `openstack volume show My-Data-<n>` → `in-use` + attached to the instance. **Layer 3:** dispatch `e2e-verify-instance.yml` → conclusion **success** (mount + home-symlink + Slicer present in-guest). NOT merely "ACTIVE" or "run passed". |
| Credential email | side effect of create | within T, the test inbox receives the connection email referencing the instance/issue; URL + passphrase present. |
| `/shelve` | comment `/shelve` | `control-instance` success; `status:shelved`; OS `SHELVED_OFFLOADED`; **FIP disassociated** back to pool (Port==null). |
| `/unshelve` | comment `/unshelve` | success; `status:*` running; **same FIP/IP reused** (URL survives the cycle); reachable. |
| `/email` | comment `/email` | `send-email` re-sends; a **second** credential email lands in the inbox. |
| `update-request-status-label` | dispatch | `status:*` label matches OpenStack. |
| `collect-instance-uptime` | dispatch | run success (uptime recorded). |
| **Create idempotency** (the backfill relies on this) | re-comment `/create` on the **same** live issue | `check-instance-exists` → exists; the run posts "Instance **…** already created" and **fails cleanly** (no duplicate VM, no second volume, original instance untouched, FIP unchanged). This is the exact no-op `workshop-backfill` depends on when re-dispatching `create-instance-from-workflow`. |

### 6.3 Workshop — provision + readiness (Phase C) — C=2

Keep the 2 instances **alive**; cleanup is §6.4.

| Step | Trigger | Assert |
|------|---------|--------|
| Request opened | open `03-workshop-request`: `E2E_FLAVOR`, Duration=1, Count=2, start = now (so `start−12h` is past → create allowed), a valid TZ (bot) | schedule validation stamps `start:<epoch>` + `workshop-target:2`; admin-notify ran; no `needs-fix`. |
| `/approve` | comment `/approve` (**admin**) | `approve-workshop` adds `request:approved`; reply shows create-window-open time in organizer TZ; **`send-workshop-email-approval` emails the organizer (test bot) — assert the approval/welcome email lands in the test inbox** (looked up via `MORPHOCLOUD_WORKSHOP_EMAIL_LOOKUP_URL`, the same lookup the bot is registered in). |
| `/unapprove` then re-`/approve` | (admin) | approval marker clears, then re-sets. |
| `/create` | comment `/create` (bot organizer) | `create-workshop` creates **2** sub-issues assigned to the organizer; dispatches up to `BATCH` builds; parent labeled in-progress. |
| Backfill | dispatch `workshop-backfill` | remaining sub-issue(s) built; backfill comments only on live-count change. |
| Readiness per instance | each sub-issue | reliable readiness (§6.2 layers 1–3) holds for **both** instances. |
| Readiness + creds | dispatch `update-workshop` | when 2/2 `exoSetup==complete`, workshop labels flip to ready and the **organizer inbox receives the credential CSV** (2 rows). |

### 6.4 Cleanup, expiry & renewal (Phase D — these ARE the teardown) — reuses C from C/D

Runs **only after Phases C pass**; the destructive items tear the instances down for
real. Forced with the short-expiry test labels (no waiting). If a prior phase failed,
this block is skipped and the **Phase E sweeper** force-deletes instead.

| Pathway | Setup (no waiting) | Trigger | Assert |
|---------|--------------------|---------|--------|
| `/renew` ↔ auto-delete | on the live individual issue, replace expiration labels with `expiration:1d` + `expiration:2d` | dispatch `close-expired-issues` | instance is in the ≤7-day window → renewal **warning email** arrives + `renewal-notice:*` label set; instance **not** deleted. |
| `/renew` protects | (continuing) | comment `/renew`, then dispatch `close-expired-issues` again | `renewed:1` added, `renewal-notice:*` cleared; expiration now uses `2d`; instance still **not** deleted (renew pushed it out). |
| Auto-shelve | apply `timeout:0hrs` to a running instance | dispatch `automatic-instance-shelving` | instance shelves; `status:shelved`; OS `SHELVED_OFFLOADED`; FIP disassociated. (Unshelve it back if a later step needs it running.) |
| `/delete_instance` | live instance | comment `/delete_instance` | instance gone in OpenStack; **volume remains**; labels reflect deleted. |
| `/delete_volume` | (continuing) | comment `/delete_volume` | volume gone. |
| `/delete_all` | one fresh/remaining instance | comment `/delete_all` | instance **and** volume gone in one shot; FIP released. |
| Auto-delete (real deletion) | apply `expiration:0d` (created_at+0 → already past), remove other `expiration:*` | dispatch `close-expired-issues` | volume deleted first, then instance deleted via `control-instance-from-workflow`; `volume:deleted`; issue handling per workflow. |
| Auto-volume-delete | a volume carrying `volume:expiration-pending` | dispatch `automatic-volume-deleting` with `expiration_graceperiod_days=0` | volume past grace → deleted; `volume:expiration-pending` removed. |
| **Workshop full cleanup** (real lifecycle path) | inject `expiration:0d` on **each** of the 2 sub-issues (label only — no comments) | dispatch **`close-expired-issues.yml`** once | the workflow itself: deletes **every** workshop instance + volume; **closes every sub-issue**; **closes the parent** (last-sub-issue logic); **zero** workshop-prefixed resources remain in OpenStack. *(Named acceptance criterion — leak-free workshop teardown. We trigger + assert; we do NOT post commands on sub-issues, and do NOT reimplement deletion.)* |

> Workshop note: cleanup is the cron's job, not a command — `/delete_all` is not a valid
> workshop command and would be rejected by `validate-command-workshop`. `test-workshop-deletion.yml`
> is the per-sub-issue dry-run harness if a single sub-issue needs isolating.
>
> Individual deletion has three distinct **command** paths worth covering —
> `/delete_instance`→`/delete_volume`, `/delete_all`, and the auto-delete cron
> (`expiration:0d`). These reuse the Phase C instances (cleanup = teardown), so the
> marginal cost is ~one extra fresh instance for the standalone `/delete_all`.

---

## 7. Cost & safety guardrails (non-negotiable)

The whole point is *not* to burn credits or leak resources. Build these in from day one.

1. **Flavor is pinned** to `E2E_FLAVOR` (m3.tiny or the Milestone-0 fallback) in one
   constant. The harness refuses to run any scenario whose resolved flavor is a
   GPU/large flavor — a hardcoded denylist (`g3.*`, `g4.*`, `r3.*`, `m3.xl`).
2. **Cleanup is the ordered Phase D teardown, backed by an unconditional sweeper.**
   On a healthy run, the destructive workflows in §6.4 *are* the teardown (deleting the
   instances Phase C provisioned) — so cleanup is tested for real. Regardless of pass or
   fail, the **Phase E session-finalizer sweeper always runs** and force-deletes any
   `[E2E]` instance/volume/FIP still standing (poll OpenStack to *verify* gone). So even
   a crash mid-Phase-C cannot leak. (This replaces the earlier per-scenario `finally`
   teardown, which would have torn each box down before later phases could use it.)
3. **Pre-run leak guard.** Before starting, list `[E2E]`-tagged open issues and
   OpenStack instances matching the test name prefix. **Abort** if more than `K`
   (e.g. 3) already exist — that means a previous run leaked and a human should look.
4. **Sweeper.** A standalone `nox -s e2e-sweep` (run at Phase B start, Phase E end, and
   invokable any time) deletes any OpenStack instance/volume whose name maps to an
   `[E2E]` issue and is older than `N` hours, and closes stale `[E2E]` issues. Safe to
   run any time; idempotent. This is the safety net under guardrail #2.
   **Acceptance check (implement + prove first):** on a clean allocation the sweeper must
   find and delete **nothing** and touch **no** non-`[E2E]` resource — run it twice and
   confirm zero actions both times. Proving this before any provisioning test is what
   makes it trustworthy as the leak safety net.
5. **Concurrency = 1.** Test-Instances is single-runner + shares the FIP pool/mutex
   with any manual staging use. The harness never runs scenarios in parallel.
6. **Distinct tagging.** Every harness issue gets a `[E2E]` title prefix + an
   `e2e-test` label, so humans and the sweeper can always tell test churn from real
   staging work. (Mitigates the "collides with manual Test-Instances use" risk of
   Decision 1.)
7. **Small volume (optional).** Setting `VOLUME_SIZE_GB=10` on Test-Instances would
   cut storage churn, but it's a global repo var that also affects manual staging —
   flag for your call; default is to leave it at 100.
8. **Hard timeouts** per scenario (create ≤ ~30 min incl. the 20-min cloud-init wait;
   shelve/unshelve ≤ 10 min) so a hung runner can't idle a paid instance forever —
   on timeout, teardown fires.

---

## 8. Baseline calibration (Phase 0 of implementation)

Workflow comment/label text changes over time, so the harness must not hardcode
brittle string guesses. Instead:

1. Vendorize current MWF `main` → Test-Instances.
2. Run each happy-path scenario under the **`--capture-baseline`** mode (a pytest
   flag/marker): instead of asserting, it *records* the actual labels applied, the
   progress-comment final state, the workflow run names/conclusions, and the email
   subject/body shape into **`e2e/baseline/expected.json`** for review — no more "by hand."
3. Review and commit `expected.json`; encode the assertions to read from it, preferring
   **structural** checks — "a `flavor:*` label exists", "the `create-instance` run
   concluded success", "progress comment contains 7 ✅" — over exact-prose matching.
4. Re-run `--capture-baseline` and review the diff whenever a workflow's user-facing text
   intentionally changes (the diff *is* the change-review).

This makes the suite a *regression* detector (did behavior change vs. the last known
good?) rather than a spec re-implementation that drifts from the code.

---

## 9. Proposed repo layout

```
MorphoCloudE2ETests/
├── README.md                     # how to run; points here
├── DESIGN.md                     # this document
├── .github/workflows/
│   └── e2e.yml                   # workflow_dispatch: inputs {suite, mwf_ref, flavor, sweep_only}
├── pyproject.toml                # deps: pytest, requests/PyGithub, imapclient, openstacksdk (opt)
├── noxfile.py                    # sessions: e2e, e2e-sweep, e2e-cheap
├── e2e/
│   ├── config.py                 # repo=Test-Instances, E2E_FLAVOR, timeouts, denylist, accounts
│   ├── gh.py                     # open_issue(as_bot), comment(cmd), poll_labels, poll_run, poll_comment
│   ├── openstack.py              # (optional) assert_instance_state / volume / fip via read-only creds
│   ├── mailbox.py                # IMAP poll + STRUCTURAL parsers: wait_for_email(subject_contains,
│   │                             #   since, timeout); extract_connection_url(); extract_passphrase();
│   │                             #   parse_credential_csv()->rows (workshop attachment row count)
│   ├── sweeper.py                # leak guard + cleanup (idempotent)
│   ├── scripts/verify_vendorize.sh  # fails the run if the vendorize commit touches non-vendored paths
│   ├── baseline/expected.json    # captured by --capture-baseline; structural expected values (§8)
│   ├── docs/setup_bot.md         # runbook: mc-e2e-bot teams + Test-Instances collaborator +
│   │                             #   intake-sheet row + PAT (repeatable, see §5)
│   └── scenarios/
│       ├── test_validation.py    # Milestone 1 (cheap, C=0)
│       ├── test_individual.py    # Milestone 2 (C=1)
│       ├── test_workshop.py      # Milestone 3 (C=2)
│       └── test_lifecycle.py     # Milestone 4 (cron dispatch + label injection)
└── conftest.py                   # fixtures: bot/admin clients, inbox, os_client, sweeper, teardown,
                                  #   extra_user (env-toggled 2nd non-member for the §6.1 negative)
```

**Stage 0 (vendorize MWF → Test-Instances) always runs first** — it is not an input.
See §3, "Stage 0 — Vendorize is mandatory."

`e2e.yml` inputs:
- `suite`: `cheap` | `individual` | `workshop` | `lifecycle` | `all` (default `cheap`)
- `mwf_ref`: which MWF git ref to vendorize from (default the current checkout / `main`).
  Lets a run target a specific branch or SHA; the vendorized SHA is recorded in the report.
- `flavor`: override `E2E_FLAVOR` (still subject to the denylist).
- `sweep_only`: bool — run the sweeper and exit (skips Stage 0 and all scenarios).

---

## 10. Open questions for the morning

None block starting Milestone 0/1. These refine later milestones:

1. **Test account name + inbox.** Confirm the GitHub handle (`mc-e2e-bot`?) and which
   mailbox the harness should poll (a fresh Gmail + app password is simplest; a `+e2e`
   alias on an existing account also works). Needed before Milestone 2's email assert.
2. **Admin token for `/approve`.** OK to use a `muratmaga` admin PAT as `E2E_ADMIN_PAT`
   in this repo's secrets, or prefer a second admin identity? Needed for Milestone 3.
3. **Read-only OpenStack creds.** Want the stronger direct-OpenStack assertions
   (recommended for Milestone 4), or is asserting via issue labels/comments enough for
   now? If yes, provide a read-only application credential for `BIO240357_IU`.
4. **~~Auto-vendorize default~~ — RESOLVED.** Vendorize is **mandatory and runs first**
   on every run (§3, Stage 0). Test-Instances is always synced to the MWF ref under test
   before any scenario; a run aborts if the sync fails. (No longer an open question.)
5. **`/delete_all`-only + non-member negative** are the two scenarios that need an
   extra resource (one more C=1 run / a second non-member account). Include them, or
   skip to save credits/setup?

---

## 11. Immediate next actions

1. **Milestone 0 flavor smoke test** (§4) — by hand on Test-Instances. This is the
   single most important unknown; everything downstream depends on `E2E_FLAVOR`.
2. Stand up the **prerequisites checklist** (§5) for the test account + inbox.
3. Implement **Milestone 1 (cheap suite)** — fast feedback, zero credit cost, proves
   the harness plumbing (open issue, comment, poll, assert, teardown) end to end.
4. Then Milestones 2 → 3 → 4.

---

## 12. Coverage audit — every workflow/action in SYSTEM-OVERVIEW.md

Scope = the brief: **individual + workshop**. Course / instructor / analytics / CI are
out of scope (deferred, §D) — listed so coverage is explicit, not implied.

Legend: **Y** = directly tested + asserted · **y** = exercised as a dependency of a
tested path, no targeted assertion of its own branches · **GAP** = in-scope but not
covered · **—** = out of scope.

### A. Entry-point workflows (in scope)

| Workflow | Cov | Where / note |
|----------|:---:|--------------|
| `on-instance-request-opened.yml` | Y | §6.2 individual open; §6.1/§6.3 workshop branch |
| `create-instance.yml` | Y | §6.2 `/create` |
| `create-workshop.yml` | Y | §6.3 `/create`; §6.1 12h gate |
| `approve-workshop.yml` | Y | §6.3 `/approve` + `/unapprove` |
| `control-instance.yml` | Y | §6.2 `/shelve` `/unshelve`; §6.4 `/delete_instance` |
| `delete-instance-and-volume.yml` | Y | §6.4 `/delete_all` |
| `delete-volume.yml` | Y | §6.4 `/delete_volume` |
| `send-email.yml` | Y | §6.2 `/email` |
| `update-renew-label.yml` | Y | §6.4 `/renew` |
| `validate-command-instance.yml` | Y | §6.1 unknown cmd + every individual cmd routes through it |
| `validate-command-workshop.yml` | Y | §6.1 unknown-workshop-command negative + valid workshop commands |
| `on-admin-mention.yml` | Y | §6.1 admin-mention comment → run success |

### B. Scheduled / lifecycle workflows (in scope)

| Workflow | Cov | Where |
|----------|:---:|-------|
| `automatic-instance-shelving.yml` | Y | §6.4 via `timeout:0hrs` |
| `close-expired-issues.yml` | Y | §6.4 warning + deletion + workshop teardown |
| `automatic-volume-deleting.yml` | Y | §6.4 via `expiration_graceperiod_days=0` |
| `update-request-status-label.yml` | Y | §6.2 dispatch |
| `collect-instance-uptime.yml` | y | §6.2 dispatch — asserts run success only (data-write not verified) |
| `update-workshop.yml` | Y | §6.3 readiness + organizer credential CSV |
| `workshop-backfill.yml` | Y | §6.3 backfill |

### C. Reusable + manual/utility workflows (in scope)

| Workflow | Cov | Where / note |
|----------|:---:|--------------|
| `validate-request.yml` | y | §6.2 — asserts validation comment posted, not deep field-by-field validation |
| `request-labeler.yml` | Y | §6.2 flavor + expiration labels |
| `request-initial-comments.yml` | Y | §6.2 initial commands comment |
| `request-notify-admin.yml` | y | §6.2 — asserts the job ran; admin-email delivery only if admin inbox wired |
| `request-notify-admin-workshop.yml` | y | §6.3 — same |
| `send-renewal-email.yml` | Y | §6.4 renewal warning email to inbox |
| `create-instance-from-workflow.yml` | Y | §6.3 workshop create + backfill dispatch it |
| `control-instance-from-workflow.yml` | y | invoked by auto-shelve/auto-delete (§6.4); not dispatched directly |
| `delete-volume-from-workflow.yml` | y | invoked by auto-volume-delete (§6.4) |
| `labels.yml` | Y | §6.1 `workflow_dispatch` + assert label definitions synced |
| `test-workshop-deletion.yml` | — | it's a test *tool* (and "delete before prod"), not a target |
| `ci.yml` | — | MWF's own lint CI; not a user workflow |

### D. Composite actions (in scope)

| Action | Cov | Where / note |
|--------|:---:|--------------|
| `create-ip` | Y | §6.2 FIP assigned; §6.2 reuse-on-unshelve |
| `create-instance` | Y | §6.2 |
| `setup-instance` | Y | §6.2 readiness (its `exoSetup=complete` tail) |
| `control-instance` | Y | §6.2/§6.4 |
| `delete-volume` | Y | §6.4 |
| `update-request-status-label` | Y | §6.2 |
| `define-instance-name` / `define-volume-name` | Y | implicit in every create; names asserted |
| `generate-connection-url` | Y | §6.2 Guacamole URL present in credential email |
| `send-email` | Y | §6.2 `/create` + `/email` |
| `workshop-send-email` | Y | §6.3 organizer CSV |
| `lookup-email` | Y | §6.2 individual + §6.3 workshop lookup |
| `extract-issue-fields` | Y | §6.2 flavor parse |
| `extract-workshop-fields` | Y | §6.3 |
| `resolve-workshop-schedule` | Y | §6.1 malformed/early + §6.3 valid |
| `check-team-membership` | Y | §6.3 workshop gate; §6.1 non-member (optional) |
| `resolve-team-members` | y | exercised in create/control/approve allowlists |
| `check-approval` | Y | §6.3 |
| `update-approval` | Y | §6.3 `/approve` + `/unapprove` |
| `comment-progress` | Y | §6.2 all-✅ progress comment |
| `retrieve-metadata` | y | exercised by `/create`+`/email`; asserted only via email contents |
| `check-instance-exists` | Y | §6.2 create-idempotency (re-`/create` → "already created", clean fail) |
| `check-volume-exists` | y | same — reuse-volume branch not tested |
| `check-js2-status` | y | runs at create-time; **outage branch untestable** (can't force a JS2 outage) |
| `send-workshop-email-approval` | Y | §6.3 `/approve` → approval email asserted in test inbox (fires on every `/approve`, not onboarding-gated) |

### E. Out of scope (course / instructor / analytics / CI) — deliberately NOT tested

`on-course-request-opened.yml`, `create-course-instance.yml`,
`validate-command-course.yml`, `validate-request-course.yml`, `enroll-students.yml`,
`runner-setup-helper.yml`, `course-send-email`; `instructor-tools/enrollment-check.yml`;
`MorphoCloudAnalytics` collectors + `token-expiry-check.yml`; `morphocloud-monitor`; the
Apps Script projects + `join` app. These belong to the course / admin planes — Milestone
5+ if you want them later.

### Verdict

**Every in-scope workflow and action is now tested.** The five gaps from the prior
revision are folded in as first-class tests:

1. `on-admin-mention.yml` → §6.1 admin-mention test.
2. `send-workshop-email-approval` → §6.3 `/approve` approval-email assertion (it fires on
   every `/approve`, so no fresh onboarding is needed — the earlier concern was wrong).
3. `validate-command-workshop.yml` → §6.1 unknown-workshop-command negative.
4. `labels.yml` → §6.1 `workflow_dispatch` + label-sync assertion.
5. Create-idempotency (`check-instance-exists`) → §6.2 re-`/create` → "already created"
   clean no-op (the behaviour `workshop-backfill` relies on).

**Residual, accepted-by-dependency** (exercised inside a tested path, no targeted branch
assertion — marked `y` in the tables; not worth dedicated tests):
- `check-volume-exists` reuse-volume branch (the create-idempotency test fails at
  `check-instance-exists` before reaching it; a shelve→delete-instance→re-create cycle
  would hit it, but that's a deep edge case);
- `check-js2-status` outage branch — **untestable** without forcing a real JS2 outage;
- deep field-by-field validation in `validate-request.yml` (we assert the comment posts);
- `collect-instance-uptime` data write (we assert run success);
- `control-instance-from-workflow` / `delete-volume-from-workflow` — exercised via the
  auto-shelve/auto-delete/auto-volume-delete crons rather than dispatched directly.

Course / instructor-tools / analytics / monitor / Apps Script remain out of scope (§E).
