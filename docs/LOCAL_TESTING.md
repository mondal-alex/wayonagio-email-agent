# Local Testing

← Back to the [main README](../README.md).

Use this flow to test Phase 1 locally with your own Gmail account. This keeps the rollout manual-first: API + Gmail Add-on, with the scanner disabled. You can follow it with either **Gemini** or **Ollama** as the LLM — step 2 forks and step 3 points at the per-branch `.env` values.

## 1. Create Gmail + Drive OAuth credentials

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Create a new project, or select an existing test project.
3. Open **APIs & Services → Library**.
4. Search for **Gmail API** and enable it.
5. Still in the Library, search for **Google Drive API** and enable it. The agent reads agency content out of Drive to build the knowledge base.
6. Open **APIs & Services → OAuth consent screen**.
7. Configure the app as an **External** app if needed.
8. Keep publishing status as **Testing** (do **not** publish for local testing).
9. Add your own Gmail address as a **Test user** while the app is in testing mode.
   - If your account is not listed as a test user, OAuth login will fail with `Error 403: access_denied`.
10. Open **OAuth consent screen / Data access** and add exactly these scopes:
    - `https://www.googleapis.com/auth/gmail.readonly`
    - `https://www.googleapis.com/auth/gmail.compose`
    - `https://www.googleapis.com/auth/drive.readonly`
11. Open **APIs & Services → Credentials**.
12. Click **Create Credentials → OAuth client ID**.
13. Choose **Desktop app**.
14. Download the client credentials JSON file.
15. Save it in this project, for example as `credentials.json`.

## 2. Choose and set up your LLM

Pick **one** of the two options below. You don't need both. Whichever you pick, note the environment variables it asks you to remember — they go into `.env` in step 3.

### Option A: Gemini (managed, recommended if you don't need offline)

Lowest-friction path. No local model to pull, no extra process to keep running, and you can reuse the same API key for the knowledge base's embedding model.

