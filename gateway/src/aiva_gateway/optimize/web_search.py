"""Governed live web search (ADR-0022): the `web.search` tool, through Tavily.

Only the presenter's live market-research use case searches, and only after policy allows the tool call and
the budget reservation covers it (the orchestrator's retrieval stage). This module is the provider adapter
and the result handling:
- One basic search (1 Tavily credit), at most `MAX_RESULTS` results with their page text, each part truncated
  to a byte limit, so the passages a search can add have a known upper bound for the reservation.
- One call per `search`; the orchestrator retries once and meters each call (`finops.tools`).
- The key is read from `AIVA_SEARCH_API_KEY` and sent only as the request's bearer token. It is never logged,
  and an error message never carries it.
- Results are untrusted: `screen` drops any result with a DLP finding or a prompt-injection indicator, and
  the rest become evidence passages `WEB-<n>#1` with a citation header (`[WEB-<n> §1] <title> - <domain>`,
  the knowledge-base format the CIM is trained on) and their URL. Spans, records and reports get counts and
  domains only, never web text.
- Each rendered passage is at most `PASSAGE_MAX_BYTES`, so the reservation, made before the search, bounds
  the input the passages can add (`evidence_bound`).
- The CIM was trained on knowledge-base passages, not web results, and may omit every one of them. So every
  screened web passage is kept in the package whatever the CIM selects (the orchestrator's recall floor), and
  together they take at most `WEB_BUDGET_SHARE` of the profile's context budget (`passage_token_cap`), so the
  package still fits it. Each result's query-relevant extract comes first, then its page text.

Without a key, or on a gateway that does not run live inference, the tool is unavailable and nothing else
answers in its place. Tests use synthetic responses through a mocked transport; no real search result is
ever recorded.
"""

import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx

from aiva_gateway.config import Settings
from aiva_gateway.contracts.cim import CimEvidence
from aiva_gateway.core.enums import Provenance
from aiva_gateway.inference.tokens import PER_MESSAGE_OVERHEAD_TOKENS
from aiva_gateway.optimize.cim import EVIDENCE_PREAMBLE, evidence_block
from aiva_gateway.pipeline.interfaces import DlpScanner

TOOL_ID = "web.search"
PROVIDER = "tavily"
TAVILY_URL = "https://api.tavily.com/search"
MAX_RESULTS = 5
SEARCH_ATTEMPTS = 2  # one call plus one retry; a timed-out call may have been billed
CREDITS_PER_SEARCH = 1  # a basic search
# The worst-case credits a run reserves for: every attempt billed.
PLANNED_UNITS = SEARCH_ATTEMPTS * CREDITS_PER_SEARCH
# Per result, in UTF-8 bytes. A rendered passage (`evidence_block`) never exceeds PASSAGE_MAX_BYTES: its
# content is clipped further if the header, URL and escaping would take it over.
TITLE_MAX_BYTES = 300
URL_MAX_BYTES = 500
CONTENT_MAX_BYTES = 2000
PASSAGE_MAX_BYTES = 3600
# The share of a profile's context budget (`target_budget_tokens`) that the k web passages may take together.
WEB_BUDGET_SHARE = 0.5
LIVE_MODES = ("live", "record")
NO_KEY = "No search key configured"
NOT_LIVE = "Web search runs on a live gateway only"
# Runs that never search, whatever the gateway: a replay has no recording made from real results, and a
# recording run would write real web text into a recording (ADR-0022).
NO_SEARCH_MODES = {
    "replay": "Web search runs on live runs only; this run replays recorded exchanges",
    "record": "Web search is off while recording: a recording never holds real web results",
}


@dataclass(frozen=True)
class WebResult:
    title: str
    url: str
    content: str


@dataclass(frozen=True)
class SearchCall:
    """One provider call. `billed_units` is what the provider billed or may have billed (None: nothing)."""

    status: Literal["ok", "failed"]
    results: tuple[WebResult, ...] = ()
    billed_units: int | None = None
    units_reported: bool = False
    error_code: str | None = None
    retryable: bool = False


