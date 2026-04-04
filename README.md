# Plan: MorphoCloud End-to-End Test Suite

## Background: System Architecture & Environment Model

### The Three-Layer Stack

MorphoCloud is a three-layer system. Any meaningful E2E test must exercise all three layers together:

1. **Google Forms + Apps Script (GAS)** — User intake: form submission, email verification, GitHub org invitation. Runs entirely on Google infrastructure. Code lives in `MorphoCloudAppsScripts` and is deployed to Google Apps Script via `clasp`. The live deployment is at a stable URL that never changes between code pushes.

2. **GitHub IssueOps** — Instance lifecycle management: users open GitHub issues and post slash commands (`/create`, `/shelve`, `/unshelve`, `/delete_all`, etc.) as comments. GitHub Actions workflows respond to these events, validate authorization, and drive cloud provisioning.

3. **OpenStack (JetStream2)** — The actual cloud infrastructure: virtual machines, volumes, floating IPs. All OpenStack operations are performed by GitHub Actions runners that are physically co-located with the JetStream2 allocation.

### Production vs. Test: Two Instance Repos

| | `MorphoCloudInstances` (Production) | `MorphoCloudInstancesTest` (Staging) |
|---|---|---|
| **Purpose** | Serves real users | E2E testing and workflow development |
| **Workflow code** | Vendorized from `MorphoCloudWorkflow` | Vendorized from `MorphoCloudWorkflow` (same) |
| **OpenStack allocation** | Production JetStream2 project | Separate test JetStream2 project |
| **Runner** | Production self-hosted runner | Test self-hosted runner |
| **GitHub org** | `MorphoCloud` (same) | `MorphoCloud` (same) |
| **GAS endpoints** | Same stable deployment URLs | Same stable deployment URLs |
| **Repo variables/secrets** | Production values (prod allocation, etc.) | Test values (test allocation, etc.) |
| **Bot in ADMINS** | No | Yes — enables automated `/approve` in tests |
| **Touch in E2E tests** | **Never** | Yes — all tests run here |

### How Vendorizing Works

`MorphoCloudWorkflow` is the **source of truth** for all workflow code (`.github/workflows/`, `.github/actions/`, `.github/ISSUE_TEMPLATE/`). Neither `MorphoCloudInstances` nor `MorphoCloudInstancesTest` is ever edited directly. Instead, changes are made in `MorphoCloudWorkflow` and then "vendorized" into the target repo using:

```bash
cd ~/Desktop/Projects/MorphoCloudWorkflow
pipx run nox -s vendorize -- ~/Desktop/Projects/MorphoCloudInstancesTest/ --commit
```

This copies the relevant files verbatim, creating a commit in the target repo with a message that includes the full `git shortlog` of what changed in `MorphoCloudWorkflow`. The result is that **both instance repos always contain bit-for-bit identical workflow files**.

**What vendorize does NOT touch**: Repo variables and secrets. Those are GitHub metadata stored in each repo's Settings UI and are fully independent. The YAML workflows reference environment-specific values exclusively via `${{ vars.X }}` and `${{ secrets.X }}` references — so the same workflow file automatically uses the correct OpenStack allocation, runner, and credentials depending on which repo it is running in. This is the designed separation: code parity via vendorize + environment parity via per-repo Settings.

**Promotion from test to production** = run vendorize again targeting `MorphoCloudInstances`. No manual file edits, no search-and-replace. The per-repo variables and secrets in `MorphoCloudInstances` ensure the identical code talks to production infrastructure.

### Scope of This Test Suite

The E2E test suite targets `MorphoCloudInstancesTest` exclusively. It **must never run against `MorphoCloudInstances`** — doing so would create real issues in the production repo, provision instances on the production allocation, and send emails to real users. The harness is scoped to the test repo by configuration (`MORPHOCLOUDINSTANCESTEST_REPO` variable) with no mechanism to target production.

### The Promotion Gate

A successful E2E run (Group A + Group B, all three scenarios: individual, workshop, course) against `MorphoCloudInstancesTest` is a **hard prerequisite for any vendorization into `MorphoCloudInstances`**. No workflow code is promoted to production unless the full suite is green. This makes the test suite the formal quality gate between development and production, not just an optional validation step.

### Environment Drift: The Ongoing Risk

The greatest long-term risk in maintaining a parallel test environment is **environment drift**: the `MorphoCloudInstancesTest` repo variables/secrets diverge from `MorphoCloudInstances` in ways that cause a test to pass in staging but fail silently in production.

The primary mitigation is the **Identical Variable Name Contract**: every variable and secret referenced in the workflow YAML must exist under the exact same name in both repos. Only the *values* differ (e.g., different OpenStack allocation IDs); the *keys* are identical. If a variable is renamed in the workflow code, it must be updated in both repos’ Settings before the next vendorize + promote cycle. This contract is enforced by convention, not tooling — it must be documented in each PR that changes a variable reference.

A secondary mitigation: the E2E suite itself will fail if a required variable is missing or misnamed in `MorphoCloudInstancesTest`, surfacing the drift before promotion.

---

