# `mc-e2e-bot` + harness credentials — setup runbook

One-time setup that makes the harness able to run green. Most of this is the dedicated
test identity and the inbox; the rest is secrets. Re-runnable / idempotent where noted.

> **The bot account must use a normal user PAT, not a GitHub App installation token.**
> The harness opens issues *as the bot* so `issue.user.login == mc-e2e-bot`, which is
> what routes the credential email and sets the per-user authorization subject. An App
> token would make the app the issue creator (breaking both) and type the bot's comments
> as `Bot`, which `on-admin-mention` deliberately ignores.

## 1. Create the test GitHub account
- New GitHub user, e.g. **`mc-e2e-bot`** (handle is configurable via `E2E_BOT_USERNAME`).
- Its **primary email** must be an inbox you can poll over IMAP (see step 4).

## 2. Org teams + repo access (org owner does this)
- Add `mc-e2e-bot` to **`MorphoCloudUsers`** — gates `/create` on individual issues.
- Add `mc-e2e-bot` to **`MorphoCloudWorkshopOrganizers`** — gates workshop `/create`.
- Add `mc-e2e-bot` as a **Triage** (or Write) collaborator on **`MorphoCloud/Test-Instances`**
  so it can open issues + comment and pass the `github/command` permission gate.

## 3. Register the bot's email in the intake lookup
The credential-email path looks the bot up by GitHub username via `join.morphocloud.org/lookup`.
Make `?action=lookup&github_username=mc-e2e-bot` resolve to the test inbox:
- Easiest: have the bot complete `https://join.morphocloud.org` (ORCID → verify email).
- Or add a row to the intake sheet: `github_username=mc-e2e-bot`, `email=<inbox>`,
  `email_verified=TRUE`.
- **Verify:** `curl -s -H "X-Api-Key: $MORPHOCLOUD_LOOKUP_API_KEY" \
  "https://join.morphocloud.org/lookup?github_username=mc-e2e-bot"` returns the inbox.

## 4. Test inbox (IMAP)
- A Gmail with an **app password**, or a `+e2e` alias on an existing mailbox.
- **Verify before Milestone 2:** send a probe email to the inbox and confirm the harness
  reads it: `python -c "from e2e.mailbox import Mailbox; import datetime as d; \
  print([m.subject for m in Mailbox().__enter__()._fetch_recent(d.datetime.now(d.timezone.utc))])"`

## 5. Read-only OpenStack credential (for direct-state assertions)
- Create a **read-only application credential** for `BIO240357_IU` and add a `clouds.yaml`
  entry named to match `E2E_OS_CLOUD` (e.g. `BIO240357_IU`). The harness only ever reads.

## 6. `e2e-verify-instance.yml` on Test-Instances (in-guest readiness probe)
- Commit a test-only `workflow_dispatch` workflow that `runs-on: self-hosted`, takes an
  `issue_number`, finds the instance IP, SSHes in with the **runner's own key**, and
  asserts: `mountpoint -q /media/volume/MyData`, `/home/exouser` → volume symlink,
  `test -d /media/volume/MyData/Slicer`. Exit non-zero on any failure.
- It lives only in Test-Instances. Confirm `nox -s vendorize` does not prune it; if it
  does, add it to the noxfile exclude list. (Used by Milestone 2/3 readiness asserts.)

## 7. Environment / secrets

Local dev: a git-ignored `MorphoCloudE2ETests/.env`. CI: secrets on the
`MorphoCloud/MorphoCloudE2ETests` repo (set via `gh secret set <NAME> --repo …`).

> **Secret-by-file (local):** any `E2E_*` secret can be given as `<NAME>_FILE=<path>`
> instead of the value, so `.env` holds only a path (e.g.
> `E2E_BOT_PAT_FILE=~/.ssh/GH-morphocloud-e2e-token`). Applies to BOT/ADMIN/EXTRA/IMAP.

| Var | What |
|-----|------|
| `E2E_BOT_PAT` | **Required.** `amm554` **fine-grained** PAT, resource owner `MorphoCloud`, **Test-Instances only**, **Issues: read/write** (+ Metadata: read). A user PAT, NOT a GitHub App token (the issue creator must be the bot, and `on-admin-mention` ignores Bot-type comments). |
| `E2E_BOT_USERNAME` | bot handle (default `amm554`). |
| `E2E_ADMIN_PAT` | **Required in CI** (locally it falls back to `gh auth token`). A `muratmaga` (a `morphocloud-admins` member) **classic** PAT with **`repo` + `workflow`** — `workflow` is needed because Stage 0 pushes `.github/workflows/` changes to Test-Instances; also drives `/approve` + `workflow_dispatch`. |
| `E2E_IMAP_HOST` / `E2E_IMAP_PORT` / `E2E_IMAP_USER` / `E2E_IMAP_PASSWORD` | *(optional)* inbox IMAP → enables the real credential-email asserts. For `amm554` that inbox is `slicermorph@gmail.com` (already in the join lookup). Unset → email asserts skip. |
| `E2E_OS_CLOUD` + `E2E_OS_CLOUDS_YAML` | *(optional)* clouds.yaml entry (e.g. `BIO240357_IU`) + the read-only clouds.yaml contents (CI writes it to `~/.config/openstack/`). Enables direct OpenStack state asserts. Unset → those skip. |
| `E2E_FLAVOR` | smallest flavor whose cloud-init completes (Milestone 0; default `m3.tiny`). |
| `E2E_EXTRA_USER_PAT` | *(optional)* 2nd account NOT in `MorphoCloudUsers`, for the non-member negative. |
| `E2E_MWF_DIR` / `E2E_TEST_INSTANCES_DIR` | *(local only)* checkouts for Stage 0 vendorize. CI checks them out and sets these automatically. |

**CI (GitHub Actions):** `.github/workflows/e2e.yml` is `workflow_dispatch` (Actions → E2E
→ Run workflow → pick a `suite`). It needs `E2E_BOT_PAT` + `E2E_ADMIN_PAT`; IMAP/OS
secrets are optional. Results render in the run's job-summary report.

## 8. Smoke check the plumbing
```bash
cd MorphoCloudE2ETests
pip install -e .
python -c "from e2e import config; print('flavor', config.E2E_FLAVOR, 'repo', config.REPO)"
nox -s e2e-sweep            # must report it cleans nothing on a clean allocation
```
