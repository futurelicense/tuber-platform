# MoneyTuber (Tuber Platform) — Petition Exhibit Documentation Package

**Audience:** Client sponsors, consultants, and counsel preparing a petition exhibit  
**Purpose:** Equip non-engineering readers with end-to-end understanding so they can position MoneyTuber / Tuber Platform accurately — without overclaiming  
**Product identity:** MoneyTuber (public brand) · Tuber Platform (engineering/repo name)  
**Maturity label:** Phase 1 (explicit deferrals documented in README)  
**Date:** 8 August 2026  

---

## Does this use case make sense?

Yes. For an exhibit attached to a petition, counsel does not need to become software engineers. They need:

1. A clear, defensible description of **what the platform is and is not**  
2. Language that connects the work to **public-interest / national-importance themes** without overreach  
3. Enough **operational and integrity depth** to answer hard questions  
4. A **live or recorded demonstration path** that makes the exhibit tangible  

This package is written for that purpose.

---

## Discovered product identity (auto-discovery summary)

| Fact | Discovery |
|------|-----------|
| Public name | **MoneyTuber** (“Digital Real Estate for YouTube”) |
| Engineering name | **Tuber Platform** |
| Plain-English purpose | Unified web platform that trains/operates clipper and producer squads, wraps production tools, and sells related digital products (marketplace, master class, affiliates) |
| Domains | Role-gated Clipper (`/clip`), Producer (`/produce`), AI suggest-queue (suggest-only), channel marketplace, 0→1k Master Class, affiliate program, admin monitoring |
| Stack | Flask + SQLAlchemy; Postgres (Supabase) / SQLite local; Paystack; optional Groq/HF AI; Render deploy |
| License | **No root LICENSE found** in this workspace copy; do not assert Apache/MIT/etc. without the Petitioner’s authoritative license |
| Repo URL | **No git remote configured** in this workspace copy; obtain from Petitioner |
| Version string | No semver tag discovered; treat as **Phase 1** per README |
| Geography | Product copy is general; payments use Paystack (amounts in kobo/NGN subunits in code comments) — do not invent a jurisdiction claim |
| Local-first? | **No** — multi-user hosted web platform |
| Open-source? | Source is present and inspectable in-repo; **public open-source status not confirmed** without remote + LICENSE |
| AI-based? | **Partially** — optional AI for clip *suggestions* only; humans review; agent never cuts/publishes |
| Formula-based decision engine? | **No** — this is an operations/commerce platform, not a scoring model |

---

## Recommended reading order

| Order | Document | Who reads it | Time |
|------:|----------|--------------|------|
| 1 | [01 — Executive Brief](./01-EXECUTIVE-BRIEF.md) | Everyone; petition decision-makers | 5–10 min |
| 2 | [02 — Counsel Briefing](./02-COUNSEL-BRIEFING.md) | Lawyer / consultant drafting language | 20–30 min |
| 3 | [06 — Visual Appendix](./06-VISUAL-APPENDIX.md) | Everyone — diagrams + screenshots | 10–15 min |
| 4 | [03 — Technical White Paper](./03-TECHNICAL-WHITEPAPER.md) | Counsel + technical advisor for depth | 30–45 min |
| 5 | [04 — Exhibit Statement](./04-EXHIBIT-STATEMENT.md) | Attachment / appendix language | 10 min |
| 6 | [05 — Demonstration Script](./05-DEMONSTRATION-SCRIPT.md) | Live showcase / hearing prep | 15 min + practice |

**Supporting (already in repo):**

| Document | Role |
|----------|------|
| [../../README.md](../../README.md) | Architecture overview, tests, Phase-1 deferrals |
| [../../DEPLOYMENT.md](../../DEPLOYMENT.md) | Render + Supabase deployment walkthrough |

---

## What this package is designed to prevent

- Calling MoneyTuber a **government program**, **certified training standard**, or **YouTube partner product**  
- Claiming the AI agent **automatically cuts or publishes** content  
- Treating **illustrative / demo numbers** as production metrics or earnings proof  
- Asserting a specific **open-source license** without a LICENSE file / remote confirmation  
- Overstating Phase-1 features that README marks as **deferred** (per-user YouTube OAuth, granular activity semantics, computed rewards, durable multi-worker job state)  
- Understating value: it is still a real, role-gated, payment-integrity-aware operating platform with automated tests  

---

## Suggested exhibit set for the petition binder

1. **Exhibit A** — Executive Brief (01)  
2. **Exhibit B** — Formal Exhibit Statement (04)  
3. **Exhibit C** — Visual Appendix (06): diagrams + screenshots  
4. **Exhibit D** — Technical White Paper (03)  
5. **Exhibit E** — Source tree reference + Petitioner-supplied LICENSE / repository URL  
6. **Optional** — Live demo guided by Demonstration Script (05)  

Counsel should customize national-importance framing to the specific petition theory; documents here provide accurate product substance and safe articulation patterns.
