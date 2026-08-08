# Reusable Prompt — Petition Exhibit Documentation Package

Copy the block below into a new Agent chat **with the target project open**. No placeholders to fill — the agent must discover everything from the repo.

---

## Prompt (copy from here)

```text
Build a full PETITION EXHIBIT DOCUMENTATION PACKAGE for the project in this workspace.

## Purpose
The client will showcase this software as an exhibit in a petition. Expert petition writers / consultants / lawyers need end-to-end understanding so they can:
1) accurately describe what the product is and is not,
2) position it for well-founded national importance / public-interest framing,
3) survive hard questions without overclaiming,
4) demonstrate it live or via screenshots.

This is counsel-usable exhibit material — not a marketing brochure and not a pure engineering README.

## Auto-discovery (do this first — do NOT ask me to fill placeholders)
Inspect the open project and infer:
- Product name and plain-English purpose (README, landing copy, package.json, app routes)
- Domains / modules / major capabilities
- Repo URL (git remote)
- License (LICENSE file / README)
- Version / model version strings
- Geography or scope framing (methodology, docs, UI copy)
- Explicit non-claims / limitations already stated in the product
- What is in-scope vs out-of-scope for the current phase
- Whether it is local-first, open-source, formula-based, AI-based, etc.

If something critical cannot be inferred, state your best evidence-based assumption briefly and proceed — only ask me if blocked.

## Required deliverables (create ALL)
Create `docs/petition-exhibit/`:

1. `00-DOCUMENT-INDEX.md` — use case affirmation, reading order, binder lettering, anti-overclaim notes
2. `01-EXECUTIVE-BRIEF.md` — one-sentence summary, problem, plain-language what-it-does, national-importance framing without overreach, “what counsel should not say”, maturity limits, reusable bottom-line paragraph
3. `02-COUNSEL-BRIEFING.md` — positioning statement, non-technical end-to-end flow, national-importance themes, sample petition language, safe vs unsafe claims matrix, hard Q&A, glossary, petition workstream steps
4. `03-TECHNICAL-WHITEPAPER.md` — abstract, principles, architecture (+ diagram), modules, methodology integrity rules, interpretive limits, petition suitability
5. `04-EXHIBIT-STATEMENT.md` — formal numbered attachable statement, verification line, sub-exhibit checklist
6. `05-DEMONSTRATION-SCRIPT.md` — timed live demo with opening disclaimer, screens, “do not say”, screenshot checklist
7. `06-VISUAL-APPENDIX.md` — all diagrams/screenshots with captions and binder insert order
8. `README.md` in that folder pointing to the index and Word file
9. Combined Word file: `docs/<Product-Slug>-Petition-Exhibit-Package.docx` with images embedded (derive slug from product name)

Cross-link any existing SCOPE_OF_WORK / whitepaper rather than duplicating it.

## Required visuals (do NOT ship text-only)
Create `docs/petition-exhibit/figures/` and `screenshots/`.

Diagrams (≥4, brand-aligned to the project — avoid generic purple AI look):
- Fig 1: End-to-end user flow
- Fig 2: System architecture
- Fig 3: Capabilities/modules → outcome
- Fig 4: Integrity safeguards (transparency, honest missing/uncertain data, reproducibility, stated limits)

Screenshots (minimum):
1. Landing / overview
2. Methodology or transparency surface (if exists)
3. Workspace / dashboard
4. Input / editor (validation if any)
5. Results / output (auditability cues: IDs, logs, confidence, etc.)
6. License / open-source / about (if exists)

Screenshot rules:
- Prefer live captures from a running local app.
- If automation limits block SPA state, create clearly bannered ILLUSTRATIVE UI plates that match real product copy and real demo/engine numbers — never invent capabilities.
- Caption LIVE vs ILLUSTRATIVE. Demo/default data is never findings of fact.

## Quality bar
- Accurate to the actual codebase.
- Plain English first; technical depth second.
- Position as inspectable method/capability, not predictive certainty (unless the product truly forecasts).
- Include claims matrix + hard-question Q&A.
- No invented government endorsements, certifications, or dataset provenance.
- Wire images into brief/whitepaper/demo docs, then build the .docx AFTER visuals exist.

## Process order
1. Explore repo → write a short discovered-facts summary (for yourself)
2. Draft docs 00–05
3. Generate diagrams
4. Capture/construct screenshots
5. Draft Visual Appendix (06) and wire images
6. Build combined .docx with embedded images
7. Return: discovered product identity, file tree, reading order, path to the .docx
```

---

## Short variant

```text
Build the full petition-exhibit documentation package for this open project for counsel/consultants. Auto-discover product name, purpose, domains, license, version, scope, and non-claims from the repo — don’t ask me to fill placeholders. Produce docs/petition-exhibit/ (index, executive brief, counsel briefing with safe claims + Q&A + national-importance framing, technical white paper, formal exhibit statement, demo script, visual appendix) with ≥4 diagrams and key screenshots, then embed everything into docs/<Product-Slug>-Petition-Exhibit-Package.docx. Emphasize inspectable method over prediction; label demo data illustrative; no overclaims.
```