**TL;DR**: A new dedicated `MorphoCloudE2ETests` GitHub repo containing a Python test harness (`pytest` + `requests` + `google-auth` + `gmail-api`) and a manually-triggered GitHub Actions workflow. It exercises all three intake paths (individual, workshop, course) end-to-end — from Google Form submission through OpenStack instance provisioning and teardown — against `MorphoCloudInstancesTest` and the GAS test environment. The harness automates every step that is technically automatable; for the handful of genuinely manual steps (e.g. email verification click, credential email inspection) it pauses and prints precise tester instructions. Admin actions in the harness use a **GitHub App** (short-lived installation tokens) rather than a long-lived admin PAT.

---

## Phase 0 — One-Time Infrastructure Setup

*(Prerequisite tasks for first use; done once)*

### 0.1 Create Burner Accounts
- Create `morphocloud-test-burner@gmail.com` Google account
- Create `morphocloud-test-burner` GitHub account using the Gmail address
- Do NOT pre-add to the MorphoCloud org; the harness manages membership dynamically
- Configure the burner GitHub account: enable email notifications + privacy email (required for GitHub noreply forwarding used in the course path)

### 0.2 Google Cloud Service Account
- Create a GCP service account (under morphocloudportal's project or a dedicated one)
- Grant it read-only Sheets access to both: the Intake spreadsheet (`1XQkgz...`) and the Course Registration spreadsheet (`1sS9Or...`)
- Enable **Domain-Wide Delegation** and authorize the `gmail.readonly` scope so the service account can search the burner Gmail inbox via the Gmail API (replaces `imaplib` — the Gmail API is faster, less rate-limited, and not blocked by spam filters)
- **⚠️ DWD restriction**: Domain-Wide Delegation only works for **Google Workspace** accounts. It does **not** work for standard `@gmail.com` addresses. Since `morphocloud-test-burner@gmail.com` is a consumer Gmail account, inbox access must use an **OAuth2 Refresh Token** instead:
  1. Create a **Desktop OAuth 2.0 Client ID** in GCP (under the same project as the service account)
  2. Do a one-time manual `oauth2` authorization flow in a browser, logged in as `morphocloud-test-burner@gmail.com`, requesting `gmail.readonly` scope
  3. Extract the resulting `refresh_token` and store it as `TEST_GMAIL_OAUTH2_REFRESH_TOKEN` repo secret, alongside `TEST_GMAIL_OAUTH2_CLIENT_ID` and `TEST_GMAIL_OAUTH2_CLIENT_SECRET`
  4. At runtime, `gmail_helper.py` exchanges the refresh token for a short-lived access token via `POST https://oauth2.googleapis.com/token` before each Gmail API call
- The service account JSON (`TEST_GOOGLE_SERVICE_ACCOUNT_JSON`) is still used exclusively for **Google Sheets** read access (both spreadsheets) — no DWD needed for Sheets if those sheets are explicitly shared with the service account email
- Download service account JSON → stored as `TEST_GOOGLE_SERVICE_ACCOUNT_JSON` repo secret

### 0.3 GitHub App for Admin Actions
- Register a new **GitHub App** under the MorphoCloud org (or use the existing `morphocloud-workflow-app` if appropriate)
- Grant it: `issues: write` on `MorphoCloudInstancesTest`; **`organization members: write`** on the org (read alone is insufficient — write is required to remove the burner from the org during Layer 1 & 2 cleanup)
- **Install the App at the org level** (not just on `MorphoCloudInstancesTest` repo) — org-membership operations require an org-level installation; a repo-only installation cannot modify org membership
- Store `TEST_GITHUB_APP_ID` and `TEST_GITHUB_APP_PRIVATE_KEY` as repo secrets
- The harness exchanges these for **short-lived installation tokens** at runtime — no long-lived "god key" PAT in CI
- The burner's own fine-grained PAT (`TEST_BURNER_GITHUB_TOKEN`) remains for actions performed *as* the burner user (issue creation, comment, org invite acceptance)

### 0.4 Confirm Permanent Test Infrastructure in MorphoCloud Org
- `morphocloud-course-test` team exists with Read access to `MorphoCloudInstancesTest` ✅ (per existing notes)
- `morphocloud-workflow-app[bot]` is in `MORPHOCLOUD_GITHUB_ADMINS` of `MorphoCloudInstancesTest` ✅

### 0.4a `MorphoCloudInstancesTest` Repo Variables & Secrets (test-specific configuration)

**Important**: `nox -s vendorize` copies only files from `.github/` — it never reads or writes repo variables or secrets. Those are GitHub metadata stored exclusively in each repo’s Settings UI and are fully independent between `MorphoCloudInstancesTest` and `MorphoCloudInstances`. Code parity is guaranteed by vendorize; environment parity is maintained manually via these settings.

The workflows reference these values via `${{ vars.X }}` and `${{ secrets.X }}`, so the same YAML automatically "plugs in" to the correct environment in each repo. This is the designed separation — not a gap.

**Secrets that must be set in `MorphoCloudInstancesTest`** (and must never be shared with or copied from production):

| Secret | Test value points to | Production equivalent points to |
|--------|---------------------|----------------------------------|
| `OS_CLOUDS_YAML` | Test JetStream2 allocation (separate ACCESS project) | Production JetStream2 allocation |
| `MORPHOCLOUD_WORKFLOW_APP_PRIVATE_KEY` | Same GitHub App PEM (shared is fine — app is scoped per-repo-install) | Same |
| `STRING_ENCRYPTION_KEY` | Independent test key | Independent production key |
| `MAIL_USERNAME` / `MAIL_PASSWORD` | `morphocloudportal@gmail.com` App Password (same account is fine for test) | Same |

**Repo variables that must be set in `MorphoCloudInstancesTest`** (values differ from production):

| Variable | Test value | Production value |
|----------|-----------|------------------|
| `MORPHOCLOUD_OS_CLOUD` | Name of the test cloud entry in `OS_CLOUDS_YAML` | Name of the prod cloud entry |
| `MORPHOCLOUD_EMAIL_LOOKUP_URL` | Stable intake.gs deployment URL (same URL — both point to same GAS deployment) | Same |
| `MORPHOCLOUD_WORKSHOP_EMAIL_LOOKUP_URL` | Same | Same |
| `MORPHOCLOUD_USERS_TEAM_SLUG` | `MorphoCloudUsers` | `MorphoCloudUsers` |
| `MORPHOCLOUD_GITHUB_ADMINS` | `muratmaga,morphocloud-workflow-app[bot]` (bot added for automated `/approve` in tests) | `muratmaga` (no bot) |
| `MORPHOCLOUD_GITHUB_ADMIN_EMAILS` | `maga@uw.edu` | `maga@uw.edu` |
| `MORPHOCLOUD_WORKSHOP_ORGANIZERS_TEAM_SLUG` | `MorphoCloudWorkshopOrganizers` | `MorphoCloudWorkshopOrganizers` |
| `MORPHOCLOUD_WORKFLOW_APP_ID` | Same App ID | Same |

The E2E harness in `MorphoCloudE2ETests` does not set, read, or depend on any of these values directly — it only targets `MorphoCloudInstancesTest` by repo name and the workflows in that repo consume their own variables/secrets autonomously.

### 0.5 New Repo: `MorphoCloudE2ETests`

Repo secrets:

| Secret | Purpose |
|--------|----------|
| `TEST_BURNER_GITHUB_TOKEN` | Fine-grained PAT for `morphocloud-test-burner` (issues:write, org membership accept) |
| `TEST_GITHUB_APP_ID` | GitHub App ID for admin actions (replaces long-lived admin PAT) |
| `TEST_GITHUB_APP_PRIVATE_KEY` | GitHub App private key PEM; harness generates installation tokens at runtime |
| `TEST_GOOGLE_SERVICE_ACCOUNT_JSON` | GCP service account JSON for **Sheets read only** (shared with service account email) |
| `TEST_GMAIL_OAUTH2_REFRESH_TOKEN` | OAuth2 refresh token for Gmail API inbox search on `morphocloud-test-burner@gmail.com` |
| `TEST_GMAIL_OAUTH2_CLIENT_ID` | GCP Desktop OAuth2 client ID (paired with refresh token) |
| `TEST_GMAIL_OAUTH2_CLIENT_SECRET` | GCP Desktop OAuth2 client secret (paired with refresh token) |

Repo variables: `INTAKE_WEBAPP_URL`, `INTAKE_SPREADSHEET_ID`, `COURSE_REG_WEBAPP_URL`, `COURSE_REG_SPREADSHEET_ID`, `BURNER_GITHUB_USERNAME`, `BURNER_GMAIL`, `MORPHOCLOUDINSTANCESTEST_REPO`, `INTAKE_TEMPLATES_URL`, `COURSE_REG_TEMPLATES_URL`, `MORPHOCLOUD_SENDER_EMAIL`

### 0.6 Email Domain + Template Config (update when email migration is executed)

- Set `INTAKE_TEMPLATES_URL` = `https://raw.githubusercontent.com/muratmaga/MorphoCloudPortalContent/main/templates/intake.json`
- Set `COURSE_REG_TEMPLATES_URL` = `https://raw.githubusercontent.com/muratmaga/MorphoCloudPortalContent/main/templates/course-registration.json`
- Set `MORPHOCLOUD_SENDER_EMAIL` = current sender address (`morphocloudportal@gmail.com` before email domain migration; `portal@morphocloud.org` after). `gmail_helper.py` uses this when filtering inbox search results by sender.
- **After email domain migration**: verify the `portal@morphocloud.org` "Send mail as" alias is configured in `morphocloudportal@gmail.com` Gmail settings (required for GAS to send via `GmailApp` as the new address), then update `MORPHOCLOUD_SENDER_EMAIL` to `portal@morphocloud.org`

---

## Phase 1 — Repo Structure

```
MorphoCloudE2ETests/
├── README.md                         (setup guide + how to run)
├── pyproject.toml                    (deps: pytest, requests, google-auth, google-api-python-client, PyGithub)
├── conftest.py                       (shared fixtures: app installation token, Gmail API client, GH client)
├── helpers/
│   ├── gmail_helper.py               (Gmail API search via OAuth2 refresh token; path-specific timeouts: 2 min for direct-send, 5 min for GitHub-forwarded course emails)
│   ├── google_sheets_helper.py       (find row by email + recency; eventual-consistency retry)
│   ├── github_helper.py              (create issue, post comment, read labels, accept invite w/ backoff)
│   ├── gas_helper.py                 (HTTP POST form, call web app endpoints; 3-retry on 5xx for GAS cold-start)
│   └── portal_content_helper.py      (fetches intake.json + course-registration.json from MorphoCloudPortalContent at test-run start; pre-flight URL validation; exposes live template values as expected strings for all GAS-sent email assertions)
├── scenarios/
│   ├── individual/
│   │   ├── test_individual_intake.py      (Group A)
│   │   └── test_individual_lifecycle.py   (Group B)
│   ├── workshop/
│   │   ├── test_workshop_intake.py
│   │   └── test_workshop_lifecycle.py
│   └── course/
│       ├── test_course_intake.py
│       └── test_course_lifecycle.py
└── .github/workflows/
    └── test-e2e.yml                  (workflow_dispatch; concurrency group prevents parallel runs)
```

---

## Phase 2 — Workflow Inputs (`test-e2e.yml`)

`workflow_dispatch` inputs:

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `group` | choice: A / B / both | `A` | Group A = intake only; Group B = full lifecycle |
| `scenarios` | multi: individual / workshop / course | all | Which intake paths to test |
| `skip_cleanup` | boolean | `false` | Leave issues/instances open for debugging |

Group A runs on a standard GitHub-hosted runner (no OpenStack needed).
Group B requires the self-hosted runner in `MorphoCloudInstancesTest`.

The workflow **must** include a concurrency group to prevent two simultaneous runs from fighting over the shared burner account, org membership, and inbox:

```yaml
concurrency:
  group: e2e-tests
  cancel-in-progress: false  # do NOT cancel — let the current run finish cleanly
```

> Using `cancel-in-progress: false` rather than `true` is intentional: cancelling mid-run would skip cleanup, leaving orphan issues/instances and the burner still in the org.

---

## Phase 3 — Test Scenario Steps

**Key principle**: The harness *initiates* actions via GitHub API and GAS web app calls, then *polls* `MorphoCloudInstancesTest` issue state and the burner inbox. The actual provisioning happens inside `MorphoCloudInstancesTest`'s workflows — the harness never needs OpenStack access directly.

**Email assertion strategy**: GAS-sent emails (verification emails, instructor approval/rejection emails) have template-driven subjects and bodies — `portal_content_helper.py` fetches both template JSONs from `MorphoCloudPortalContent` at test-run start and surfaces their live values as the expected strings in all inbox assertions. No email subject or body string is ever hardcoded in the test code. GitHub Actions-sent credential emails (sent after `/create`) are not yet template-driven and may match against fixed structural patterns (e.g., presence of an IP address and passphrase).

### Scenario: Individual — Group A (Intake Only)

| # | Step | Auto? |
|---|------|-------|
| 1 | **Pre-test guard**: remove burner from MorphoCloud org if currently a member (idempotent; 404 = already clean). This is a *correctness precondition* — without it, a leftover membership from a crashed previous run would make the "user becomes a member after verification" assertion meaningless. | ✅ GitHub API |
| 2 | HTTP POST to Intake Form `formResponse` endpoint | ✅ requests |
| 3 | Sleep ~10s for `onFormSubmit` to execute | ✅ sleep |
| 4 | Poll Intake sheet for a row matching `burner_gmail` with `Timestamp` within last 2 minutes (retry up to 30s to handle Sheets eventual consistency) | ✅ Sheets API |
| 5 | `GET INTAKE_WEBAPP_URL?token=<token>&row=<row>` to verify email | ✅ requests |
| 6 | Assert sheet row: `email_verified = VERIFIED`, `github_invite_status = INVITED` | ✅ Sheets API |
| 7 | Accept GitHub org invite via burner PAT (`PATCH /user/memberships/orgs/MorphoCloud`) with **exponential backoff** (GAS invite dispatch and GitHub invite propagation have a few-second lag; 404 on first attempt is expected) | ✅ GitHub API |
| 8 | Assert burner is member of `MorphoCloudUsers` team | ✅ GitHub API |

### Scenario: Individual — Group B (adds to Group A)

| # | Step | Auto? |
|---|------|-------|
| 9 | Create `instance-request` issue as burner (flavor: `g3.large` — most consistently available in the test allocation) | ✅ GitHub API |
| 10 | Poll issue labels for `flavor:*` + `expiration:*` (up to 3 min) | ✅ poll |
| 11 | Post `/create` comment as burner | ✅ GitHub API |
| 12 | Poll for `status:running` label (timeout: 20 min) | ✅ poll |
| 13 | Assert progress comment exists on issue | ✅ GitHub API |
| 14 | Search burner Gmail inbox via **Gmail API** for message matching subject pattern `MorphoCloud` + `credentials` sent after test start time (faster and more reliable than IMAP; not affected by spam filtering or rate limits) | ✅ Gmail API |
| 15 | Assert email body contains IP address and passphrase | ✅ regex |
| 16 | Post `/shelve`; poll for `status:shelved` | ✅ |
| 17 | Post `/unshelve`; poll for `status:running` | ✅ |
| 18 | Post `/delete_all`; assert issue closed | ✅ |
| 19 | Cleanup: remove burner from org | ✅ |

### Scenario: Workshop — Group A
Same 8 steps as Individual Group A but submits `workshop` intake type. Additionally:
- Assert burner is member of **both** `MorphoCloudUsers` AND `MorphoCloudWorkshopOrganizers`

### Scenario: Workshop — Group B (adds to Group A)

| # | Step | Auto? |
|---|------|-------|
| 9 | Create `workshop-request` parent issue as burner | ✅ |
| 10 | Poll for sub-issues (created by `on-request-opened.yml`) | ✅ poll |
| 11 | Post `/approve` as admin (using `TEST_ADMIN_GITHUB_TOKEN`) | ✅ GitHub API |
| 12 | Post `/create` as burner (organizer) | ✅ |
| 13 | Poll sub-issues for `status:running` | ✅ poll |
| 14 | Search burner Gmail inbox via Gmail API for credential email (organizer receives all participant creds) | ✅ Gmail API |
| 15 | Assert credential email body | ✅ regex |
| 16 | Post `/delete_all` on parent; assert sub-issues closed | ✅ |

### Scenario: Course — Group A (Student Intake + Instructor Approval)

**Student side** (uses permanent `morphocloud-course-test` team):

| # | Step | Auto? |
|---|------|-------|
| 1 | Remove burner from org | ✅ |
| 2 | POST course intake form with `course_team_slug = morphocloud-course-test` | ✅ |
| 3–8 | Same as Individual Group A steps 3–8 | ✅ |
| 9 | Assert burner is member of `morphocloud-course-test` only (not `MorphoCloudUsers`) | ✅ |

**Instructor/admin approval side**:

| # | Step | Auto? |
|---|------|-------|
| A | POST course registration form (burner as instructor) | ✅ |
| B | Read `approval_token` from Course Reg sheet via service account | ✅ Sheets API |
| C | `GET COURSE_REG_WEBAPP_URL?action=approve&token=<token>` (Step 1 — confirmation page, no side effects) | ✅ |
| D | `GET COURSE_REG_WEBAPP_URL?action=do-approve&token=<token>` (Step 2 — actual approval) | ✅ |
| E | Assert GitHub team created with expected name + `duration:Nd` in description | ✅ GitHub API |
| F | Search burner Gmail inbox via Gmail API for instructor approval email — subject matched against `instructor_approved_subject` from live `COURSE_REG_TEMPLATES_URL`; assert body contains the pre-filled issue URL | ✅ Gmail API |

> **Fallback if service account unavailable**: Steps B–D are replaced by printing the approval URL and pausing with `input("Press Enter after approving in browser...")`. In CI (`CI=true`), the step is marked `SKIPPED`.

### Scenario: Course — Group B (adds to Course Group A student side)

| # | Step | Auto? |
|---|------|-------|
| 10 | Create `course-instance-request` issue as burner using pre-filled URL | ✅ |
| 11 | Poll for labels from `on-request-opened.yml` | ✅ |
| 12 | Post `/create` as burner | ✅ |
| 13 | Poll for `status:running` (timeout: 20 min) | ✅ |
| 14 | Search burner Gmail inbox via Gmail API for credential email (sent to GitHub noreply, forwarded to burner Gmail) | ✅ Gmail API |
| 15 | Assert credential email body | ✅ regex |
| 16 | Post `/delete_all`; cleanup | ✅ |

---

## Phase 4 — Manual Gate Handling

For any step that can't be automated, the harness prints a structured instruction block:

```
╔══════════════════════════════════════════════════════════════╗
║  MANUAL STEP REQUIRED                                        ║
║  What to do:  Open this URL in an incognito/private window   ║
║  URL:  https://...                                           ║
║  What you should see:  Approval confirmation page            ║
║  Then:  Click "Confirm Approval"                             ║
║  When done:  Press Enter to continue                         ║
╚══════════════════════════════════════════════════════════════╝
```

In CI mode (`CI=true`, non-interactive): marks the step `SKIPPED` — not a failure — and continues.

---

## Phase 5 — Cleanup Strategy

Membership cleanup is handled at **three layers** for different reasons:

### Layer 1 — Pre-test guard (start of every scenario, mandatory)

Before submitting any form, the harness runs the following checks unconditionally:

1. **Remove burner org membership** if currently a member (idempotent; 404 = already clean). This is a correctness precondition — a leftover membership from a crashed run would make “assert burner becomes a member after verification” a vacuous assertion.

2. **Template JSON pre-flight**: `GET` both `INTAKE_TEMPLATES_URL` and `COURSE_REG_TEMPLATES_URL`, assert HTTP 200 and parseable JSON. If either fails, abort immediately with: “Template JSON unreachable — GAS would send blank emails; tests cannot assert email content.” This surfaces misconfiguration and `MorphoCloudPortalContent` outages before any form is submitted.

3. **Detect and drain orphaned OpenStack instances** from previous failed Group B runs. A test that crashes after OpenStack provisioning but before `/delete_all` leaves a live instance consuming JetStream2 quota. The guard:
   - Queries `MorphoCloudInstancesTest` for any open issues authored by the burner account that carry the `test-harness` label
   - For each found: posts `/delete_all` as the admin app token and polls for the issue to close (timeout: 10 min)
   - Only proceeds to the new test scenario once no open burner issues remain
   - If the drain times out, the test is **aborted** (not failed) with a clear message: “Orphaned instance detected — manual cleanup required before retrying”

This prevents quota exhaustion from cascading across multiple failed runs.

### Layer 2 — Post-test teardown (end of every scenario, best-effort)

After each scenario (pass *or* fail), the harness runs teardown. Failures in teardown are logged as warnings, not test failures — the pre-test guard will recover on the next run:
1. Delete any test issues created during the run (by `test-harness` label, using admin app token)
2. Remove burner from MorphoCloud org
3. If course registration test created a new GitHub team (not the permanent `morphocloud-course-test`): delete it
4. No GAS sheet cleanup — rows accumulate but are found by `burner_gmail` + `Timestamp > test_start_time` matching

`--skip_cleanup` flag skips Layer 2 only. Layer 1 always runs — skipping it would compromise test validity, not just hygiene.

---

## Phase 6 — Reporting

- **JUnit XML** → uploaded as GitHub Actions artifact at end of run
- **Progressive step logging** → each test step writes a timestamped line to `$GITHUB_STEP_SUMMARY` *as it completes*, not only at the end. For a Group B run that can take 20+ minutes, this is critical: if the test fails at minute 18, the summary immediately shows whether the failure was "OpenStack provisioned but label never appeared" vs "label appeared but credential email never arrived" vs "email arrived but regex assertion failed"
- **`::error::` annotations** on failed assertions with full context: HTTP response body, issue URL, Gmail message ID, label state at time of failure- **Failure artifact**: any `5xx` response body or unexpected JSON payload encountered during a run is appended to `failure_dump.json` (keyed by scenario + step). The file is always uploaded as a GitHub Actions artifact at end of run (even on success, where it will be empty), making post-mortems on Group B failures possible without re-running — raw GAS error messages and OpenStack API responses are captured verbatim.- **Structured step log format** (written by a shared `log_step()` helper called from all scenario files):
  ```
  [12:34:01 +00s]  ✅  individual/group_a  step 4  Found sheet row 47 for burner (Timestamp: 2026-03-25T12:33:58)
  [12:34:03 +02s]  ✅  individual/group_a  step 5  GAS verification endpoint returned 200
  [12:34:05 +04s]  ❌  individual/group_a  step 6  email_verified = PENDING (expected VERIFIED) — row 47
  ```
- No comments posted to any existing GitHub issue — all output lives in the workflow run summary and artifact

---

## Relevant Repos & Files

| File | Role |
|------|------|
| `MorphoCloudAppsScripts/intake/intake.gs` | Email verification + lookup endpoint under test |
| `MorphoCloudAppsScripts/course-registration/course-registration.gs` | Two-step admin approval under test |
| `MorphoCloudWorkflow/.github/workflows/on-request-opened.yml` | Issue router exercised by lifecycle tests |
| `MorphoCloudWorkflow/.github/workflows/create-instance.yml` | Individual `/create` handler |
| `MorphoCloudWorkflow/.github/workflows/create-course-instance.yml` | Course `/create` handler |
| `MorphoCloudWorkflow/.github/workflows/create-workshop.yml` | Workshop `/create` handler |
| `MorphoCloudInstancesTest` | Target repo where test issues live and workflows execute |

---

## Verification Steps

1. `pytest --co` in `MorphoCloudE2ETests` — all test IDs resolve, no import errors
2. `pytest -m "group_a and individual"` — completes in < 2 min with no OpenStack usage; asserts Gmail API and Sheets access work
3. `pytest -m "group_a"` — all three intake paths pass (individual, workshop, course)
4. `pytest -m "group_b and individual"` — on self-hosted runner; `status:running` label appears within 20-min timeout; credential email arrives at burner Gmail
5. Manual check: verify that `skip_cleanup=false` leaves no residual org memberships, open issues, or orphan OpenStack instances

---

## Further Considerations

1. **Runner requirements for Group B**: OpenStack provisioning happens *inside* `MorphoCloudInstancesTest` workflows (triggered by issue comments). The test harness itself only needs a standard GitHub-hosted runner — it submits actions via GitHub API and polls issue state. No OpenStack access is needed by the test harness directly.

2. **GitHub noreply + Gmail API (Course path)**: GitHub noreply (`{id}+{username}@users.noreply.github.com`) only forwards to the burner Gmail if the burner GitHub account has email notifications enabled and privacy email enabled. Configure once in Phase 0 and document in README. **Forwarding delay**: there is typically a 30–90 second lag between a GitHub Action finishing and the forwarded email appearing in the Gmail inbox. `gmail_helper.py` must use a **5-minute poll timeout** specifically for course credential emails (forwarded path), vs 2 minutes for individual/workshop emails (sent directly by the workflow).

3. **Sheets eventual consistency**: Google Sheets API can return stale data for a few seconds after a GAS `onFormSubmit` write. The `google_sheets_helper.py` must poll retry (up to ~30s) and match on `burner_gmail` + `Timestamp > test_start_time` rather than assuming the last row is the one just written.

4. **GitHub invite propagation delay**: After GAS dispatches the org invite, the GitHub API `GET /user/memberships/orgs/MorphoCloud` may return 404 for a few seconds. `github_helper.py` must implement retry with exponential backoff (e.g., 2s → 4s → 8s → 16s, up to ~60s total) for the invite-acceptance step specifically.

5. **GAS cold-start and execution limits**: GAS has a 6-minute execution limit and can return `502` or `504` when the script engine is cold or a concurrent execution is already in progress ("Task already in progress"). All `POST` and `GET` calls in `gas_helper.py` must include a **3-retry loop with a fixed 5-second wait** between attempts before raising. This is distinct from the GitHub invite backoff — GAS retries are fixed-interval (cold-start recovery), not exponential. GAS can also return `429 Too Many Requests` under rapid burst traffic; the Chunk 2 stress test will determine whether this needs a separate back-off path or whether the existing fixed-interval loop is sufficient. Additionally, the `loadTemplates()` function in both `intake.gs` and `course-registration.gs` calls `UrlFetchApp.fetch()` to retrieve the template JSON on a cache miss — this fetch runs inside the same 6-minute execution budget, before `MailApp.sendEmail()`. The GAS `CacheService` cache is internal to the script execution environment and is not warmed by the Layer 1 pre-flight check (which hits the raw GitHub URL from outside GAS); a true cache warm requires a prior GAS execution that called `loadTemplates()` successfully. If a prior `onFormSubmit` trigger is still running when the Sheets API is queried, the sheet row may be locked for writing; `google_sheets_helper.py` must treat a `503 Service Unavailable` or `Resource Busy` response from the Sheets API as a transient error and retry with the same 30-second patience window already applied for eventual consistency.

6. **GitHub App: JWT → Installation ID → Installation Token**: The `conftest.py` GitHub App authentication flow is three steps, not two. The App ID + Private Key generate a **JWT** (valid 10 min). That JWT is used to call `GET /app/installations` to find the **Installation ID** for the `MorphoCloud` org. The Installation ID is then exchanged for a short-lived **installation token** via `POST /app/installations/{id}/access_tokens`. Only that final token can be used in API calls. The JWT alone cannot call org or repo endpoints. `conftest.py` must implement all three steps and cache the installation token (reusing it until expiry rather than regenerating on every helper call).

7. **Test isolation vs. shared org state**: Tag all test-generated issues with a `test-harness` label so they're visually distinct from real user issues. The cleanup step specifically targets issues carrying that label to avoid accidentally touching real user issues.

8. **GAS `test_mode` toggle (optional future improvement)**: Adding a `?test_mode=true` query parameter to the GAS `doGet` handler that returns the `verification_token` directly in the HTTP response body would eliminate the Sheets API polling entirely for Group A tests, making them near-instantaneous. This is a low-risk GAS change since the token has no value without the correct `row` parameter, but it does require a GAS code change and redeployment — defer to a later iteration.

9. **No ephemeral test repos**: The "shadow repo" pattern (creating a temporary repo per Group B run) was considered and rejected. Mirroring the full `.github/workflows` stack into a temp repo would test a snapshot, not the live code. Strict `test-harness` labeling + the concurrency group provides sufficient isolation within `MorphoCloudInstancesTest`.

10. **PAT rotation testing**: Out of scope for this suite. The PAT in GAS Script Properties is a live credential — rotation is a separate operational procedure tracked in morphocloud-requirements.md.

11. **Email delivery as a tester-assisted step**: Verifying that credential emails are *readable and correct-looking* (not just machine-assert-able) is kept as an optional human spot-check. The harness asserts structure (IP address regex, passphrase present) but does not render HTML or validate formatting. Tester can inspect the raw email from burner Gmail if the visual format needs to be verified.

12. **Template externalization and `MorphoCloudPortalContent`**: `intake.gs` and `course-registration.gs` (in the `externalize-templates` branch, not yet merged to main) load all user-facing text (email subjects, bodies, HTML page messages) from JSON files in `muratmaga/MorphoCloudPortalContent` at runtime, cached for 5 minutes via GAS `CacheService`. Three implications for the test harness: **(a) No hardcoded email strings** — `portal_content_helper.py` fetches the live template values at test-run start and uses them as expected assertion values, so tests remain valid regardless of future template edits; **(b) New silent failure mode** — if the GitHub raw URL for a template is unreachable, GAS falls back to an empty object and sends emails with blank subject and body; the Layer 1 pre-flight check surfaces this before any form is submitted; **(c) Cache lag** — if templates are edited between the pre-flight fetch and the actual form submission, the GAS-cached version (up to 5 min old) may differ from what the harness expects; never edit `MorphoCloudPortalContent` templates mid-run.

13. **Email domain migration (`portal@morphocloud.org`)**: When the email migration in `email-migration.md` is executed, two changes affect the harness: **(a)** `MORPHOCLOUD_SENDER_EMAIL` must be updated from `morphocloudportal@gmail.com` to `portal@morphocloud.org` so `gmail_helper.py` inbox searches filter by the correct sender; **(b)** The GAS `MailApp` → `GmailApp` switch (Part 2 of the migration) requires the `portal@morphocloud.org` "Send mail as" Gmail alias to be configured and verified before the updated scripts are deployed — add this alias check to the Phase 0 §0.6 checklist. No code changes are needed in the test harness itself beyond updating the `MORPHOCLOUD_SENDER_EMAIL` variable.

---

## Implementation Roadmap

Each chunk ends in a runnable, verifiable state. No chunk leaves the repo partially working. Chunks 1–4 require only GitHub-hosted runners; Chunks 5–6 require the `MorphoCloudInstancesTest` self-hosted runner.

### Chunk 1 — Repo skeleton + CI plumbing

- Create `MorphoCloudE2ETests` repo with `pyproject.toml`, stub `conftest.py`, and `test-e2e.yml`
- `workflow_dispatch` inputs (`group`, `scenarios`, `skip_cleanup`) and concurrency group in place
- One trivial always-passing smoke test

**Done when**: `workflow_dispatch` run is green; all three inputs are functional; concurrency group prevents parallel runs.

---

### Chunk 2 — Helper modules with live integration tests

- `github_helper.py` — GitHub App JWT → Installation ID → installation token; burner PAT operations; exponential-backoff invite acceptance
- `google_sheets_helper.py` — service account auth; row lookup by email + recency; eventual-consistency retry; 503/Resource Busy handling
- `gmail_helper.py` — OAuth2 refresh token exchange; Gmail API inbox search; path-specific timeouts (2 min direct, 5 min forwarded)
- `gas_helper.py` — form `POST`; web app `GET`; 3-retry fixed-interval loop for GAS cold-start; `429 Too Many Requests` handler
- `portal_content_helper.py` — fetches both template JSONs from `INTAKE_TEMPLATES_URL` and `COURSE_REG_TEMPLATES_URL`; validates HTTP 200 + parseable JSON; exposes live values as assertion targets
- Lightweight integration test per helper: authenticate + read/call something real (does not mutate state); template pre-flight test asserts both URLs return 200 + valid JSON with expected keys (`verify_email_subject`, `instructor_approved_subject`, `contact_email`, etc.)
- **GAS cold-start stress test**: call the web app endpoint 4–5 times in rapid succession and assert every call either succeeds or retries cleanly within the fixed-interval loop. Use results to tune the retry interval (5 s default) and confirm whether `429` responses need a separate back-off path distinct from `502`/`504`.

**Done when**: `pytest tests/helpers/` — all four helpers authenticate and communicate with live services using stored secrets; GAS stress test completes without unhandled exceptions regardless of engine warmth.

---

### Chunk 3 — Individual Group A (intake only)

- Full individual intake scenario: form POST → Sheets poll → email verification → org invite accept → `MorphoCloudUsers` membership assert
- Layer 1 pre-test guard: idempotent burner org-membership removal before scenario starts

**Done when**: `pytest -m "group_a and individual"` completes in under 2 minutes; burner ends up in the correct team; re-running immediately is fully idempotent.

---

### Chunk 4 — Workshop + Course Group A; Layer 2 teardown

- Workshop Group A: same 8-step flow plus `MorphoCloudWorkshopOrganizers` membership assertion
- Course Group A: student intake + two-step instructor approval (Sheets token read → GAS approve → GAS do-approve) + team creation assert + instructor approval email check
- Layer 2 post-test teardown wired into all three scenarios (best-effort; failures logged as warnings, not test failures)

**Done when**: `pytest -m "group_a"` — all three intake paths green; teardown leaves no residual memberships or test issues.

---

### Chunk 5 — Individual Group B (full instance lifecycle)

- Create `instance-request` issue → poll labels → `/create` → poll `status:running` → credential email assert (Gmail API, 2-min timeout) → `/shelve` → `/unshelve` → `/delete_all` → assert issue closed
- Layer 1 orphan drain: query for open `test-harness`-labeled burner issues, post `/delete_all`, poll close (10-min timeout → abort with clear message)

**Done when**: `pytest -m "group_b and individual"` — a real OpenStack instance is provisioned and fully deleted; credential email regex passes; no residual issues, instances, or org memberships after run.

---

### Chunk 6 — Workshop + Course Group B

- Workshop Group B: parent issue → poll sub-issues → `/approve` (admin app token) → `/create` (burner organizer) → poll `status:running` → credential email → `/delete_all`
- Course Group B: `course-instance-request` issue from pre-filled URL → `/create` → poll `status:running` → GitHub noreply → Gmail forward (5-min timeout) → credential email assert → `/delete_all`

**Done when**: `pytest -m "group_b"` green; full `pytest` (group A + B, all three scenarios) green end-to-end.

---

### Chunk 7 — Reporting polish + README

- `log_step()` helper writing timestamped lines to `$GITHUB_STEP_SUMMARY` as each step completes (not only at end)
- `::error::` annotations on assertion failures with full context (HTTP body, issue URL, Gmail message ID, label state)
- **Failure artifact**: any `5xx` response body or unexpected JSON payload encountered during a run is appended to `failure_dump.json` (keyed by scenario + step). The file is always uploaded as a GitHub Actions artifact at end of run (even on success, where it will be empty), making post-mortems on Group B failures possible without re-running — raw GAS error messages and OpenStack API responses are captured verbatim.
- JUnit XML artifact upload
- README covering: Phase 0 setup steps, how to run each chunk, how to interpret `$GITHUB_STEP_SUMMARY` failures, and how to read `failure_dump.json`

**Done when**: a deliberately broken run (e.g. wrong spreadsheet ID) produces a summary that pinpoints the exact failing step with context — not just a bare Python traceback; `failure_dump.json` artifact contains the raw `5xx` response body from that failure.
