# ADR-0026: An OpenAI-compatible API on the gateway

**Status.** Proposed. Owner decision 2026-10-26 to offer one; this ADR records the design. Govern reviews
authentication, classification and policy.

**Owners.** Optimize & Inference:
- `api/openai_compat.py`;
- `contracts/openai_compat.py`;
- the `RunContext.required_model` check in `pipeline/orchestrator.py`, a shared file.

Govern reviews the auth and governance-field mapping.

**Context.**
- **The gap.** Any OpenAI SDK, framework or tool can talk to an endpoint that speaks
  `POST /v1/chat/completions`. AiVa's own API, `POST /v1/ai/chat`, needs a custom client.
- **The goal.** An OpenAI-compatible surface lets those clients use AiVa unchanged.
- **The risk.** The surface becomes a side door around governance.
- **Credentials reach the gateway as they are:**
  - On Space 2, a standard `Authorization: Bearer <credential>` passes HF's edge and the front door's nginx,
    which forwards it as `X-AiVa-Authorization` (ADR-0019, ADR-0025; checked at $0 on 2026-10-26).
  - An SDK's `api_key=<credential>` therefore works with no custom header.

**Decision.**

**1. Two routes, beside the native API, on the same pipeline.**
- **Routes:**
  - `POST /v1/chat/completions`;
  - `GET /v1/models` and `GET /v1/models/{id}`.
- **Base URL:** `https://<owner>-gct-rlcd-gateway.hf.space/gateway/v1`.
- **One executor.** A request is mapped onto a `ChatRequest` and run by `execute_chat`, which the native route
  now shares: identity, the Idempotency-Key store, approvals, the orchestrator and persistence. So the full
  chain applies with no bypass:
  - policy (request stage, then at every egress) and DLP;
  - the quota reservation and RLCD;
  - the CIM and the Context Guard;
  - assurance (output DLP, citations), metering, tracing and the run record.
- **Invariants.** `gateway/tests/invariants/test_openai_compat_invariants.py` re-proves the rules through
  this route:
  - confidential data takes the sovereign path;
  - the sovereign path never falls back;
  - a policy block is a block with no usage;
  - policy is evaluated first;
  - no prompt text reaches records or spans.

**2. `model`: RLCD chooses, or the caller requires.**
- **`aiva-auto` or omitted:** RLCD chooses inside the policy-eligible set, as on the native API.
- **A registry id:** the model is *required*. It is set as `RunContext.required_model`, not as the native
  `routing.preferred_model` hint:
  - After policy and the path decision, the eligible set is narrowed to that model alone, so RLCD chooses
    inside a set of one.
  - If the model is not in the eligible set, the run is blocked there: before the CIM, the reservation or any
    model call. The error is 403 `model_not_allowed`, "Model not allowed by AI policy."
  - It is never substituted.
  - RLCD can never widen the set: a required external model never moves a request off the sovereign path.
  - It also stops a coding or Q&A request from taking the in-house-only local fast path when the caller
    required an external model.
- **The required model joins the response-cache key,** so it is only ever served its own answers.
- **Unknown ids, the CIM and the grader** are 404 `model_not_found`, with no run.
- **Why a context field, not native `pinned` mode.** The native `routing.mode: pinned` stays a hint. Invariant
  `test_confidential_never_reaches_cim_or_external` sends a pinned Claude request with confidential data and
  expects the sovereign model, and the native API keeps that behaviour.
- **`GET /v1/models`** lists:
  - `aiva-auto`;
  - the upstream models the application is granted that the request-stage policy leaves for a request at the
    application's own classification floor. A request's own classification can narrow them further: a
    confidential request takes the sovereign path. The fraud investigator, whose floor is confidential, sees
    only the sovereign model.

**3. Governance fields: headers, else metadata, else the application.**

| field | header | `metadata` key |
|---|---|---|
| declared classification | `X-AiVa-Data-Classification` | `aiva_data_classification` |
| quality tier (default `standard`) | `X-AiVa-Quality-Tier` | `aiva_quality_tier` |
| routing tags (comma-separated) | `X-AiVa-Routing-Tags` | `aiva_routing_tags` |
| task hint | `X-AiVa-Task` | `aiva_task` |
| scenario (its context profile) | `X-AiVa-Scenario` | `aiva_scenario` |
| application, for a user token | `X-AiVa-Application` | `aiva_application` |
| simulated source, testers only | `X-AiVa-Simulated-Source-IP` | `aiva_simulated_source_ip` |

