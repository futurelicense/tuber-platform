# Deploying to Render + Supabase

What gets deployed (all defined in `render.yaml`):

| Piece | What it is |
|---|---|
| `tuber-platform` web service | Docker, standard plan (always-on), gunicorn `--workers 1 --threads 8`, 10 GB persistent disk at `/app/data` |
| `tuber-suggest-agent` cron | Same Docker image, runs `flask run-suggest-agent` every 6 hours |
| Database | **Supabase Postgres** (not Render-managed Postgres) — set via `DATABASE_URL`, never linked with `fromDatabase` |

Do everything below in order. Steps 1–4 are one-time setup; step 5 is the
deploy itself.

---

## 1. Supabase database

1. Create a project at [supabase.com](https://supabase.com) (any region close
   to your Render region; note the database password you choose).
2. Get the connection string: project **Connect** dialog → **Session pooler**
   tab. It looks like:

   ```
   postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
   ```

   **Use the Session pooler string specifically:**
   - Not the **direct connection** — it's IPv6-only and Render's network can't
     reach it (connections just time out).
   - Not the **Transaction pooler** (port 6543) — the app keeps a normal
     SQLAlchemy connection pool with session state; transaction pooling breaks
     that. `app/config.py` already sets `pool_pre_ping` + `pool_recycle=280`
     to cope with the session pooler's idle-connection killing.
   - A `postgres://` scheme also works — `config.py` normalizes it.
   - If your DB password contains special characters (`@`, `:`, `/`, `#`),
     URL-encode them in the string.

This string is your `DATABASE_URL` for **both** Render services below.

## 2. Push the repo to a remote

Render deploys from GitHub/GitLab. Create a **private** repo (the vendored
apps and platform code aren't secret-free-by-accident — keep it private
anyway) and push:

```bash
git remote add origin git@github.com:<you>/tuber-platform.git
git push -u origin main
```

Confirm secrets stayed out: `git ls-files | grep -E '\.env$|credentials|cookies'`
must print nothing.

## 3. OAuth apps (external portals)

You won't know the final domain until the first deploy; Render service URLs
are predictable (`https://tuber-platform.onrender.com` unless the name is
taken), but you can also come back and fill these in after step 5.

