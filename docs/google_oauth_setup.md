# Google OAuth for Workflow 6 (PRD/TechDoc doc-review)

Workflow 6 (`POST /workflow/doc-review`) extracts `PRD:` / `TechDoc:` Google Docs
links from a Jira ticket and reviews them with the LLM. Those docs are shared
**only inside the organisation**, so they cannot be read via the public export
URL. The service-account + domain-wide-delegation path we had configured is not
able to read them, so the app now authenticates as an **authorised org user via
OAuth** and falls back to the service account only if OAuth is unset.

Because Workflow 6 runs non-interactively (triggered by n8n), the OAuth consent
is done **once** by a human; the resulting offline *refresh token* is stored and
the app mints short-lived access tokens from it automatically.

## How the app chooses credentials

`app/doc_review.py` → `_drive()` tries, in order:

1. **OAuth user credentials** — `GOOGLE_OAUTH_CLIENT_ID`,
   `GOOGLE_OAUTH_CLIENT_SECRET`, and a refresh token from
   `GOOGLE_OAUTH_REFRESH_TOKEN` or `GOOGLE_OAUTH_TOKEN_FILE`.
2. **Service account** — `GOOGLE_SA_CREDENTIALS_JSON` + `GOOGLE_IMPERSONATE_USER`.
3. **Public export URL** — only works for "anyone with the link" docs.

## One-time setup

### 1. Google Cloud console

1. Pick/create a project and **enable the Google Drive API**
   (APIs & Services → Library → "Google Drive API" → Enable).
2. **OAuth consent screen**: User type **Internal** (so only org members can
   consent — this also enforces the "same organisation" requirement). Add the
   scope `https://www.googleapis.com/auth/drive.readonly`. If the project must be
   **External**, add the consenting user under **Test users**.
3. **Credentials → Create credentials → OAuth client ID → Desktop app.**
   Download the client-secret JSON.

### 2. Generate the refresh token (run once, as an org member who can open the docs)

```bash
cd JIRA-AI
pip install google-auth-oauthlib            # one-time, for the setup script only
python scripts/google_oauth_setup.py \
    --client-secret /path/to/client_secret.json \
    --out /run/secrets/wf6-gdocs-oauth.json
```

A browser opens; sign in with the **org account** that can view the BRD/PRD docs
and approve read-only Drive access.

On a **headless server**, forward the callback port and open the URL from your
laptop (Google's OOB copy/paste flow is disabled, so the callback must reach a
localhost server):

```bash
ssh -L 8765:localhost:8765 user@server      # in a second terminal
python scripts/google_oauth_setup.py --client-secret cs.json \
    --no-browser --port 8765 --out /run/secrets/wf6-gdocs-oauth.json
# open the printed URL in your local browser; the redirect to localhost:8765
# tunnels back to the server and the script captures the code.
```

The script prints `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and
`GOOGLE_OAUTH_REFRESH_TOKEN`, and (with `--out`) writes a JSON secret file.

### 3. Configure the app environment (`.env` / deployment secrets)

```dotenv
GOOGLE_OAUTH_CLIENT_ID=<client id>
GOOGLE_OAUTH_CLIENT_SECRET=<client secret>
# Either inline:
GOOGLE_OAUTH_REFRESH_TOKEN=<refresh token>
# …or via a file (recommended, matches the /run/secrets pattern):
GOOGLE_OAUTH_TOKEN_FILE=/run/secrets/wf6-gdocs-oauth.json
```

Restart the app. On the next doc-review, the logs show
`Google Drive client initialised via oauth`.

## Verify

```bash
curl -s -X POST localhost:8000/workflow/doc-review \
  -H 'Content-Type: application/json' \
  -d '{"issueKey":"RFT-123","description":"PRD: https://docs.google.com/document/d/<id>/edit"}'
```

- Success → the response has `reviewed >= 1` / `commentPosted: true`.
- `document is not shared with the review account (...)` → share the doc with the
  consenting user, or that user lacks org access.

## Notes & gotchas

- The refresh token is long-lived but is revoked if: the user removes the app at
  <https://myaccount.google.com/permissions>, the password changes with the
  consent app in *Testing* status (test-mode refresh tokens expire after 7 days —
  **publish** the consent screen for production), or the client secret is rotated.
- Scope is read-only (`drive.readonly`); the app never modifies docs.
- Keep the token file out of version control and readable only by the app user
  (`chmod 600`).
