# Tuber Platform

Wraps [Youtube-Clipper](vendor/youtube-clipper) (mounted at `/clip`, for
Clippers) and [ytproduction](vendor/ytproduction) (mounted at `/produce`,
for Producers) behind a single Flask app with accounts, roles, an admin
dashboard, activity/login monitoring, and IP geolocation. Deploys as one
Render web service.

See `/home/emc2/.claude/plans/woolly-rolling-bird.md` for the full
architecture writeup this was built from.

## Local development

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt -r requirements-clipper.txt -r requirements-ytproduction.txt

cp .env.example .env   # fill in AI_KEY etc if you want the clip-suggest/script
                        # features to work locally; everything else runs without it

export FLASK_APP=wsgi:platform_app   # note: platform_app, not application —
                                      # `application` is the fully WSGI-wrapped
                                      # (ProxyFix+DispatcherMiddleware) callable
                                      # gunicorn serves; Flask CLI commands need
                                      # the plain Flask instance instead.
flask db upgrade
flask create-admin you@example.com yourpassword

python3 wsgi.py   # dev server on :8000, both /clip and /produce mounted
```

Then log in at `http://localhost:8000/login`. Create clipper/producer
accounts from the admin dashboard's Users page — there's no public signup.

## Tests

```bash
python3 -m unittest tests.test_prefix_rewrite -v
```

Covers `PrefixRewriteMiddleware` — the middleware that rewrites the
root-relative URLs both vendored apps hardcode in their HTML/JSON responses
so they resolve under `/clip`/`/produce` instead of the platform root. See
that file's module docstring for why this middleware exists at all.

## Known pre-deployment blockers (not yet done)

These aren't optional polish — the platform won't work correctly on Render
until they're resolved:

1. **Submodule URLs point at local filesystem paths** (`/home/emc2/Developer/Youtube-Clipper`,
   `/home/emc2/Developer/ytproduction`), since that's what existed at build
   time and neither is on a remote host yet. Render's build can't resolve
   those. Push both to a real git host (GitHub etc.) and update
   `.gitmodules` + `git submodule set-url` before deploying.
2. **Google OAuth client must be reissued as a "Web application" type.**
   `vendor/youtube-clipper/credentials.json` (gitignored, not vendored) is
   currently a Desktop/installed-app client, which Google restricts to
   loopback redirect URIs only — it cannot accept a production HTTPS
   redirect at all. Create a new Web application OAuth client in Google
   Cloud Console with redirect URI
   `https://<your-render-domain>/clip/google/callback`, and place the
   resulting `credentials.json` in `vendor/youtube-clipper/` (as a Render
   secret file, not committed).
3. **TikTok's registered redirect URI** needs updating to
   `https://<your-render-domain>/clip/tiktok/callback` in the TikTok
   Developer Portal.
4. **`cookies.txt`** (yt-dlp bot-detection bypass) is gitignored and not
   vendored — provision it as a Render secret file if age-gated/bot-checked
   video extraction needs to work in production.
5. Set `PUBLIC_BASE_URL` in Render's env vars to the real deployed domain
   (no trailing slash) once known.

## Explicitly deferred past Phase 1

- Real per-tuber YouTube OAuth (the `connected_channels` table exists but
  nothing writes to it yet — Clipper still uses its own shared token file).
- Granular activity instrumentation (Phase 1's `ActivityLog` only records
  coarse route-hit events from the gate middleware, not real action
  semantics like "clip created").
- Computed reward evaluation against `metric_events`/`reward_rules` (CRUD
  only right now).
- Job-state durability — both vendored apps hold job state in an in-process
  dict; `gunicorn` is intentionally pinned to `--workers 1` in the
  Dockerfile to avoid a second worker "losing" jobs started on the first.
