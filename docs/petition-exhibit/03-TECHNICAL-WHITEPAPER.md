# MoneyTuber / Tuber Platform — Technical White Paper

**Audience:** Counsel with a technical advisor, or technically literate consultants  
**Scope:** Architecture, modules, integrity mechanisms, limits, petition suitability  
**Sources:** Repository README, `app/`, `vendor/`, `tests/`, `DEPLOYMENT.md`  

---

## Abstract

Tuber Platform is a Flask-based WSGI application that mounts two vendored media tools — Youtube-Clipper at `/clip` and ytproduction at `/produce` — behind shared authentication, role authorization, admin monitoring, affiliate attribution, and Paystack-backed commercial products. Public marketing uses the MoneyTuber brand. Phase 1 deliberately defers per-user YouTube OAuth, granular activity semantics, computed rewards, and multi-worker durable job state. AI is used for clip *suggestion* only. Automated tests cover middleware, affiliate flows, Master Class webhooks, marketplace locking, and the suggest-agent data model.

---

## 1. Principles

1. **One deployable service** — platform + tools share one Render web service.  
2. **Role boundaries first** — tool mounts are not anonymously public.  
3. **Vendored, not submodule-fragile** — Clipper and ytproduction are committed directories with small platform tweaks.  
4. **Commerce integrity over UI trust** — amounts and payment success are server-verified.  
5. **Human-in-the-loop for AI** — suggestions are queued; cutting/publishing remain human actions.  
6. **Honest deferrals** — unfinished capabilities are documented rather than silently implied.

---

## 2. Architecture

![Figure 2 — System architecture](./figures/fig-02-architecture.png)

### 2.1 Runtime composition

`wsgi.py` builds `platform_app = create_app()` and wraps it with `build_wsgi_app()`:

- `ProxyFix` for reverse-proxy headers  
- `DispatcherMiddleware` mapping `/` → platform, `/clip` → Clipper, `/produce` → ytproduction  

Each mount is wrapped (outer → inner conceptually for request entry):

1. **OriginCheckMiddleware** — CSRF defense for mutating methods  
2. **PrefixRewriteMiddleware** — fixes root-relative URLs/JSON paths for the mount prefix  
3. **RoleGateMiddleware** — session/role check + coarse `ActivityLog` writes  

### 2.2 Platform modules (Flask blueprints)

| Blueprint / area | Responsibility |
|------------------|----------------|
| `auth` | Login/logout, homepage routing |
| `admin` | Users, activity, logins, affiliates, marketplace admin, master class admin, rewards, watched channels |
| `suggestions` | Human review queue for AI suggestions |
| `producer_scout` | Producer-side scout UI |
| `affiliate` | Signup, referral `/r/<code>`, interest capture, dashboard |
| `master_class` | Sales, checkout, pending/success |
| `marketplace` | Browse, detail, buy, pending/success/conflict |
| `webhooks` | Consolidated Paystack webhook |

### 2.3 Data & deploy

- SQLAlchemy models; Flask-Migrate migrations  
- Postgres via `DATABASE_URL` (Supabase in deployment docs); SQLite default for local  
- Persistent disk paths for clipper downloads / ytproduction output  
- Gunicorn: intentionally `--workers 1` because vendored apps keep job state in-process  

---

## 3. Capability modules → outcomes

![Figure 3 — Capabilities → outcomes](./figures/fig-03-modules.png)

### 3.1 Clipper (`/clip`)

Local-style clipping UX (URL → in/out → cut → download), quality options, progress streaming, and related integrations present in the vendored app. Platform contribution: mount prefix, role gate, origin check, URL rewrite.

### 3.2 Producer (`/produce`)

Production generation/assembly workflows from ytproduction, similarly gated and rewritten.

### 3.3 Suggest-agent

`flask run-suggest-agent` polls active `WatchedChannel` rows, lists uploads, fetches transcripts, calls suggestion helpers, and writes `SuggestedClip` rows. Status transitions and review-queue race guards are tested. **No autonomous publish path.**

### 3.4 Marketplace

Listings with availability states; `reserve_listing` uses atomic `UPDATE…WHERE`; finalize on payment; stale reservations self-heal; `payment_conflict` path covered in tests.

### 3.5 Master Class

Server-side pricing only; webhook signature verification (valid/missing/tampered); idempotent duplicate delivery handling; callback re-verify; commission creation.

### 3.6 Affiliates

Referral codes, cookie/attribution paths, interest intake types, admin commission CRUD / rate resolution, dashboard authorization. Affiliates excluded from tool mounts.

---

## 4. Methodology integrity rules (platform engineering)

![Figure 4 — Integrity safeguards](./figures/fig-04-integrity.png)

| Rule | Implementation signal |
|------|------------------------|
| Least privilege to tools | `User.can_access` + RoleGate |
| CSRF for tokenless mounts | Origin/Referer matching middleware |
| No client-trusted prices | Paystack amounts from server |
| Verify payment events | HMAC-SHA512 webhook + callback verify |
| Prevent double-sale | Two-stage lock + conflict handling |
| AI non-autonomy | Suggest-only agent docstring/CLI |
| Fail closed on secrets in prod | RuntimeError if critical keys missing when `DATABASE_URL` set |
| Token-at-rest intent | Fernet via `CHANNEL_TOKEN_ENC_KEY` (OAuth write path still Phase-1 deferred) |
| Regression safety | `unittest` suites listed in README |

---

## 5. Interpretive limits

- **Not a forecasting model** for subscriber growth or revenue.  
- **Not a certification** of creator skill or channel quality.  
- **Not proof** of YouTube partnership or ToS compliance for every user action.  
- **Marketing claims** (e.g., “0 to 1,000 subscribers in 30 days”) are offer copy — counsel must not treat them as validated experimental results unless separately evidenced.  
- **Demo data** in screenshots is illustrative.  
- **License/public repo** must be confirmed outside this workspace copy before open-source characterizations.

---

## 6. Petition suitability

MoneyTuber is suitable as a software exhibit when the petition theory needs evidence of:

- An implemented multi-user digital operations platform  
- Engineering attention to authorization, payment integrity, and concurrency  
- Responsible AI assistance (human review)  
- Transparent scope management (Phase-1 deferrals)

It is a weak exhibit if counsel needs a scientific scoring engine, peer-reviewed economic model, or government-certified dataset — those are different genres (see, e.g., decision-support frameworks with published formulas).

---

## 7. Verification line for counsel

Independent technical reviewers can:

1. Read `README.md` and `app/mounting/*.py`  
2. Run `python3 -m unittest discover -s tests -v`  
3. Inspect Paystack verification in `app/paystack.py` and webhook routes  
4. Inspect marketplace reservation in `app/marketplace/services.py`  
5. Confirm suggest-only language in `app/__init__.py` (`run-suggest-agent`)
