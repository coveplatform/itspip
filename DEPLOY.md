# Taking Pip live on pip.mixreflect.com

The plan: deploy the app to a host (Render — easiest with Docker + free HTTPS),
point a subdomain of your existing **mixreflect.com** at it, then update Google.

## 1. Put the code on GitHub
```bash
git init
git add .
git commit -m "Pip"
# create a repo on github.com, then:
git remote add origin https://github.com/<you>/pip.git
git push -u origin main
```

## 2. Deploy on Render
1. Go to <https://render.com> → **New → Blueprint** → connect the repo.
   (Render reads `render.yaml` and `Dockerfile` automatically.)
2. When prompted, set the two secret env vars:
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
3. Deploy. You'll get a URL like `https://pip.onrender.com`. Confirm it loads
   and a **sample dig** works (Gmail won't work until step 4–5).

## 3. Point pip.mixreflect.com at it
1. In Render → your service → **Settings → Custom Domains** → add
   `pip.mixreflect.com`. Render shows a target value.
2. In your **mixreflect.com DNS** (wherever you manage it), add a record:
   ```
   Type: CNAME   Name: pip   Value: <the target Render shows>
   ```
3. Wait for DNS + Render to issue the TLS cert (minutes to an hour). Now
   `https://pip.mixreflect.com` is live with HTTPS.

> The `OAUTH_REDIRECT` in `render.yaml` is already
> `https://pip.mixreflect.com/auth/google/callback`. If you use a different
> subdomain, change it there and redeploy.

## 4. Update Google Cloud (OAuth)
In **APIs & Services → Credentials → your OAuth client**:
- **Authorized redirect URIs** → add:
  ```
  https://pip.mixreflect.com/auth/google/callback
  ```
In **OAuth consent screen → Branding / App domain**:
- **Application home page:** `https://pip.mixreflect.com`
- **Privacy policy:** `https://pip.mixreflect.com/privacy`
- **Terms of service:** `https://pip.mixreflect.com/terms`
- **Authorised domains:** add `mixreflect.com`

## 5. Choose your launch mode (important)
`gmail.readonly` is a **restricted scope**, so Google gates public access:

- **Testing (now, instant):** keep the consent screen in *Testing* and add real
  users under **Test users** (up to **100**). They can connect immediately, no
  review. Best way to launch a beta today.
- **Production (public):** click *Publish App* and submit for **verification**.
  Google reviews the app because of the Gmail scope. Expect to provide:
  the privacy policy (done — `/privacy`), a short demo video of the consent
  flow, and — for restricted scopes — possibly a **CASA security assessment**.
  This can take days to weeks.

**Recommendation:** launch in **Testing** with your first 100 users now, start
collecting waitlist emails publicly, and run verification in parallel.

## 6. Add real payments (last piece)
Unlock currently flips `paid=1` directly. To charge:
1. Create a Stripe account → get API keys.
2. In `server.py` `unlock()`, create a **Stripe Checkout Session**, return its
   URL, and only set `paid=1` from the Stripe **webhook**.
3. Put Stripe keys in Render env vars. Tiers/prices live in the `TIERS` dict.

## Notes
- SQLite lives on the mounted disk (`/app/data`) so signups persist across
  deploys. For scale, swap to Postgres.
- Gmail session tokens are in memory; users reconnect after a redeploy. For
  production, move sessions to Redis/DB.
