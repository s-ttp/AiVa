"""Record and replay provider HTTP exchanges (PLAN §4.7: CI runs on recorded responses labelled `replayed`).

Recordings sit below the provider SDKs, at the HTTP transport, so replay exercises the same request
building and stream parsing as a live call. A recording stores the request (method, URL without query
credentials, JSON body) and the response (status, content type, body text). Request and response headers
are never stored, so credentials cannot reach a recording. Replay never falls through to the network: a
request without a recording raises `ReplayMissError`.

The Anthropic and OpenAI SDKs use `httpx2`, the Google GenAI SDK uses `httpx`; both expose the same
transport API, so each gets a thin transport class over the shared `RecordingStore`.
"""

import json
import os
import re
import urllib.request
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx
import httpx2

from aiva_gateway.core.canonical import canonical_json, sha256_hex
from aiva_gateway.inference.base import ReplayMissError

_SECRET_QUERY_KEYS = frozenset({"key", "api_key", "apikey", "access_token", "token"})

# An Inference Endpoint's hostname names a private deployment. Recordings and fingerprints keep only the
# domain, so committed fixtures never carry it; replay then calls the redacted host it reads back.
_HF_ENDPOINT_HOST = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)*\.endpoints\.huggingface\.cloud")
_HF_ENDPOINTS_API_HOST = "api.endpoints.huggingface.cloud"
REDACTED_ENDPOINT_HOST = "redacted.endpoints.huggingface.cloud"


def _clean_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _SECRET_QUERY_KEYS]
    suffix = f"?{urlencode(sorted(query))}" if query else ""
    netloc = parts.netloc
    host = parts.hostname or ""
    if host != _HF_ENDPOINTS_API_HOST and _HF_ENDPOINT_HOST.fullmatch(host):
        netloc = REDACTED_ENDPOINT_HOST + (f":{parts.port}" if parts.port else "")
    return f"{parts.scheme}://{netloc}{parts.path}{suffix}"


