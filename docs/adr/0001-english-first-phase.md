# ADR-0001: English-only first phase

**Context.** LLD §2 and §22.4 require bilingual Arabic/English operation, RTL layout and scenario 7
(Arabic–English Customer Support). The owner decided to deliver English first.

**Decision.**
- Build and accept the POC in English. Scenario 7 stays in the catalogue, marked deferred.
- Keep the system Arabic-ready at low cost: UI strings via i18n keys, CSS logical properties,
  `dir`/`lang` driven by locale, the `language` classification dimension recorded on every run.
- RLCD uses `answerdotai/ModernBERT-base` as specified (English-centric). For the Arabic phase the planned
  swap is mmBERT (same architecture, multilingual), validated by benchmark.

**Consequences.** §22.10 bilingual acceptance criteria are deferred; English acceptance criteria apply now.
Presidio runs with the English NLP model plus pattern-based telecom recognizers.
