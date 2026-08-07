# Tuber Platform

Wraps [Youtube-Clipper](vendor/youtube-clipper) (mounted at `/clip`, for
Clippers) and [ytproduction](vendor/ytproduction) (mounted at `/produce`,
for Producers) behind a single Flask app with accounts, roles, an admin
dashboard, activity/login monitoring, and IP geolocation. Deploys as one
Render web service.

`vendor/youtube-clipper` and `vendor/ytproduction` are plain committed
directories in this repo (not git submodules) — one repo, one push, nothing
to keep in sync. They're a snapshot of the standalone apps with two small
platform-specific tweaks (parametrized OAuth redirect URIs and output
directories, see git log for those files); pulling in upstream changes to
either app later is a manual copy-and-diff, not an automatic submodule
update.

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
accounts from the admin dashboard's Users page. Affiliate accounts are
self-serve at `/affiliate/signup`; there's no public signup for the
internal squad roles.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

- `test_prefix_rewrite` — `PrefixRewriteMiddleware`, which rewrites the
  root-relative URLs both vendored apps hardcode in their HTML/JSON responses
  so they resolve under `/clip`/`/produce` instead of the platform root. See
  that file's module docstring for why this middleware exists at all.
- `test_origin_check` — `OriginCheckMiddleware`, the CSRF defense for
  `/clip`/`/produce` (neither vendored app has a token concept of its own).
  See that file's module docstring for why Origin/Referer matching is used
  instead of a token.
- `test_suggest_agent` — the suggest-agent's data model (dedup, status
  transitions, controlled vocab) and the suggestions review-queue's
  approve/reject race guard.
- `test_affiliate` — the affiliate program: signup, `/r/<code>` referral
  attribution, the homepage's `/interest` capture, admin commission CRUD
  and rate resolution, dashboard authorization, and confirming
  `RoleGateMiddleware` still excludes the affiliate role from `/clip`/`/produce`.
- `test_master_class` — Paystack-backed enrollment: server-side-only
  pricing, webhook signature verification (valid/missing/tampered),
  idempotency on duplicate webhook delivery, the callback route re-verifying
  rather than trusting its query string, and automatic commission creation
  at the correct (override-or-default) rate.
- `test_marketplace` — the channel marketplace: everything `test_master_class`
  covers, plus the two-stage locking that keeps a unique listing from being
  sold to two buyers at once (reserve at checkout, finalize at payment) and
  the `payment_conflict` fallback for the rare case both stages still race.

Both `test_master_class` and `test_marketplace` post webhook payloads to the
single consolidated `/webhooks/paystack` endpoint (`app/webhooks/routes.py`)
that both products actually share in production — see that module's
docstring for why (Paystack only supports one webhook URL per account).

## Deploying

See **[DEPLOYMENT.md](DEPLOYMENT.md)** for the full Render + Supabase
walkthrough — Supabase connection-string choice, Blueprint env vars, the
Google "Web application" OAuth client reissue, secret files
(`credentials.json`/`cookies.txt`, copied into place by
`docker-entrypoint.sh`), first-admin bootstrap, and a post-deploy
verification checklist.

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
