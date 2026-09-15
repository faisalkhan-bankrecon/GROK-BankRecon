# Concord — Bank Reconciliation Engine (Multi-user)

Python matching engine + web UI with **Google sign-in** and **private per-user data**.

Supports **CSV**, **Excel (.xlsx)**, **OFX/QFX**.

---

## Features

- Google sign-in (Supabase Auth)
- Each user has a private workspace (Postgres)
- Auto-match + manual match
- Exceptions + recon report
- Export CSV / Excel

---

## 1. Create a free Supabase project

1. Go to [https://supabase.com](https://supabase.com) → New project  
2. Wait for the database to finish provisioning  
3. Open **SQL Editor** → paste and run the contents of `supabase_schema.sql`  
4. Go to **Project Settings → API** and copy:
   - Project URL → `SUPABASE_URL`
   - `anon` `public` key → `SUPABASE_ANON_KEY`
   - `JWT Secret` (under JWT Keys) → `SUPABASE_JWT_SECRET`
5. Go to **Project Settings → Database** → copy the **URI** connection string  
   (use the one with password; replace `[YOUR-PASSWORD]`) → `DATABASE_URL`  
   Prefer the **Transaction** pooler URI on port `6543` if available.

---

## 2. Enable Google sign-in

1. In Supabase: **Authentication → Providers → Google** → Enable  
2. Create OAuth credentials in [Google Cloud Console](https://console.cloud.google.com/apis/credentials):
   - Type: **OAuth client ID** → Web application  
   - Authorized JavaScript origins:
     - `http://localhost:8080`
     - `https://YOUR-RAILWAY-DOMAIN.up.railway.app`
   - Authorized redirect URIs:
     - `https://YOUR_SUPABASE_PROJECT.supabase.co/auth/v1/callback`
3. Copy Client ID + Client Secret into the Supabase Google provider settings → Save  

---

## 3. Railway environment variables

In Railway → your service → **Variables**, add:

| Name | Value |
|------|--------|
| `SUPABASE_URL` | from Supabase API settings |
| `SUPABASE_ANON_KEY` | anon public key |
| `SUPABASE_JWT_SECRET` | JWT secret |
| `DATABASE_URL` | Postgres URI from Supabase |

Start command (already in `Procfile` / `railway.toml`):

```text
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

---

## 4. Local run

```bash
export SUPABASE_URL=...
export SUPABASE_ANON_KEY=...
export SUPABASE_JWT_SECRET=...
export DATABASE_URL=...

pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Open http://127.0.0.1:8080 → **Continue with Google**.

---

## Security model

- Browser signs in via Supabase (Google OAuth)
- API requires `Authorization: Bearer <access_token>`
- Backend verifies JWT with `SUPABASE_JWT_SECRET`
- All transactions/matches are stored with `user_id` — users never see each other’s data

---

## License

MIT