- **The application's own floor always applies,** and the effective classification is still the most
  restrictive of declared, floor, DLP and RLCD. Declaring `public` cannot lower an `internal` floor
  (tested).
- **Other metadata keys** are accepted and never stored.
- **Who the run executes as:**
  - An application credential runs as its own application. Naming another is 403.
  - A user token (tester, visitor) must name its application in `X-AiVa-Application`, and may simulate its
    source as the Test Console does. The facade then builds the same `test_profile` the console sends, and
    `resolve_run_identity` checks it as before: testers only, where simulated ingress is allowed.
  - An application credential may not simulate anything (403).
  - Public visitors may use this route exactly as they use `POST /v1/ai/chat`: the same console run, in
    replay by default (ADR-0019). The route matrix in `test_public_demo.py` classifies it as open.

**4. Parameters: refuse what cannot be honoured; accept, and say so, what AiVa sets itself.**
- **`max_completion_tokens`, or the older `max_tokens`,** becomes `max_output_tokens`. The newer name wins
  if both are sent; the default is 1,024.
- **Refused with a 400 `unsupported_parameter` naming the parameter:**
  - `tools`, `tool_choice` (other than `none`), `functions` and `function_call`, plus tool or function
    messages. Tools go through ADR-0012 and are out of scope here.
  - `n > 1`, `logprobs`, `top_logprobs`, `stop`, `logit_bias`, a `response_format` other than `text`,
    `audio`, non-text `modalities`, `prediction` and `web_search_options`;
  - any parameter the API does not know.
  - Images, audio and files in message content are 400 `unsupported_content`.
- **Accepted, not applied, and listed in `aiva.ignored_parameters`:**
  - `temperature`, `top_p`, `presence_penalty`, `frequency_penalty`, `seed`, `reasoning_effort`,
    `service_tier` and `store`.
  - AiVa sets the answer temperature per model and grounding (ADR-0009, 2026-10-22). Letting a client
    override it would change the route's quality guarantees, and the shipped replay recordings with them.
  - `user` is accepted and never stored.
- **Message mapping:**
  - `developer` messages are `system` messages.
  - Text content parts are joined.

**5. Response.**
- **A `chat.completion`:**
  - `id` is the run id.
  - `model` is the model that served.
  - `usage` holds the answering model's prompt, completion, cached and reasoning tokens.
- **An `aiva` object**, which OpenAI SDKs keep as an extra field:
  - `run_id`, `trace_id`, `status` and `execution_path`;
  - the selected model, and `rlcd_version`: the router that chose it (the RLCD model's version, or
    `heuristic-v0`), null on a cache return, so per-request routing is visible without a trace lookup
    (2026-10-27);
  - the policy action, effective classification and matched rules;
  - the CIM state (§22.8 copy), the cache hit and the output DLP result;
  - provenance;
  - cost in micro-USD, with its provenance, and the GPU estimate;
  - `ignored_parameters` and the idempotency key.
  - The full record stays at `GET /v1/runs/{run_id}`.
- **Output withheld.** When output assurance withholds the answer, the response is a completion with
  `finish_reason: content_filter` and null content. That is how OpenAI reports a filtered completion.
- **Errors use OpenAI's shape**, `{"error": {"message", "type", "param", "code"}}`, with an `aiva` object
  holding the run id, rule ids, remediation and any approval id. It applies to every failure on these
  routes, including authentication and validation, which happen before the route.

| outcome | status | `code` |
|---|---|---|
| blocked by policy | 403 | `policy_blocked`, "Blocked by AI policy." |
| required model not eligible | 403 | `model_not_allowed` |
| approval required | 403 | `approval_required` (`aiva.approval_id`; resume through the native API) |
| budget or quota | 429 | the native code, lower-cased |
| dependency unavailable (fails closed) | 503 | e.g. `sovereign_endpoint_unavailable` |
| endpoint asleep, for a run that never wakes one (ADR-0019, ADR-0027 §3) | 503 | `live_demo_unavailable`, `aiva.details.reason` `endpoint_cold` |
| any other run the gateway finished without an answer | its status, else 503 | the native code, lower-cased |
| unknown model | 404 | `model_not_found` |
| unsupported or invalid parameter | 400 | `unsupported_parameter`, `unsupported_content`, `invalid_value` |

