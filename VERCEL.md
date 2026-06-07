# Deploying Pip on Vercel

Pip is now serverless-ready: it uses **Postgres** for data (not SQLite) and
keeps the **Gmail connection in the signed session cookie** (not server memory),
so it survives across Vercel's function instances. The repo already includes
`vercel.json` and a Postgres-aware `store.py`.

## 1. Add a Postgres database
In your Vercel project → **Storage → Create Database → Postgres** (Neon-backed).
Vercel auto-injects the connection env vars, including **`POSTGRES_URL`** — which
`store.py` picks up automatically. (Any `POSTGRES_URL` or `DATABASE_URL` works,
e.g. Neon or Supabase directly.)

## 2. Set environment variables
Project → **Settings → Environment Variables** (Production + Preview):

| Name | Value |
|------|-------|
| `GOOGLE_CLIENT_ID` | your OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | your OAuth client secret |
| `PIP_SECRET` | a long random string — **required** so the session cookie stays valid across instances |
| `OAUTH_REDIRECT` | `https://<your-domain>/auth/google/callback` |

`POSTGRES_URL` is set automatically by step 1. Don't commit secrets — `.env` is
gitignored and only used locally.

## 3. Deploy
Push to GitHub (the repo is already connected) or run `vercel --prod`. Vercel
reads `vercel.json`, installs `requirements.txt`, and routes all requests to the
FastAPI app in `server.py`.

## 4. Point Google at the live URL
In **Google Cloud → Credentials → your OAuth client → Authorized redirect URIs**,
add exactly what you set for `OAUTH_REDIRECT`, e.g.:
```
https://itspip.vercel.app/auth/google/callback
```
Keep yourself in **Test users** (consent screen) — up to 100 users work with no
verification. Submit for verification when you want it fully public.

## 5. (Optional) Custom domain pip.mixreflect.com
1. Vercel → project → **Domains** → add `pip.mixreflect.com`.
2. In mixreflect.com DNS add the CNAME Vercel shows.
3. Update `OAUTH_REDIRECT` to `https://pip.mixreflect.com/auth/google/callback`
   and add that same URI to the Google OAuth client. Redeploy.

## Notes / serverless gotchas
- **Function timeout.** A Gmail dig reads messages one by one; the scan is capped
  at 25 messages to fit. `vercel.json` sets `maxDuration: 60` (Pro). On the Hobby
  plan functions cap at ~10s — if scans time out, lower the limit in
  `_run_gmail_scan`.
- **Cookie size.** The Gmail token lives in the signed session cookie (~1 KB,
  well under the 4 KB limit). It's the user's own token in their own browser;
  signed (tamper-proof) but not encrypted — fine for launch, encrypt later if
  you want belt-and-suspenders.
- **Bundle size.** The Google + psycopg libraries are sizeable; if a build hits
  Vercel's size limit, that's the usual nudge to move to a container host
  (Render `Dockerfile` is already in the repo as a fallback).
