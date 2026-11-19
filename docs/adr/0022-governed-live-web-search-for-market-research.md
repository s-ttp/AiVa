# ADR-0022: Governed live web search for market research

**Status.** Accepted. The owner decided on 2026-10-20 that the market-research use case searches the real
web through AiVa. It is an owner-approved exception to the synthetic-only rule, limited to this use case.

**Owners.**
- Govern: this ADR, the tool policy (`AIPEL-TOOL-006`, `AIPEL-TOOL-007`) and audit.
- Optimize & Inference: the `web.search` adapter, result screening and evidence.
- FinOps & Ledger: reservation, metering and the rate card.
- Scenarios: the `gct-market-intelligence` application.
- Control Center UI: the fifth button on `/live`.

**Context.** The presenter live demo (`/live`, ADR-0021) gains a fifth use case, "Market research
(augmented search)". The presenter asks about competitors' products and services. AiVa then:
1. searches the real web with its governed `web.search` tool;
2. has the CIM trim the results;
3. has DeepSeek V4.1 answer.

The provider is Tavily: 1,000 free searches a month, then $0.008 per search (docs.tavily.com,
documentation/api-credits, fetched 2026-10-20). The owner accepted Tavily explicitly on 2026-10-20 by adding
the key to Space 2 ("tavily key added to space 2"). It is stored as the Space 2 secret `AIVA_SEARCH_API_KEY`.
AiVa reads it from the environment and never logs or prints it. Without it, `web.search` is unavailable: the
use case says so and never falls back to another source.

**Decision.**

*1. An exception to synthetic-only, for this use case only.*
- Real web content may appear in live answers on `/live`.
- Nothing from the real web is committed. Tests and fixtures use synthetic search responses, and a real
  search result is never written to a committed recording. Use case 5 has no replay made from real results.

*2. Who may search.* Policy is evaluated before the tool call (Appendix B):
- **The application must grant the tool.** Only applications whose `allowed_tools` include `web.search` may
  search, today only `gct-market-intelligence` (`AIPEL-TOOL-004`).
- **Confidential and above:** `web.search` sends data outside GCT (`egress: external`). So it is denied when
  the effective classification is confidential, restricted or sovereign-only (`AIPEL-INV-002`,
  `AIPEL-TOOL-005`).
- **DLP findings (`AIPEL-TOOL-006`):** if DLP finds anything in the request (personal data or secrets, in the
  question or its evidence), the search is denied. The question is never rewritten or redacted and then
  searched.
- **The public visitor (`AIPEL-TOOL-007`):** the visitor never searches. Live web search is for signed-in
  presenters on `/live`, so real web content and per-search cost stay off the public demo.
- Both new rules apply to external read tools, where the request's own question leaves AiVa; today that is
  only `web.search`. External write tools keep their approval rule (`GCT-TOOL-005`).

*3. The query.* Only the user's question is sent, after DLP. No conversation history, evidence or system
prompt is sent.

*4. Results are untrusted input.*
- Each result is screened like retrieved text (ADR-0011): for prompt injection and by DLP. A flagged result
  is dropped and counted, never shown, and never redacted and kept.
  - The DLP part is Govern's addition. Real pages often name people, and one such result would otherwise
    raise the run to confidential and send personal data to an external model.
- The remaining results become evidence passages with their URL and title. They are data, never
  instructions.
- No web text goes into spans, run records or reporting tables (Appendix B). Only counts and domains do.

*5. Caching.* The answer cache keeps its scope rules. Search responses are not cached beyond the run. The one
exception is a presenter's answer-quality comparison of the run, which reuses its screened passages from
memory (ADR-0023, see Consequences).

*6. Cost and audit.*
- The search call is a billable stage. It is reserved before the call and metered (FinOps), with a rate card
  that cites its source and date.
- `audit_log` gets `tool.web_search`, recording the pseudonym, the application, the result count and the
  outcome. The question itself is never recorded.

**Consequences.**
- The demo can answer market questions from the live web, without synthetic stand-ins, inside the same
  governed path as every other tool call.
- Tavily receives the DLP-clean question. The owner accepted the provider on 2026-10-20 (see Context). Its
  privacy policy (tavily.com/privacy, fetched 2026-10-20) says:
  - queries have no fixed retention period: they are kept "for the period necessary to provide and improve
    the Services";
  - query data may be used to improve responses or to train models;
  - queries may be shared with third-party search index providers;
  - no zero-retention option is stated, and the policy is written for Tavily's Platform without addressing
    the API separately, so contract terms could differ.
- So every search query is treated as disclosed to a third party. That is why any DLP finding stops the
  search (`AIPEL-TOOL-006`) and confidential and above are denied. Presenters ask public-information
  questions only, and the `/live` market-research button shows a help line saying so.
- A presenter's answer-quality comparison (ADR-0023) gives the frontier reference the same screened web
  passages the served model received, so both answers are judged on the same evidence.
  - The passages are held only in the gateway's process memory, keyed by run id, for at most 30 minutes,
    with a small cap on the runs held.
  - They are never written to disk, Redis, run records, spans or logs, so no web text is persisted.
  - If they are gone, the comparison runs without them and says so. It never searches again.
- The exception is narrow: one tool, one application, presenters only, live only. Any wider use of real web
  content needs its own decision.
