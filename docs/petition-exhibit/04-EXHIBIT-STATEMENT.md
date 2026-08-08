# Exhibit Statement — MoneyTuber (Tuber Platform)

**Title for binder:** Software Exhibit — MoneyTuber / Tuber Platform  
**Document type:** Formal attachable statement  
**Date:** 8 August 2026  

---

## Statement

1. **Identity.** The software system described herein is publicly branded **MoneyTuber** and implemented in source as **Tuber Platform**, a Flask web application that unifies accounts, roles, admin monitoring, affiliate attribution, commercial checkout flows, and two role-gated media tools.

2. **Purpose.** The system is designed to operate clipper and producer workflows for YouTube-oriented content operations, to queue human-reviewed AI clip suggestions, and to support related commercial offerings (channel marketplace and Master Class enrollment) with server-side payment verification.

3. **Architecture.** The platform mounts Youtube-Clipper at `/clip` and ytproduction at `/produce` via WSGI dispatcher middleware, with role gating, URL prefix rewriting, and Origin/Referer checks for mutating requests to the mounted applications.

4. **Roles.** Supported roles include `admin`, `clipper`, `producer`, and `affiliate`. Access to `/clip` and `/produce` is restricted by role; affiliate accounts are not authorized for those tool mounts.

5. **AI limitation.** The suggest-agent component may propose clip suggestions from watched channels for human review. It does not, by its documented design, cut or publish content autonomously.

6. **Payment integrity.** Master Class and marketplace payments integrate with Paystack. Webhook signatures are verified using HMAC-SHA512; callback handling re-verifies transactions rather than trusting client query parameters alone; pricing used for initialization is determined server-side.

7. **Marketplace concurrency controls.** Channel listings use a two-stage reservation and finalization approach intended to prevent a unique listing from being sold to two buyers, with an explicit conflict path if races still occur.

8. **Monitoring.** The admin interface provides user management, coarse activity logging from gate middleware, and login attempt records that may include IP geolocation lookups.

9. **Phase-1 limitations.** The repository README expressly defers: real per-tuber YouTube OAuth wiring; granular activity instrumentation beyond coarse route hits; computed reward evaluation against metric events; and multi-worker durable job state for vendored in-process jobs.

10. **Licensing & repository.** Counsel should attach the Petitioner’s authoritative public repository URL and LICENSE. This documentation package was generated from a workspace copy in which a root LICENSE file and git remote were not present; therefore this Statement does **not** assert a specific open-source license.

11. **Non-claims.** This Exhibit does not assert government endorsement, YouTube partnership, guaranteed subscriber or revenue outcomes, or that illustrative demonstration data are production findings of fact.

12. **Verification.** The source tree, middleware modules, payment/marketplace services, and automated test suites are available for independent technical inspection consistent with the Technical White Paper accompanying this Exhibit.

---

## Verification line

> I understand that the foregoing describes the MoneyTuber / Tuber Platform software as implemented in the referenced source tree and supporting documentation, and that demonstration interfaces may include illustrative data not representing audited production results.

_Petitioner / authorized technologist: _______________________ Date: _________

---

## Sub-exhibit checklist

| Sub-exhibit | Item |
|-------------|------|
| A | Executive Brief (`01-EXECUTIVE-BRIEF.md`) |
| B | This Exhibit Statement |
| C | Visual Appendix diagrams + screenshots (`06-VISUAL-APPENDIX.md`) |
| D | Technical White Paper (`03-TECHNICAL-WHITEPAPER.md`) |
| E | Repository URL + LICENSE (Petitioner-supplied) |
| F | Optional: unittest run log / demo recording per `05-DEMONSTRATION-SCRIPT.md` |