- **Never 502 or 504** for a run the gateway finished: Space 2's front door replaces those with its own
  "starting" page (`gateway-proxy.sh`), so the caller would never see the refusal. Found live on
  2026-10-26. The native error's `details` are kept in `aiva.details`.
- **A refusal a retry cannot change** (`aiva.retryable` false) carries `x-should-retry: false`, which OpenAI
  SDKs honour. Without it an SDK resends a 5xx twice, and each resend is another refused run.

**6. Streaming is buffered, and says so.**
- **Why.** Output DLP and the assurance checks need the whole answer before any of it is released.
- **So,** with `stream: true`:
  - the run completes first;
  - then the answer is sent as `chat.completion.chunk` server-sent events: the role, the content in pieces of
    about 120 characters, a final chunk with the finish reason and the `aiva` object, the usage chunk when
    `stream_options.include_usage` is set, and `[DONE]`.
- **Clients see no time-to-first-token benefit.** A blocked or failed run is an ordinary JSON error, never a
  stream.

**7. Idempotency.**
- **A client's `Idempotency-Key` is honoured** as on the native API: an identical retry replays the stored
  completion, and a different body is a 409.
- **The facade's keys are prefixed in the store,** so an OpenAI-shaped replay never answers a native request.
- **OpenAI SDKs send no key.** One is generated (`gen-…`), so every run is still recorded under a key and the
  store's fail-closed rule applies.
- **Stated plainly: a generated key cannot de-duplicate an SDK's own retries,** because each retry gets a new
  one. A client that needs that passes its own key (`extra_headers={"Idempotency-Key": …}`).

**Example.** With a `gct-api-sandbox` key, the application for SDK and API use, which takes key calls from the
internet (ADR-0027 §3). `gateway/tests/integration/test_openai_sdk.py` runs this flow in-process, behind a
simulated Space 2 front door:

```python
from openai import OpenAI

# api_key: a gct-api-sandbox key, issued from the admin console (ADR-0027)
client = OpenAI(base_url="https://<owner>-gct-rlcd-gateway.hf.space/gateway/v1", api_key="aiva_…")
completion = client.chat.completions.create(
    model="aiva-auto",  # or a model id from client.models.list()
    messages=[{"role": "user", "content": "What does the Traveller 10 roaming bundle include?"}],
    extra_headers={"X-AiVa-Quality-Tier": "high"},
)
print(completion.choices[0].message.content, completion.model_extra["aiva"]["run_id"])
```

**Consequences.**
- **Network trust on Space 2.** Behind the front door the observed source address is null, by design
  (ADR-0019), and `AIPEL-NET-001` denies a request whose source matches none of its application's trusted
  networks. The owner's answer, 2026-10-26, is ADR-0027 §3: an application grant flag, `internet_callers`.
  This API changes nothing for it. So on Space 2:
  - **a key for `gct-api-sandbox`** (internal floor, $5 a month cap), **`gct-coding-assistant` or
    `gct-knowledge-portal`** passes `AIPEL-NET-001` as an internet caller. Every other rule still applies.
  - **any other application's key is blocked by policy**, through this API as through the native one, with
    the OpenAI-shaped 403 `policy_blocked` naming `AIPEL-NET-001` (tested through the simulated front door);
  - **a tester token works** with `X-AiVa-Application` and the Test Console's simulated source
    (`192.0.2.10`). The flag admits keys only, never a user or a simulated identity.
- **Any OpenAI-speaking client becomes an AiVa client.** Every request is governed, routed, metered and
  traced like a native one.
- **Not offered through this API:**
  - tool calling;
  - sampling control;
  - multiple choices, log probabilities and stop sequences;
  - multimodal input;
  - true token streaming.
  - Clients that need these get a clear 400. Tools remain on the native agent path (ADR-0012).
- **Approval-required runs** are reported here, but resumed through the native API (`approval_id`).
- **No contract change to the native API.** The shared executor and the `required_model` field are
  internal. The shipped replay recordings are unchanged: the facade adds routes, not bodies.