def _parse_body(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return body.decode("utf-8", errors="replace")


@dataclass(frozen=True)
class Recording:
    method: str
    url: str
    request_body: Any
    status_code: int
    content_type: str
    response_body: str

    def to_json(self) -> dict[str, Any]:
        return {
            "request": {"method": self.method, "url": self.url, "body": self.request_body},
            "response": {
                "status_code": self.status_code,
                "content_type": self.content_type,
                "body": self.response_body,
            },
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Recording":
        request, response = data["request"], data["response"]
        return cls(
            method=request["method"],
            url=request["url"],
            request_body=request["body"],
            status_code=int(response["status_code"]),
            content_type=str(response["content_type"]),
            response_body=str(response["body"]),
        )


class RecordingStore:
    """One JSON file per exchange, named `<label>-<fingerprint>.json` under `directory`."""

    def __init__(self, directory: Path, *, label: str) -> None:
        self.directory = directory
        self.label = label

    @staticmethod
    def fingerprint(method: str, url: str, body: bytes) -> str:
        return sha256_hex(
            canonical_json({"method": method.upper(), "url": _clean_url(url), "body": _parse_body(body)})
        )[:24]

    def path_for(self, fingerprint: str) -> Path:
        return self.directory / f"{self.label}-{fingerprint}.json"

    def load(self, method: str, url: str, body: bytes) -> Recording:
        fingerprint = self.fingerprint(method, url, body)
        path = self.path_for(fingerprint)
        if not path.is_file():
            raise ReplayMissError(f"no recording {path.name} for {method} {_clean_url(url)}")
        return Recording.from_json(json.loads(path.read_text(encoding="utf-8")))

    def save(
        self, method: str, url: str, body: bytes, status_code: int, content_type: str, text: str
    ) -> Path:
        recording = Recording(
            method=method.upper(),
            url=_clean_url(url),
            request_body=_parse_body(body),
            status_code=status_code,
            content_type=content_type,
            response_body=text,
        )
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.path_for(self.fingerprint(method, url, body))
        path.write_text(
            json.dumps(recording.to_json(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        return path


_RECORDING_NAME = re.compile(r"(?P<label>[a-z0-9][a-z0-9.-]{0,127})-(?P<fingerprint>[0-9a-f]{24})\.json")
MAX_RECORDING_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class StoredRecording:
    name: str
    label: str
    fingerprint: str
    recording: Recording


def read_recordings(directory: Path, *, secrets: Iterable[str]) -> tuple[list[StoredRecording], int]:
    """Every recording in `directory`, scrubbed as a committed fixture is, and how many files were withheld.

    Each file is re-read through `Recording` (request method, URL, body; response status, content type,
    body) and its URL cleaned again, so nothing else a file might hold is returned. A file that is not a
    recording, is over 4 MB, or contains any of `secrets` is withheld.
    """
    values = [s for s in secrets if s]
    found: list[StoredRecording] = []
    withheld = 0
    for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
        match = _RECORDING_NAME.fullmatch(path.name)
        try:
            if match is None or not path.is_file() or path.stat().st_size > MAX_RECORDING_BYTES:
                raise ValueError(path.name)
            stored = Recording.from_json(json.loads(path.read_text(encoding="utf-8")))
            recording = Recording(
                method=stored.method,
                url=_clean_url(stored.url),
                request_body=stored.request_body,
                status_code=stored.status_code,
                content_type=stored.content_type,
                response_body=stored.response_body,
            )
            text = json.dumps(recording.to_json(), ensure_ascii=False)
            if any(value in text for value in values):
                raise ValueError(path.name)
        except (OSError, ValueError, KeyError, TypeError):
            withheld += 1
            continue
        found.append(StoredRecording(path.name, match["label"], match["fingerprint"], recording))
    return found, withheld


async def _tee(stream: AsyncIterator[bytes], on_complete: Callable[[bytes], None]) -> AsyncIterator[bytes]:
    chunks: list[bytes] = []
    async for chunk in stream:
        chunks.append(chunk)
        yield chunk
    on_complete(b"".join(chunks))


def env_proxy() -> str | None:
    """HTTPS proxy from the environment; explicit transports do not pick it up on their own."""
    return urllib.request.getproxies().get("https") or os.environ.get("HTTPS_PROXY")


# --- httpx2 (Anthropic and OpenAI SDKs) ------------------------------------------------------------


class _Httpx2Body(httpx2.AsyncByteStream):
    def __init__(self, source: AsyncIterator[bytes]) -> None:
        self._source = source

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._source:
            yield chunk


class Httpx2ReplayTransport(httpx2.AsyncBaseTransport):
    def __init__(self, store: RecordingStore) -> None:
        self.store = store

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        recording = self.store.load(request.method, str(request.url), await request.aread())
        return httpx2.Response(
            recording.status_code,
            headers={"content-type": recording.content_type},
            content=recording.response_body.encode("utf-8"),
            request=request,
        )


class Httpx2RecordingTransport(httpx2.AsyncBaseTransport):
    def __init__(self, store: RecordingStore, inner: httpx2.AsyncBaseTransport | None = None) -> None:
        self.store = store
        self.inner = inner or httpx2.AsyncHTTPTransport(proxy=env_proxy(), trust_env=True)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        body = await request.aread()
        # Ask for an uncompressed body so the recording is plain text.
        request.headers["accept-encoding"] = "identity"
        response = await self.inner.handle_async_request(request)
        content_type = response.headers.get("content-type", "application/json")

        def save(data: bytes) -> None:
            text = data.decode("utf-8", errors="replace")
            self.store.save(request.method, str(request.url), body, response.status_code, content_type, text)

        stream = response.stream
        assert isinstance(stream, httpx2.AsyncByteStream)
        return httpx2.Response(
            response.status_code,
            headers=response.headers,
            stream=_Httpx2Body(_tee(stream.__aiter__(), save)),
            request=request,
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.inner.aclose()


# --- httpx (Google GenAI SDK) ----------------------------------------------------------------------


class _HttpxBody(httpx.AsyncByteStream):
    def __init__(self, source: AsyncIterator[bytes]) -> None:
        self._source = source

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._source:
            yield chunk


class HttpxReplayTransport(httpx.AsyncBaseTransport):
    def __init__(self, store: RecordingStore) -> None:
        self.store = store

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        recording = self.store.load(request.method, str(request.url), await request.aread())
        return httpx.Response(
            recording.status_code,
            headers={"content-type": recording.content_type},
            content=recording.response_body.encode("utf-8"),
            request=request,
        )


class HttpxRecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self, store: RecordingStore, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self.store = store
        self.inner = inner or httpx.AsyncHTTPTransport(proxy=env_proxy(), trust_env=True)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        request.headers["accept-encoding"] = "identity"
        response = await self.inner.handle_async_request(request)
        content_type = response.headers.get("content-type", "application/json")

        def save(data: bytes) -> None:
            text = data.decode("utf-8", errors="replace")
            self.store.save(request.method, str(request.url), body, response.status_code, content_type, text)

        stream = response.stream
        assert isinstance(stream, httpx.AsyncByteStream)
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_HttpxBody(_tee(stream.__aiter__(), save)),
            request=request,
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.inner.aclose()