1. Go to [Google AI Studio → API keys](https://aistudio.google.com/app/apikey).
2. Create an API key (the free tier is generous enough for local testing).
3. Keep note of:
    - `LLM_MODEL=gemini/gemini-2.5-flash`
    - `GEMINI_API_KEY=<your-api-key>`

No server to start — `litellm` calls the Gemini HTTP API directly.

### Option B: Ollama (self-hosted, fully offline)

Pick this if you want the LLM to run fully on your machine with no outbound LLM traffic. You'll still want a Gemini API key for the KB's embedding model (see [Configure the knowledge base](#configure-the-knowledge-base) below) unless you also swap embeddings to an Ollama model.

1. Install Ollama from [ollama.com](https://ollama.com/download).
2. Start the Ollama server:

```bash
ollama serve
```

3. In a second terminal, download a model, for example:

```bash
ollama pull llama3.2
```

4. Keep note of:
    - `LLM_MODEL=ollama/llama3.2`
    - `OLLAMA_BASE_URL=http://localhost:11434`

## 3. Configure the app

Copy [`.env.example`](../.env.example) to `.env`:

```bash
cp .env.example .env
```

### Common values (both LLM options)

- `GMAIL_CREDENTIALS_PATH=credentials.json`
- `GMAIL_TOKEN_PATH=token.json`
- `AUTH_BEARER_TOKEN=<your test token>` (generate with `openssl rand -base64 32`)
- `SCANNER_ENABLED=false`

The scanner should stay off for Phase 1 local testing.

### LLM-specific values

If you picked **Option A (Gemini)** in step 2:

- `LLM_MODEL=gemini/gemini-2.5-flash`
- `GEMINI_API_KEY=<your-api-key>`
- Comment out or leave blank the `OLLAMA_*` lines — they're ignored whenever `LLM_MODEL` starts with `gemini/`.

If you picked **Option B (Ollama)** in step 2:

- `LLM_MODEL=ollama/llama3.2`
- `OLLAMA_BASE_URL=http://localhost:11434`
- Leave `GEMINI_API_KEY` unset for the LLM — but you'll likely still need it for the KB's embedding model, below.

### Configure the knowledge base

The KB is a **hard dependency** — every draft must be grounded in agency content, so `cli draft-reply` and the API both fail with `Knowledge base unavailable` until the KB is populated. Wire it up before moving on:

1. **Pick a Drive folder** that contains real agency material (tour descriptions, FAQs, templates). One or two Google Docs or PDFs is enough for local testing. Copy the folder ID from the URL (`https://drive.google.com/drive/folders/<id>`) or the full share URL — the app accepts either.

2. **Get a Gemini API key for embeddings.** Embeddings default to `gemini/text-embedding-004` because Google AI Studio hands out free-tier keys and the dimensionality matches what production uses. If you're already on Option A, reuse the same `GEMINI_API_KEY`. If you're on Option B and want to stay fully offline, set `KB_EMBEDDING_MODEL` to an Ollama embedding model such as `ollama/nomic-embed-text` (you'll also need to `ollama pull` it).

3. **Add these to `.env`:**
    - `KB_RAG_FOLDER_IDS=<folder-id-or-share-url>`
    - `KB_LOCAL_DIR=./kb_artifacts` (dev fallback; production uses `KB_GCS_URI`)
    - `KB_EMBEDDING_MODEL=gemini/text-embedding-004`
    - `GEMINI_API_KEY=<your-aistudio-key>` (same key you set for the LLM if you're on Option A)

You'll actually populate the index in step 4 below, after `cli auth` has produced a `token.json` — the ingest pipeline reuses the same OAuth token to read Drive.

## 4. Install dependencies and authenticate with Gmail

```bash
uv sync
uv run python -m wayonagio_email_agent.cli auth
```

The `auth` command opens a browser, asks you to sign in with your test Gmail account, and writes `token.json`. After that, the app can read messages and create drafts in that account.

If you change scopes later, delete `token.json` and run `cli auth` again so OAuth consent is refreshed with the updated scopes.

If you get `Error 403: access_denied`, verify all of the following:
- OAuth app is still in **Testing** (not published).
- The Gmail account you used to sign in is added under **OAuth consent screen / Audience → Test users**.
- You are using the same project/client that generated your `credentials.json`.

### Populate the knowledge base

With `token.json` now on disk, ingest the Drive folder you configured in step 3 and verify the result:

```bash
uv run python -m wayonagio_email_agent.cli kb-ingest
uv run python -m wayonagio_email_agent.cli kb-doctor
```

`kb-ingest` walks `KB_RAG_FOLDER_IDS`, extracts text from every Doc / PDF / plain-text file, chunks it, embeds it via the configured embedding model, and writes `./kb_artifacts/kb_index.sqlite`. `kb-doctor` prints a one-shot health report (artifact present, chunk count, per-source breakdown, embedding model the index was built with, ingest timestamp) and exits non-zero if anything is wrong — so it doubles as a smoke test.

You want to see `KB status: HEALTHY` and a non-zero chunk count before moving on. Common failures and fixes:

- **`KBConfigError: KB_RAG_FOLDER_IDS is required`** — you haven't set the env var yet. Re-check step 3.
- **Drive `403` / `insufficientPermissions`** — either the Drive API isn't enabled on your Cloud project (step 1 #5) or your `token.json` was issued before you added the `drive.readonly` scope. Delete `token.json` and re-run `cli auth` so OAuth re-prompts with the full scope set.
- **`KBUnavailableError: KB index ... is empty`** — the folder exists but contained no readable files (Google Sheets, Slides, and Forms are intentionally skipped). Drop a `.pdf`, Google Doc, `.txt`, or `.md` into the folder and re-ingest.
- **Gemini `401` / `quota exceeded`** — `GEMINI_API_KEY` is wrong or exhausted. Grab a fresh key at [aistudio.google.com/app/apikey](https://aistudio.google.com/app/apikey).

Re-run `kb-ingest` whenever you edit the source docs — the runtime caches the index in-process, but a second `kb-ingest` publishes a new artifact and the next `uvicorn` restart picks it up. For local testing, restarting the server is the easiest way to force a reload.

## 5. Test the backend manually from the CLI

List recent unread messages:

```bash
uv run python -m wayonagio_email_agent.cli list
```

```bash
uv run python -m wayonagio_email_agent.cli list --max 50
```

Pick a `message_id`, then create a draft reply:

```bash
uv run python -m wayonagio_email_agent.cli draft-reply <message_id>
```

Open Gmail and confirm:
- a draft was created,
- it appears in the correct thread,
- it was not sent automatically.

## 6. Test the local API directly

Start the API server:

```bash
uv run uvicorn wayonagio_email_agent.api:app --host 127.0.0.1 --port 8000
```

Then call it with `curl`:

```bash
curl -X POST http://127.0.0.1:8000/draft-reply \
  -H "Authorization: Bearer <AUTH_BEARER_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"message_id":"<message_id>","language":"it"}'
```

If the response includes a draft ID and a draft appears in Gmail, the API path is working.

## 7. Test the Gmail Add-on against your local machine

Google Apps Script cannot call `localhost` directly, so expose your local API with a public HTTPS tunnel such as `ngrok` or `cloudflared`.

Typical flow:

1. Start the API locally on port `8000`.
2. Start a tunnel that forwards to `http://127.0.0.1:8000`.
3. Copy the public HTTPS URL from the tunnel.
4. Go to [script.google.com](https://script.google.com/).
5. Create a new Apps Script project.
6. Copy in the contents of [`addon/Code.gs`](../addon/Code.gs) and [`addon/appsscript.json`](../addon/appsscript.json). See [Viewing `appsscript.json` in the editor](#viewing-appsscriptjson-in-the-editor) below if you don't see the manifest file.
7. In **Project Settings → Script Properties**, set:
    - `BACKEND_URL=<your public tunnel URL>`
    - `BEARER_TOKEN=<AUTH_BEARER_TOKEN from .env>`
8. Deploy it as a Google Workspace Add-on. See [Deploying the Add-on for testing](#deploying-the-add-on-for-testing) below for detailed steps.
9. Install it for your test account (done as part of the deployment flow below).
10. Open a Gmail message and click **Draft reply**.

You should see a notification in Gmail and a new draft should appear in the thread.

### Choosing a tunnel: `cloudflared` vs `ngrok`

| | `cloudflared` (quick tunnel) | `ngrok` |
|---|---|---|
| Signup required | No | Yes (free account) |
| Setup speed | Fastest | Fast after signup |
| Free stable URL | Only with a named tunnel (requires a domain on Cloudflare) | Reserved domains on paid plans |
| Request inspector | No | Yes, at `http://127.0.0.1:4040` |

For quick end-to-end testing, `cloudflared` is the lowest-friction option. For ongoing development where you want to see each request the Add-on makes, `ngrok` is easier to debug.

Quick tunnel with `cloudflared`:

```bash
brew install cloudflared
cloudflared tunnel --url http://127.0.0.1:8000
```

Copy the printed `https://*.trycloudflare.com` URL into the `BACKEND_URL` Script Property.

Quick tunnel with `ngrok`:

```bash
brew install ngrok
ngrok config add-authtoken <your-authtoken>
ngrok http 8000
```

Copy the printed `https://*.ngrok-free.app` URL into the `BACKEND_URL` Script Property.

Both tunnel URLs change on every restart (unless you pay for a reserved domain), so you'll need to update `BACKEND_URL` each time.

Security: both tunnels expose your backend to the public internet. Use a strong `AUTH_BEARER_TOKEN` (`openssl rand -base64 32`) and stop the tunnel with `Ctrl+C` when you're done testing.

### Viewing `appsscript.json` in the editor

Apps Script hides the manifest file by default. To edit it:

1. In the Apps Script editor, click the **gear icon** (⚙️ Project Settings) in the left sidebar.
2. Check **"Show 'appsscript.json' manifest file in editor"**.
3. Go back to the **Editor** (`<>` icon in the left sidebar).
4. `appsscript.json` now appears alongside `Code.gs` in the Files panel. Paste in the contents of [`addon/appsscript.json`](../addon/appsscript.json).

### Deploying the Add-on for testing

For day-to-day testing you use a **test deployment**, not a full Google Workspace Marketplace publication. A test deployment installs the Add-on only for your account (or Workspace domain, if signed in with a Workspace account) and can be updated as often as you like.

Before you deploy:

- Make sure `BACKEND_URL` and `BEARER_TOKEN` Script Properties are set.
- Make sure your tunnel is running and reachable at `BACKEND_URL`.
- Make sure the linked Google Cloud project has the **Gmail API** enabled and your account is listed as a test user on the OAuth consent screen (same project you use for the backend OAuth, or a separate project — either works).

Link a Google Cloud project (required for Gmail Add-ons):

1. In Apps Script, open **⚙️ Project Settings → Google Cloud Platform (GCP) Project**.
2. Click **Change project** and paste your project number (from Google Cloud Console → **IAM & Admin → Settings → Project number**).
3. Save.

Create a test deployment:

1. Click the blue **Deploy** button (top right) → **Test deployments**.
2. Click **Install**.
3. In the **Application(s)** section, make sure **Gmail** is selected.
4. Click **Done**.

Authorize the Add-on:

1. Open [Gmail](https://mail.google.com/) (refresh if it was already open).
2. Open any email.
3. Look for the Wayonagio Add-on icon in the right-hand sidebar (or under the **⋯** overflow menu). Click it.
4. You will be prompted to authorize the Add-on. Accept the scopes (`gmail.addons.execute`, `gmail.readonly`, `script.external_request`).
5. The Add-on card should now show **🇮🇹 Draft in Italian** and **🇵🇪 Draft in Spanish** buttons.

Click one of the buttons. You should see a "Draft created" notification and a new draft appear in the thread within a few seconds.

Updating the deployment:

- Any change you save in the Apps Script editor is picked up automatically by the test deployment the next time the Add-on runs. No redeploy needed.
- If you change `appsscript.json` scopes, reopen Gmail and re-authorize. See [Forcing re-authorization after a scope change](#forcing-re-authorization-after-a-scope-change) below if Gmail keeps showing the old permissions.

Troubleshooting:

- **Add-on icon doesn't appear**: refresh Gmail, or check **Settings → Add-ons** in Gmail and make sure the Add-on is installed and enabled.
- **"Authorization required" loop**: reinstall via **Deploy → Test deployments → Install** and re-authorize.
- **"You do not have permission to call UrlFetchApp.fetch" / "Specified permissions are not sufficient"**: the manifest is missing `https://www.googleapis.com/auth/script.external_request`, or Google is still using a cached authorization from before you added it. Confirm the scope is present in `appsscript.json`, then follow [Forcing re-authorization after a scope change](#forcing-re-authorization-after-a-scope-change).
- **"Backend error 401/403"**: the `BEARER_TOKEN` Script Property doesn't match `AUTH_BEARER_TOKEN` in `.env`. Update and retry (no redeploy needed).
- **"Backend error 404" or connection errors**: `BACKEND_URL` is stale — your tunnel URL changed. Update the Script Property.
- **Nothing happens after clicking a button**: open **Apps Script → Executions** (left sidebar) to see the Add-on's execution log, and check the backend logs in your terminal.

#### Forcing re-authorization after a scope change

Google caches the scopes you've granted per-user. When you add a new scope to `appsscript.json`, Gmail keeps using the old (smaller) grant until you explicitly revoke it, which produces errors like *"You do not have permission to call UrlFetchApp.fetch"* even though the manifest looks correct.

To force a fresh authorization:

1. **Confirm the updated manifest is saved.** In Apps Script, open `appsscript.json`, verify the new scope is present, and save (Cmd+S). The editor should no longer show "Unsaved changes". Cross-check in **⚙️ Project Settings → OAuth Scopes** — the new scope should be listed there.
2. **Uninstall the test deployment.** **Deploy → Test deployments → Uninstall** (if present).
3. **Revoke the Add-on in your Google Account.** Go to [myaccount.google.com/permissions](https://myaccount.google.com/permissions), find **"Wayonagio Draft Reply"** (or whatever the Add-on is named), click it, then **Remove access**. This is the step most often missed.
4. **Reinstall the test deployment.** **Deploy → Test deployments → Install**.
5. **Fully close and reopen Gmail** (not just refresh — close the tab).
6. **Click the Add-on icon** in Gmail's right sidebar. Google should now prompt you to grant the new permission (e.g. *"Connect to an external service"*). Accept.
7. **Retry the draft button.** It should now succeed.

You do **not** need to publish to the Google Workspace Marketplace for internal use. Publishing is only required if you want to install the Add-on outside your own domain.

### Optional: sync the Add-on from this repo with `clasp`

Apps Script has no native GitHub integration, but Google's [`clasp`](https://github.com/google/clasp) CLI lets you push the files in this repo straight into your Apps Script project so you don't have to copy-paste after every edit.

Install and sign in:

```bash
npm install -g @google/clasp
clasp login
```

Link the `addon/` folder to your Apps Script project (get the Script ID from Apps Script → ⚙️ Project Settings → **IDs → Script ID**):

```bash
cd addon
clasp clone <your-script-id> --rootDir .
```

After editing `addon/Code.gs` or `addon/appsscript.json` locally, push to Apps Script:

```bash
clasp push
```

`clasp clone` creates a `.clasp.json` file in `addon/`; it's already covered by `.gitignore`.

## 8. Recommended checks

Verify these before moving beyond local testing:
- Drafts are created, never sent.
- Replies stay in the original Gmail thread.
- Language detection looks reasonable for Italian, Spanish, and English emails.
- Invalid bearer tokens are rejected.
- `SCANNER_ENABLED=false` prevents the automatic scanner from starting.
- `cli kb-doctor` reports `HEALTHY` and a sensible chunk count — and the drafted replies visibly reflect content from your Drive folder (a tour description you recognize, a phrase from an FAQ). If drafts look generic, the KB isn't actually being consulted: re-check `KB_RAG_FOLDER_IDS`, re-run `kb-ingest`, and restart the API.