1. **Google** (Clipper's Drive/YouTube upload): in Google Cloud Console →
   Credentials, create an OAuth client of type **Web application** (the
   existing local `credentials.json` is a Desktop client — Google restricts
   those to loopback redirects, so it *cannot* work in production). Authorized
   redirect URI:

   ```
   https://<your-domain>/clip/google/callback
   ```

   Download the JSON — this is the `credentials.json` you'll upload as a
   Render secret file in step 4.
2. **TikTok** (Clipper's TikTok upload): in the TikTok Developer Portal, set
   the app's redirect URI to:

   ```
   https://<your-domain>/clip/tiktok/callback
   ```

## 4. Render Blueprint + configuration

1. Render dashboard → **New → Blueprint** → pick the repo. Render reads
   `render.yaml` and proposes both services. Apply.
2. Every `sync: false` env var must be filled in by hand before the first
   successful boot. In the **web service** → Environment:

   | Var | Value |
   |---|---|
   | `DATABASE_URL` | Supabase Session-pooler string from step 1 |
   | `PUBLIC_BASE_URL` | `https://<your-domain>` — no trailing slash. This is what flips session cookies to `Secure`, so set it to the real https domain, not a placeholder |
   | `AI_KEY` | Groq API key (clip suggestions, chaptering, metadata) |
   | `HF_API_KEY` | Hugging Face key (Clipper's suggest feature) |
   | `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET` | from the TikTok portal |
   | `PAYSTACK_SECRET_KEY` | from the Paystack dashboard → Settings → API Keys & Webhooks. **Required** once `DATABASE_URL` is set — same boot guard as `SECRET_KEY`/`CHANNEL_TOKEN_ENC_KEY`. Also register `https://<your-domain>/webhooks/paystack` as the webhook URL in that same Paystack settings page — Paystack only supports **one** webhook URL per account/mode, so both Master Class and marketplace payment confirmations share this single endpoint (it dispatches by the payment reference's `mc-`/`ch-` prefix) |
   | `PAYSTACK_PUBLIC_KEY` | from the same page — optional, only needed if a client-side Paystack flow is added later |

   `SECRET_KEY` and `CHANNEL_TOKEN_ENC_KEY` are `generateValue: true` —
   Render fills them; don't touch. `CHANNEL_TOKEN_ENC_KEY` must be a valid
   Fernet key if you ever set it manually
   (`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).

3. In the **cron service** → Environment:

   | Var | Value |
   |---|---|
   | `SECRET_KEY` | copy the web service's generated value. **Required** — the app now refuses to boot with `DATABASE_URL` set but no `SECRET_KEY` |
   | `CHANNEL_TOKEN_ENC_KEY` | copy the web service's generated value. **Required** — same boot guard as `SECRET_KEY`, refuses to start with `DATABASE_URL` set but no `CHANNEL_TOKEN_ENC_KEY` |
   | `DATABASE_URL` | same Supabase string as the web service |
   | `PUBLIC_BASE_URL` | same as the web service |
   | `AI_KEY` | same Groq key |
   | `PAYSTACK_SECRET_KEY` | copy the web service's value. **Required** — same boot guard as `SECRET_KEY`/`CHANNEL_TOKEN_ENC_KEY`, even though this process never touches Master Class checkout |

4. **Secret Files** (web service → Environment → Secret Files). Render mounts
   these at `/etc/secrets/<filename>`; `docker-entrypoint.sh` copies them into
   `vendor/youtube-clipper/` at boot where the Clipper reads them:

   | Filename (exactly) | Contents |
   |---|---|
   | `credentials.json` | the **Web application** OAuth client JSON from step 3 |
   | `cookies.txt` | Netscape-format browser cookie export for yt-dlp's bot-detection bypass — optional but age-gated/bot-checked videos fail without it |

## 5. First deploy and bootstrap

1. Trigger a deploy (Blueprint apply usually starts one). The
   `preDeployCommand: flask db upgrade` applies all migrations to Supabase
   before traffic switches — no manual migration step, this deploy and every
   future one.
2. Create the first admin from the web service's **Shell** tab:

   ```bash
   flask create-admin you@example.com a-strong-password
   ```

3. If the actual domain differs from what you guessed in steps 3–4, update
   `PUBLIC_BASE_URL` (both services) and the Google/TikTok redirect URIs now,
   then redeploy.

## 6. Verify

- [ ] `https://<domain>/healthz` returns `{"status": "ok"}`
- [ ] `/login` works; wrong password 10× throttles for 15 minutes (shows in Admin → Logins, including attempts against nonexistent emails)
- [ ] Admin dashboard loads; create one clipper + one producer from Users
- [ ] As the clipper: `/` lands on the suggestions queue; `/clip/` loads; `/produce/` returns 403
- [ ] As the producer: `/producer-scout/` loads; `/clip/` returns 403
- [ ] Clipper page → Google connect button completes OAuth (proves `credentials.json` + redirect URI)
- [ ] Admin → Watched channels: add a channel, then cron service → **Trigger Run**; suggestions appear in the clipper's queue afterwards
- [ ] Cut a small clip; confirm it survives a manual redeploy (proves the disk mount)

## Operational notes

- **Never scale past 1 instance / 1 worker.** Both vendored apps keep job
  state in an in-process dict; a second worker or instance silently loses
  jobs. The Dockerfile pins `--workers 1` and the login throttle also assumes
  one process. Scaling up requires the deferred job-state-durability work
  first.
- **Migrations** run automatically on every deploy via `preDeployCommand`.
- **Disk**: 10 GB at `/app/data` (clips + assembled videos). Watch usage in
  the Render dashboard; old job directories are never garbage-collected yet.
- **Supabase pauses free-tier projects** after ~1 week of inactivity — the
  cron's 6-hourly DB access normally keeps it warm, but if the project is
  paused the whole platform 500s until you resume it. A paid Supabase plan
  removes this.
- **Logs**: gunicorn + both vendored apps print to stdout → Render's Logs
  tab. The suggest agent's per-channel results appear in the cron service's
  run logs.
