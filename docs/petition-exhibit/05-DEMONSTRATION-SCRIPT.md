# MoneyTuber — Demonstration Script

**Audience:** Live showcase for counsel, consultants, or petition prep  
**Duration:** ~12–15 minutes  
**Environment:** Local `python3 wsgi.py` on `:8000` or deployed staging URL  

---

## Opening disclaimer (say this first)

> What you will see is a **working software platform**. Any user counts, listings, or commissions shown in a demo environment are **illustrative** unless we explicitly switch to production records. MoneyTuber does **not** guarantee subscriber growth or earnings. The AI suggestion feature **proposes** clips for human review — it does **not** automatically cut or publish. We are demonstrating an **inspectable operating method**, not a prediction of market outcomes.

---

## Do not say

- “This is an official YouTube partner product.”  
- “The AI runs the channel by itself.”  
- “Everyone who takes the Master Class hits 1,000 subscribers.”  
- “We’re open-source under [license]” — unless LICENSE + repo URL are confirmed.  
- “These dashboard numbers are our audited production metrics” — unless they are.

---

## Timed walkthrough

| Min | Screen | Action | Talk track |
|----:|--------|--------|------------|
| 0:00 | — | Read opening disclaimer | Set integrity frame |
| 0:45 | Landing `/` | Scroll pillars | Public MoneyTuber offers: grow, buy channel, Master Class, affiliates |
| 2:00 | Login | Log in as admin | Accounts are role-based; squad roles are admin-created |
| 3:00 | Admin dashboard | Show stats, activity, logins | Oversight: coarse activity + login geo |
| 5:00 | Users | Point at roles | admin / clipper / producer / affiliate separation |
| 6:30 | Suggestions queue | Show pending approve/reject | AI suggests; humans decide |
| 8:00 | `/clip` (clipper user) | Paste a **rights-cleared** demo URL or show UI only | Role-gated tool mount; do not demo piracy |
| 10:00 | Marketplace or Master Class | Browse UI; explain Paystack verify | Server-side price; webhook HMAC; reserve/finalize |
| 12:00 | Affiliate signup/dashboard | Referral link concept | Attribution + commissions on verified conversions |
| 13:30 | Close | Return to disclaimer themes | Phase-1 limits: OAuth deferral, coarse logs, workers=1 |

---

## Screenshot checklist (for binders)

1. Landing / pillars  
2. Login  
3. Admin dashboard  
4. Suggestions queue  
5. Marketplace browse (or Coming soon state — either is honest)  
6. Clipper tool (`/clip`)  
7. Affiliate surface  
8. Source/license status plate (until LICENSE confirmed)

See [06 — Visual Appendix](./06-VISUAL-APPENDIX.md).

---

## Hard-question rebound lines

**“So it’s automated content spam?”**  
→ “No. Roles gate tools, and the suggestion agent only queues ideas for people to review.”

**“Prove the Master Class works.”**  
→ “We can prove the **enrollment and payment verification system** works. Learning outcomes require separate evidence.”

**“Is the marketplace live?”**  
→ “The code path is implemented and tested; the public storefront may be closed (‘Coming soon’). We’ll state the configured state accurately.”