def charge_provenance(execution_mode: str, call: SearchCall) -> Provenance:
    """A billed call's label (`finops.tools`): `measured` when Tavily reported the credits it billed,
    `estimated` when they were inferred (a timeout, no usage block); `simulated` on a simulated run, which
    only a test's mocked provider reaches."""
    if execution_mode == "simulated":
        return Provenance.SIMULATED
    return Provenance.MEASURED if call.units_reported else Provenance.ESTIMATED


class WebSearch(Protocol):
    provider: str

    @property
    def available(self) -> bool: ...

    @property
    def reason(self) -> str | None: ...

    async def search(self, query: str, *, max_results: int) -> SearchCall: ...


def _clip(text: str, limit: int) -> str:
    """At most `limit` UTF-8 bytes, never splitting a character."""
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore").strip()


class TavilySearch:
    provider = PROVIDER

    def __init__(self, *, api_key: str, http: httpx.AsyncClient, timeout_seconds: float) -> None:
        self._key = api_key
        self._http = http
        self._timeout = timeout_seconds

    @property
    def available(self) -> bool:
        return True

    @property
    def reason(self) -> str | None:
        return None

    async def search(self, query: str, *, max_results: int) -> SearchCall:
        body = {
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_answer": False,
            "include_images": False,
            "include_raw_content": "text",
            "include_usage": True,
        }
        try:
            response = await self._http.post(
                TAVILY_URL,
                json=body,
                headers={"Authorization": f"Bearer {self._key}"},
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            # Tavily may have run (and billed) the search: count it, labelled as an estimate.
            return SearchCall(
                "failed", billed_units=CREDITS_PER_SEARCH, error_code="SEARCH_TIMEOUT", retryable=True
            )
        except httpx.HTTPError:
            return SearchCall("failed", error_code="SEARCH_CONNECTION_ERROR", retryable=True)
        if response.status_code != 200:
            retryable = response.status_code == 429 or response.status_code >= 500
            return SearchCall("failed", error_code=f"SEARCH_HTTP_{response.status_code}", retryable=retryable)
        try:
            payload = response.json()
        except ValueError:
            return SearchCall("failed", billed_units=CREDITS_PER_SEARCH, error_code="SEARCH_INVALID_RESPONSE")
        reported = (payload.get("usage") or {}).get("credits")
        units = reported if isinstance(reported, int) and reported >= 0 else CREDITS_PER_SEARCH
        results = tuple(
            WebResult(
                title=_clip(str(item.get("title") or ""), TITLE_MAX_BYTES),
                url=_clip(str(item.get("url") or ""), URL_MAX_BYTES),
                content=_clip(_text(item), CONTENT_MAX_BYTES),
            )
            for item in (payload.get("results") or [])[:max_results]
            if isinstance(item, dict) and item.get("url")
        )
        return SearchCall("ok", results=results, billed_units=units, units_reported=units == reported)


def _text(item: dict[str, object]) -> str:
    """Tavily's extract of the page most related to the query, then the page text."""
    parts = (str(item.get("content") or "").strip(), str(item.get("raw_content") or "").strip())
    return "\n".join(part for part in parts if part)


@dataclass
class UnavailableSearch:
    unavailable_reason: str
    provider: str = PROVIDER

    @property
    def available(self) -> bool:
        return False

    @property
    def reason(self) -> str | None:
        return self.unavailable_reason

    async def search(self, query: str, *, max_results: int) -> SearchCall:
        return SearchCall("failed", error_code="SEARCH_UNAVAILABLE")


def build_web_search(settings: Settings, http: httpx.AsyncClient | None) -> WebSearch:
    if settings.search_api_key is None:
        return UnavailableSearch(NO_KEY)
    if settings.inference_mode not in LIVE_MODES or http is None:
        return UnavailableSearch(NOT_LIVE)
    return TavilySearch(
        api_key=settings.search_api_key.get_secret_value(),
        http=http,
        timeout_seconds=settings.search_timeout_seconds,
    )


def evidence_bound(k: int) -> int:
    """Upper bound, in billed-token units (UTF-8 bytes), on the input `k` web passages can add to a payload:
    the evidence message they may open and each rendered passage."""
    message = PER_MESSAGE_OVERHEAD_TOKENS + len(EVIDENCE_PREAMBLE.encode("utf-8")) + 1
    return message + k * (PASSAGE_MAX_BYTES + 1)


@dataclass
class Screened:
    passages: list[CimEvidence] = field(default_factory=list)
    sources: list[tuple[str, str]] = field(default_factory=list)  # (title, url) of each passage kept
    dropped_injection: int = 0
    dropped_dlp: int = 0


def passage_token_cap(target_budget_tokens: int, k: int) -> int:
    """The estimated tokens one of k web passages may take: together at most `WEB_BUDGET_SHARE` of the
    profile's budget, so a package that keeps all of them still fits the Context Guard's token budget."""
    return int(target_budget_tokens * WEB_BUDGET_SHARE) // max(k, 1)


def passage(
    evidence_id: str, document_id: str, result: WebResult, *, max_tokens: int | None = None
) -> CimEvidence:
    """A result as an evidence passage: its citation header, its URL, then its text, at most
    `PASSAGE_MAX_BYTES` once wrapped as evidence and, with `max_tokens`, at most that many estimated tokens
    (`estimate_tokens`, four characters each). Only the text is shortened."""
    host = domain(result.url) or "web"
    header = f"[{document_id} §1] {' '.join(result.title.split()) or host} - {host}"
    content = result.content
    while True:
        text = f"{header}\nSource: {result.url}\n{content}"
        over_chars = len(text) - 4 * max_tokens if max_tokens is not None else 0
        if over_chars > 0 and content:
            content = content[: max(0, len(content) - over_chars)].rstrip()
            continue
        excess = len(evidence_block(evidence_id, text).encode("utf-8")) - PASSAGE_MAX_BYTES
        if excess <= 0 or not content:
            return CimEvidence(id=evidence_id, text=text)
        content = _clip(content, len(content.encode("utf-8")) - excess)


def screen(
    results: Sequence[WebResult],
    dlp: DlpScanner,
    *,
    taken_ids: set[str],
    max_tokens: int | None = None,
) -> Screened:
    """Drop every result with a prompt-injection indicator or a DLP finding (never redacted and kept).
    The rest become evidence passages `WEB-<n>#1` (`passage`, at most `max_tokens` each), numbered past any
    id the run already uses."""
    screened = Screened()
    n = 0
    for result in results:
        number = n + 1
        while f"WEB-{number}#1" in taken_ids:
            number += 1
        candidate = passage(f"WEB-{number}#1", f"WEB-{number}", result, max_tokens=max_tokens)
        scanned = dlp.scan(candidate.text, detect_injection=True)
        if scanned.injection_indicators:
            screened.dropped_injection += 1
        elif scanned.findings:
            screened.dropped_dlp += 1
        else:
            n = number
            screened.passages.append(candidate)
            screened.sources.append((result.title, result.url))
    return screened


# A presenter's comparison (ADR-0023) gives the frontier reference the same screened web passages the served
# model received, held in this process's memory only and only briefly.
WEB_EVIDENCE_TTL_SECONDS = 30 * 60
WEB_EVIDENCE_RUNS = 50


class WebEvidenceMemory:
    """The screened web passages of the latest runs, in this process's memory only, for a presenter's
    answer-quality comparison (ADR-0023 amendment). Never written to disk, Redis, records, spans or logs:
    gone after `ttl_seconds`, beyond the last `capacity` runs, or when the gateway restarts."""

    def __init__(
        self,
        *,
        ttl_seconds: float = WEB_EVIDENCE_TTL_SECONDS,
        capacity: int = WEB_EVIDENCE_RUNS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl, self._capacity, self._clock = ttl_seconds, capacity, clock
        self._runs: OrderedDict[str, tuple[float, tuple[CimEvidence, ...]]] = OrderedDict()

    def keep(self, run_id: str, passages: Sequence[CimEvidence]) -> None:
        self._runs[run_id] = (self._clock() + self._ttl, tuple(passages))
        self._runs.move_to_end(run_id)
        while len(self._runs) > self._capacity:
            self._runs.popitem(last=False)

    def get(self, run_id: str) -> tuple[CimEvidence, ...] | None:
        held = self._runs.get(run_id)
        if held is None:
            return None
        if held[0] <= self._clock():
            del self._runs[run_id]
            return None
        return held[1]


def domain(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")
