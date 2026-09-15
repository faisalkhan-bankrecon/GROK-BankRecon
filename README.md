# Concord — Bank Reconciliation Engine

Python matching engine + web UI with **Google sign-in** (Supabase) and **private per-user workspaces**.

Supports **CSV**, **Excel (.xlsx)**, **OFX / QFX**.

---

## Quick start (Railway)

### 1. Supabase project

1. Create a project at https://supabase.com
2. **SQL Editor** → run the entire contents of `supabase_schema.sql`
3. **Project Settings → API** copy:
   - Project URL → `SUPABASE_URL`
   - `anon` `public` key → `SUPABASE_ANON_KEY`
   - (optional) JWT Secret → `SUPABASE_JWT_SECRET` (legacy HS256; JWKS is preferred)
4. **Project Settings → Database** → copy the **Transaction pooler** URI (port `6543`) → `DATABASE_URL`

### 2. Google OAuth (critical)

1. Supabase → **Authentication → Providers → Google** → Enable  
2. Google Cloud Console → **APIs & Services → Credentials** → Create **OAuth client ID** (Web application)

**Authorized JavaScript origins**
```
http://localhost:8080
https://YOUR-APP.up.railway.app
```

**Authorized redirect URIs**
```
https://YOUR_SUPABASE_REF.supabase.co/auth/v1/callback
```

3. Paste Client ID + Client Secret into the Supabase Google provider → Save

4. Supabase → **Authentication → URL Configuration**
   - **Site URL** = `https://YOUR-APP.up.railway.app`
   - **Redirect URLs** add:
     ```
     https://YOUR-APP.up.railway.app/**
     http://localhost:8080/**
     ```

### 3. Railway variables

| Variable | Required | Notes |
|----------|----------|-------|
| `SUPABASE_URL` | Yes | `https://xxxx.supabase.co` (no trailing slash) |
| `SUPABASE_ANON_KEY` | Yes | anon public key |
| `DATABASE_URL` | Yes | Postgres pooler URI |
| `SUPABASE_JWT_SECRET` | Optional | only needed for very old projects |

Start command (already set):
```
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

### 4. Deploy

```bash
git add .
git commit -m "Concord auth + recon engine"
git push
```

Railway will auto-deploy. Open the public URL → **Continue with Google**.

---

## Local development

```bash
export SUPABASE_URL=https://xxxx.supabase.co
export SUPABASE_ANON_KEY=eyJ...
export DATABASE_URL=postgresql://...
# optional:
# export SUPABASE_JWT_SECRET=...

pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Open http://127.0.0.1:8080

---

## Auth architecture

- Browser uses Supabase JS → Google OAuth
- After redirect, Supabase stores the session and the UI reads `session.access_token`
- Every API call sends `Authorization: Bearer <access_token>`
- Backend verifies the JWT with:
  1. **JWKS** (ES256 / RS256) — modern default
  2. Fallback to **HS256** + `SUPABASE_JWT_SECRET` if present
- All data is stored with `user_id` so each account is isolated

---

## Troubleshooting “stuck on Continue with Google”

1. Open browser DevTools → Console + Network after clicking the button.
2. Hit `https://YOUR-APP.up.railway.app/api/health` — all flags should be `true` except maybe `jwt_secret`.
3. Confirm **Site URL** and **Redirect URLs** in Supabase exactly match your Railway domain (including `https://`).
4. Confirm Google Cloud redirect URI is exactly:
   `https://YOUR_SUPABASE_REF.supabase.co/auth/v1/callback`
5. Check Railway logs for lines starting with `[auth] verify failed`.

---

## License

MIT
