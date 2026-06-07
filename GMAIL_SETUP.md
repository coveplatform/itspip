# Connecting real Gmail to Cashew

Cashew can scan real inboxes read-only via Google OAuth. The code is already wired
up — you just need to give the server Google credentials. ~10 minutes.

## 1. Make a Google Cloud project
1. Go to <https://console.cloud.google.com/> → create a project (e.g. "Cashew").

## 2. Enable the Gmail API
1. **APIs & Services → Library** → search **Gmail API** → **Enable**.

## 3. Configure the OAuth consent screen
1. **APIs & Services → OAuth consent screen**.
2. User type: **External** → Create.
3. Fill app name ("Cashew"), your support email, developer email. Save.
4. **Scopes** → Add → add `.../auth/gmail.readonly` → Save.
5. **Test users** → add the Gmail addresses you'll test with (up to 100).
   - While the app is in "Testing", only these users can connect. That's fine
     for a beta. To open it to everyone you must submit for **verification**
     (Google reviews apps that use the restricted `gmail.readonly` scope).

## 4. Create OAuth credentials
1. **APIs & Services → Credentials → Create credentials → OAuth client ID**.
2. Application type: **Web application**.
3. **Authorized redirect URIs** → add exactly:
   ```
   http://127.0.0.1:8000/auth/google/callback
   ```
   (add your production URL too when you deploy)
4. Create → copy the **Client ID** and **Client secret**.

## 5. Give them to the server
Set environment variables before running `python server.py`:

**PowerShell (Windows):**
```powershell
$env:GOOGLE_CLIENT_ID    = "xxxx.apps.googleusercontent.com"
$env:GOOGLE_CLIENT_SECRET = "yyyy"
$env:PIP_SECRET          = "any-long-random-string"   # keeps sessions stable
python server.py
```

**bash:**
```bash
export GOOGLE_CLIENT_ID="xxxx.apps.googleusercontent.com"
export GOOGLE_CLIENT_SECRET="yyyy"
export PIP_SECRET="any-long-random-string"
python server.py
```

Now open <http://127.0.0.1:8000>, click **Connect your Gmail**, approve the
read-only consent, and hit **dig my real inbox**.

## How it works / privacy
- Scope is **`gmail.readonly`** — Cashew can read, never send/delete/modify.
- The server queries only money-bearing mail (gift cards, credits, rewards),
  scans each message in memory, and **discards it** — message bodies and codes
  are never written to disk. Only the per-scan results (brand, amount) live in
  `data/waitlist.db` so the paywall can reveal them on unlock.
- Tokens are held in memory for the session and dropped on **disconnect**.

## Going to production
- Swap the in-memory `_GMAIL_SESSIONS` dict for a real session store (Redis/DB).
- Serve over **https** and remove `OAUTHLIB_INSECURE_TRANSPORT`.
- Submit the consent screen for verification to allow non-test users.
- Move SQLite → Postgres.
