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
Local dev: put these in `MorphoCloudE2ETests/.env` (git-ignored). CI: Actions repo secrets.

| Var | What |
|-----|------|
| `E2E_BOT_PAT` | `mc-e2e-bot` user PAT — scopes **`repo` + `read:org`** only. Rotate regularly. |
| `E2E_BOT_USERNAME` | bot handle (default `mc-e2e-bot`). |
| `E2E_ADMIN_PAT` | admin (morphocloud-admins) PAT for `/approve` + `workflow_dispatch`. Local dev may omit it — the harness falls back to `gh auth token`. |
| `E2E_IMAP_HOST` / `E2E_IMAP_PORT` / `E2E_IMAP_USER` / `E2E_IMAP_PASSWORD` | inbox IMAP. |
| `E2E_OS_CLOUD` | clouds.yaml entry (e.g. `BIO240357_IU`). Unset → OS assertions skip. |
| `E2E_FLAVOR` | smallest flavor whose cloud-init completes (set by Milestone 0; default `m3.tiny`). |
| `E2E_EXTRA_USER_PAT` | *(optional)* 2nd account NOT in `MorphoCloudUsers`, for the non-member negative. |
| `E2E_MWF_DIR` / `E2E_TEST_INSTANCES_DIR` | local checkouts for Stage 0 vendorize. |

## 8. Smoke check the plumbing
```bash
cd MorphoCloudE2ETests
pip install -e .
python -c "from e2e import config; print('flavor', config.E2E_FLAVOR, 'repo', config.REPO)"
nox -s e2e-sweep            # must report it cleans nothing on a clean allocation
```
