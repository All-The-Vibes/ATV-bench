"""Strict stdlib backend for OpenAI-compatible ``/v1/responses`` endpoints.

The endpoint origin is operator-configured.  This module does not assume a
specific provider hostname and does not retain broker-held bearer credentials.
One ``create`` or ``stream`` invocation performs exactly one HTTP request; no
redirects or retries are followed.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import math
import re
import socket
import ssl
import threading
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, NoReturn
from urllib.parse import urlsplit

from atv_bench.security.gateway import ProviderUsage
from atv_bench.security.responses_gateway import (
    ResponsesBackendError,
    ResponsesBackendRequest,
    ResponsesBackendResponse,
    ResponsesCancelled,
    ResponsesStreamChunk,
)


_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_HEADER_VALUE_RE = re.compile(r"^[\x20-\x7e]*$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/=-]{0,255}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TERMINAL_EVENTS = frozenset(
    {"response.completed", "response.incomplete", "response.failed"}
)
_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "cookie",
        "proxy-authorization",
        "set-cookie",
    }
)
_READ_CHUNK_BYTES = 64 * 1024
_CANCELLATION_POLL_SECONDS = 0.02


class OpenAIResponsesBackendErrorCode(str, Enum):
    """Stable, sanitized failure categories exposed by the provider adapter."""

    INVALID_REQUEST = "invalid_request"
    INVALID_CREDENTIAL = "invalid_credential"
    TRANSPORT = "transport"
    TLS = "tls"
    TIMEOUT = "timeout"
    HTTP_STATUS = "http_status"
    INVALID_HEADERS = "invalid_headers"
    INVALID_CONTENT_TYPE = "invalid_content_type"
    RESPONSE_TOO_LARGE = "response_too_large"
    INVALID_JSON = "invalid_json"
    INVALID_RESPONSE = "invalid_response"
    INVALID_SSE = "invalid_sse"
    PROVIDER_ERROR = "provider_error"


class OpenAIResponsesBackendError(ResponsesBackendError):
    """Typed provider failure whose message never includes provider content."""

    def __init__(
        self,
        *,
        code: OpenAIResponsesBackendErrorCode,
        retryable: bool = False,
        request_id: str | None = None,
        status_code: int | None = None,
    ):
        super().__init__(retryable=retryable, request_id=request_id)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class OpenAIResponsesBackendConfig:
    """Connection policy for one operator-controlled Responses endpoint."""

    base_url: str
    provider_id: str
    connect_timeout_seconds: float = 10.0
    write_timeout_seconds: float = 30.0
    read_timeout_seconds: float = 120.0
    max_request_bytes: int = 1_048_576
    max_response_bytes: int = 8_388_608
    max_stream_event_bytes: int = 1_048_576
    max_header_bytes: int = 32_768
    max_header_count: int = 128
    max_credential_bytes: int = 16_384
    request_id_header_names: tuple[str, ...] = ("x-request-id",)
    allow_insecure_http_loopback: bool = False
    ssl_context: ssl.SSLContext | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or not self.base_url:
            raise ValueError("base_url must be non-empty text")
        if self.base_url != self.base_url.strip():
            raise ValueError("base_url must not contain surrounding whitespace")
        if not isinstance(self.provider_id, str) or not _PROVIDER_RE.fullmatch(
            self.provider_id
        ):
            raise ValueError("provider_id must be a bounded ASCII identifier")
        for name in (
            "connect_timeout_seconds",
            "write_timeout_seconds",
            "read_timeout_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{name} must be a positive finite number")
        for name in (
            "max_request_bytes",
            "max_response_bytes",
            "max_stream_event_bytes",
            "max_header_bytes",
            "max_header_count",
            "max_credential_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_stream_event_bytes > self.max_response_bytes:
            raise ValueError(
                "max_stream_event_bytes must not exceed max_response_bytes"
            )
        if not isinstance(self.allow_insecure_http_loopback, bool):
            raise TypeError("allow_insecure_http_loopback must be boolean")
        if (
            not isinstance(self.request_id_header_names, tuple)
            or not self.request_id_header_names
        ):
            raise ValueError("request_id_header_names must be a non-empty tuple")
        normalized: list[str] = []
        for raw_name in self.request_id_header_names:
            if not isinstance(raw_name, str):
                raise TypeError("request ID header names must be text")
            name = raw_name.lower()
            if raw_name != name or not _HEADER_NAME_RE.fullmatch(name):
                raise ValueError(
                    "request ID header names must be lowercase HTTP field names"
                )
            if name in _SENSITIVE_HEADER_NAMES:
                raise ValueError("request ID headers must not contain credentials")
            if name in normalized:
                raise ValueError("request ID header names must be unique")
            normalized.append(name)
        if self.ssl_context is not None:
            _validate_ssl_context(self.ssl_context)
        _parse_endpoint(self)


@dataclass(frozen=True, slots=True)
class _Endpoint:
    scheme: str
    host: str
    port: int
    target: str = "/v1/responses"


@dataclass(slots=True)
class _ResponseHeaders:
    values: dict[str, list[str]]
    provider_request_id: str | None
    content_length: int | None


class _ActiveCall:
    def __init__(
        self,
        connection: http.client.HTTPConnection,
        cancel_event: threading.Event,
    ):
        self.connection = connection
        self.cancel_event = cancel_event
        self.response: http.client.HTTPResponse | None = None
        self.provider_request_id: str | None = None
        self.done_event = threading.Event()
        self._lock = threading.RLock()
        self._cancelled = False
        self._closed = False

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled or self.cancel_event.is_set()

    def bind_response(self, response: http.client.HTTPResponse) -> None:
        with self._lock:
            if self._closed:
                response.close()
                return
            self.response = response

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._lock:
            self._cancelled = True
        self.close()

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            response = self.response
            sock = self.connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if response is not None:
            try:
                response.close()
            except OSError:
                pass
        try:
            self.connection.close()
        except OSError:
            pass

    def finish(self) -> None:
        self.done_event.set()
        self.close()

    def watch_cancellation(self) -> None:
        while not self.done_event.wait(_CANCELLATION_POLL_SECONDS):
            if self.cancel_event.is_set():
                self.cancel()
                return


class OpenAIResponsesBackend:
    """One-shot backend for an operator-configured compatible Responses API."""

    def __init__(self, config: OpenAIResponsesBackendConfig):
        if not isinstance(config, OpenAIResponsesBackendConfig):
            raise TypeError("config must be OpenAIResponsesBackendConfig")
        self._config = config
        self._endpoint = _parse_endpoint(config)
        self._ssl_context = _build_ssl_context(config)
        self._active_lock = threading.RLock()
        self._active: dict[str, _ActiveCall] = {}

    @property
    def config(self) -> OpenAIResponsesBackendConfig:
        return self._config

    def create(
        self,
        credential: str | bytes,
        request: ResponsesBackendRequest,
    ) -> ResponsesBackendResponse:
        body = self._request_body(request, streaming=False)
        bearer = self._bearer_credential(credential)
        credential = ""
        active = self._begin_call(request)
        try:
            response, headers = self._send(active, bearer, body, streaming=False)
            payload = self._read_json_response(active, response, headers)
            return self._backend_response(
                payload,
                request=request,
                provider_request_id=self._required_request_id(headers),
            )
        except ResponsesCancelled:
            raise
        except OpenAIResponsesBackendError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            self._raise_transport_error(
                active,
                OpenAIResponsesBackendErrorCode.TIMEOUT,
                retryable=True,
                cause=exc,
            )
        except ssl.SSLError as exc:
            self._raise_transport_error(
                active,
                OpenAIResponsesBackendErrorCode.TLS,
                retryable=False,
                cause=exc,
            )
        except (OSError, http.client.HTTPException) as exc:
            self._raise_transport_error(
                active,
                OpenAIResponsesBackendErrorCode.TRANSPORT,
                retryable=True,
                cause=exc,
            )
        finally:
            bearer = ""
            self._finish_call(request.provider_request_id, active)

    def stream(
        self,
        credential: str | bytes,
        request: ResponsesBackendRequest,
    ) -> Iterable[ResponsesStreamChunk]:
        return self._stream(credential, request)

    def _stream(
        self,
        credential: str | bytes,
        request: ResponsesBackendRequest,
    ) -> Iterator[ResponsesStreamChunk]:
        body = self._request_body(request, streaming=True)
        bearer = self._bearer_credential(credential)
        credential = ""
        active = self._begin_call(request)
        try:
            response, headers = self._send(active, bearer, body, streaming=True)
            provider_request_id = self._required_request_id(headers)
            terminal_seen = False
            for event_name, raw_data in self._iter_sse(
                response,
                active,
                provider_request_id,
            ):
                if active.cancelled:
                    raise ResponsesCancelled()
                if raw_data == "[DONE]":
                    if not terminal_seen:
                        raise self._error(
                            OpenAIResponsesBackendErrorCode.INVALID_SSE,
                            request_id=provider_request_id,
                        )
                    return
                event = self._strict_json_mapping(
                    raw_data.encode("utf-8"),
                    code=OpenAIResponsesBackendErrorCode.INVALID_SSE,
                    request_id=provider_request_id,
                )
                event_type = event.get("type")
                if not isinstance(event_type, str) or not event_type:
                    raise self._error(
                        OpenAIResponsesBackendErrorCode.INVALID_SSE,
                        request_id=provider_request_id,
                    )
                if event_name is not None and event_name != event_type:
                    raise self._error(
                        OpenAIResponsesBackendErrorCode.INVALID_SSE,
                        request_id=provider_request_id,
                    )
                if terminal_seen:
                    raise self._error(
                        OpenAIResponsesBackendErrorCode.INVALID_SSE,
                        request_id=provider_request_id,
                    )
                if event_type == "error":
                    raise self._error(
                        OpenAIResponsesBackendErrorCode.PROVIDER_ERROR,
                        request_id=provider_request_id,
                    )
                if event_type in _TERMINAL_EVENTS:
                    terminal_seen = True
                    raw_response = event.get("response")
                    if not isinstance(raw_response, Mapping):
                        raise self._error(
                            OpenAIResponsesBackendErrorCode.INVALID_RESPONSE,
                            request_id=provider_request_id,
                        )
                    final = self._backend_response(
                        dict(raw_response),
                        request=request,
                        provider_request_id=provider_request_id,
                    )
                    yield ResponsesStreamChunk(event=event, final_response=final)
                    return
                yield ResponsesStreamChunk(event=event)
            if active.cancelled:
                raise ResponsesCancelled()
            raise self._error(
                OpenAIResponsesBackendErrorCode.INVALID_SSE,
                request_id=provider_request_id,
            )
        except ResponsesCancelled:
            raise
        except OpenAIResponsesBackendError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            self._raise_transport_error(
                active,
                OpenAIResponsesBackendErrorCode.TIMEOUT,
                retryable=True,
                cause=exc,
            )
        except ssl.SSLError as exc:
            self._raise_transport_error(
                active,
                OpenAIResponsesBackendErrorCode.TLS,
                retryable=False,
                cause=exc,
            )
        except (OSError, http.client.HTTPException) as exc:
            self._raise_transport_error(
                active,
                OpenAIResponsesBackendErrorCode.TRANSPORT,
                retryable=True,
                cause=exc,
            )
        finally:
            bearer = ""
            self._finish_call(request.provider_request_id, active)

    def cancel(self, provider_request_id: str) -> None:
        if not isinstance(provider_request_id, str):
            return
        with self._active_lock:
            active = self._active.get(provider_request_id)
        if active is not None:
            active.cancel()

    def _begin_call(self, request: ResponsesBackendRequest) -> _ActiveCall:
        self._validate_request(request)
        if request.cancel_event.is_set():
            raise ResponsesCancelled()
        connection = self._connection()
        active = _ActiveCall(connection, request.cancel_event)
        with self._active_lock:
            if request.provider_request_id in self._active:
                raise self._error(
                    OpenAIResponsesBackendErrorCode.INVALID_REQUEST,
                )
            self._active[request.provider_request_id] = active
        watcher = threading.Thread(
            target=active.watch_cancellation,
            name=f"atv-provider-cancel-{request.provider_request_id[:12]}",
            daemon=True,
        )
        watcher.start()
        return active

    def _finish_call(self, provider_request_id: str, active: _ActiveCall) -> None:
        active.finish()
        with self._active_lock:
            if self._active.get(provider_request_id) is active:
                del self._active[provider_request_id]

    def _connection(self) -> http.client.HTTPConnection:
        if self._endpoint.scheme == "https":
            return http.client.HTTPSConnection(
                self._endpoint.host,
                self._endpoint.port,
                timeout=float(self._config.connect_timeout_seconds),
                context=self._ssl_context,
            )
        return http.client.HTTPConnection(
            self._endpoint.host,
            self._endpoint.port,
            timeout=float(self._config.connect_timeout_seconds),
        )

    def _send(
        self,
        active: _ActiveCall,
        bearer: str,
        body: bytes,
        *,
        streaming: bool,
    ) -> tuple[http.client.HTTPResponse, _ResponseHeaders]:
        if active.cancelled:
            raise ResponsesCancelled()
        connection = active.connection
        connection.connect()
        if active.cancelled:
            raise ResponsesCancelled()
        if connection.sock is None:
            raise self._error(OpenAIResponsesBackendErrorCode.TRANSPORT, retryable=True)
        connection.sock.settimeout(float(self._config.write_timeout_seconds))
        connection.putrequest(
            "POST",
            self._endpoint.target,
            skip_accept_encoding=True,
        )
        connection.putheader("Authorization", f"Bearer {bearer}")
        connection.putheader("Content-Type", "application/json")
        connection.putheader(
            "Accept",
            "text/event-stream" if streaming else "application/json",
        )
        connection.putheader("User-Agent", "atv-bench-responses-backend/1")
        connection.putheader("Connection", "close")
        connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
        if active.cancelled:
            raise ResponsesCancelled()
        if connection.sock is None:
            raise self._error(OpenAIResponsesBackendErrorCode.TRANSPORT, retryable=True)
        connection.sock.settimeout(float(self._config.read_timeout_seconds))
        response = connection.getresponse()
        active.bind_response(response)
        headers = self._validate_response_headers(response)
        active.provider_request_id = headers.provider_request_id
        if response.status != 200:
            self._discard_error_body(response, active)
            status = int(response.status)
            raise self._error(
                OpenAIResponsesBackendErrorCode.HTTP_STATUS,
                retryable=status in {408, 425, 429} or 500 <= status <= 599,
                request_id=headers.provider_request_id,
                status_code=status,
            )
        self._required_request_id(headers)
        expected = "text/event-stream" if streaming else "application/json"
        self._validate_content_type(headers, expected=expected)
        if (
            headers.content_length is not None
            and headers.content_length > self._config.max_response_bytes
        ):
            raise self._error(
                OpenAIResponsesBackendErrorCode.RESPONSE_TOO_LARGE,
                request_id=headers.provider_request_id,
            )
        return response, headers

    def _validate_response_headers(
        self,
        response: http.client.HTTPResponse,
    ) -> _ResponseHeaders:
        raw_headers = response.getheaders()
        if len(raw_headers) > self._config.max_header_count:
            raise self._error(OpenAIResponsesBackendErrorCode.INVALID_HEADERS)
        defects = getattr(response.headers, "defects", ())
        if defects:
            raise self._error(OpenAIResponsesBackendErrorCode.INVALID_HEADERS)
        total_bytes = 0
        values: dict[str, list[str]] = {}
        for raw_name, raw_value in raw_headers:
            if not isinstance(raw_name, str) or not isinstance(raw_value, str):
                raise self._error(OpenAIResponsesBackendErrorCode.INVALID_HEADERS)
            name = raw_name.lower()
            value = raw_value
            try:
                total_bytes += len(name.encode("ascii")) + len(value.encode("ascii")) + 4
            except UnicodeEncodeError:
                raise self._error(
                    OpenAIResponsesBackendErrorCode.INVALID_HEADERS
                ) from None
            if (
                not _HEADER_NAME_RE.fullmatch(name)
                or not _HEADER_VALUE_RE.fullmatch(value)
                or value != value.strip()
                or total_bytes > self._config.max_header_bytes
            ):
                raise self._error(OpenAIResponsesBackendErrorCode.INVALID_HEADERS)
            values.setdefault(name, []).append(value)
        for name in (
            "content-type",
            "content-length",
            "transfer-encoding",
            "content-encoding",
            "connection",
        ):
            if len(values.get(name, ())) > 1:
                raise self._error(OpenAIResponsesBackendErrorCode.INVALID_HEADERS)
        content_encoding = values.get("content-encoding", [])
        if content_encoding and content_encoding[0].lower() != "identity":
            raise self._error(OpenAIResponsesBackendErrorCode.INVALID_HEADERS)
        transfer_encoding = values.get("transfer-encoding", [])
        if transfer_encoding:
            if transfer_encoding[0].lower() != "chunked" or "content-length" in values:
                raise self._error(OpenAIResponsesBackendErrorCode.INVALID_HEADERS)
        connection_values = values.get("connection", [])
        if connection_values and "upgrade" in {
            item.strip().lower() for item in connection_values[0].split(",")
        }:
            raise self._error(OpenAIResponsesBackendErrorCode.INVALID_HEADERS)
        content_length: int | None = None
        if "content-length" in values:
            raw_length = values["content-length"][0]
            if not raw_length.isascii() or not raw_length.isdecimal():
                raise self._error(OpenAIResponsesBackendErrorCode.INVALID_HEADERS)
            content_length = int(raw_length)
        provider_request_id = self._extract_request_id(values)
        return _ResponseHeaders(
            values=values,
            provider_request_id=provider_request_id,
            content_length=content_length,
        )

    def _extract_request_id(self, values: Mapping[str, list[str]]) -> str | None:
        found: list[str] = []
        for name in self._config.request_id_header_names:
            header_values = values.get(name, [])
            if len(header_values) > 1:
                raise self._error(OpenAIResponsesBackendErrorCode.INVALID_HEADERS)
            if header_values:
                found.append(header_values[0])
        if len(found) > 1:
            raise self._error(OpenAIResponsesBackendErrorCode.INVALID_HEADERS)
        if not found:
            return None
        request_id = found[0]
        if not _IDENTIFIER_RE.fullmatch(request_id):
            raise self._error(OpenAIResponsesBackendErrorCode.INVALID_HEADERS)
        return request_id

    def _required_request_id(self, headers: _ResponseHeaders) -> str:
        if headers.provider_request_id is None:
            raise self._error(OpenAIResponsesBackendErrorCode.INVALID_HEADERS)
        return headers.provider_request_id

    def _validate_content_type(
        self,
        headers: _ResponseHeaders,
        *,
        expected: str,
    ) -> None:
        raw_values = headers.values.get("content-type", [])
        if len(raw_values) != 1:
            raise self._error(
                OpenAIResponsesBackendErrorCode.INVALID_CONTENT_TYPE,
                request_id=headers.provider_request_id,
            )
        parts = [part.strip() for part in raw_values[0].split(";")]
        if not parts or parts[0].lower() != expected:
            raise self._error(
                OpenAIResponsesBackendErrorCode.INVALID_CONTENT_TYPE,
                request_id=headers.provider_request_id,
            )
        parameters: dict[str, str] = {}
        for raw_parameter in parts[1:]:
            if "=" not in raw_parameter:
                raise self._error(
                    OpenAIResponsesBackendErrorCode.INVALID_CONTENT_TYPE,
                    request_id=headers.provider_request_id,
                )
            name, value = raw_parameter.split("=", 1)
            name = name.strip().lower()
            value = value.strip().strip('"').lower()
            if not name or name in parameters:
                raise self._error(
                    OpenAIResponsesBackendErrorCode.INVALID_CONTENT_TYPE,
                    request_id=headers.provider_request_id,
                )
            parameters[name] = value
        if parameters and parameters != {"charset": "utf-8"}:
            raise self._error(
                OpenAIResponsesBackendErrorCode.INVALID_CONTENT_TYPE,
                request_id=headers.provider_request_id,
            )

    def _read_json_response(
        self,
        active: _ActiveCall,
        response: http.client.HTTPResponse,
        headers: _ResponseHeaders,
    ) -> dict[str, Any]:
        request_id = self._required_request_id(headers)
        body = self._read_bounded(response, active, request_id)
        return self._strict_json_mapping(
            body,
            code=OpenAIResponsesBackendErrorCode.INVALID_JSON,
            request_id=request_id,
        )

    def _read_bounded(
        self,
        response: http.client.HTTPResponse,
        active: _ActiveCall,
        provider_request_id: str,
    ) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            if active.cancelled:
                raise ResponsesCancelled()
            chunk = response.read(
                min(
                    _READ_CHUNK_BYTES,
                    self._config.max_response_bytes - total + 1,
                )
            )
            if not chunk:
                break
            total += len(chunk)
            if total > self._config.max_response_bytes:
                raise self._error(
                    OpenAIResponsesBackendErrorCode.RESPONSE_TOO_LARGE,
                    request_id=provider_request_id,
                )
            chunks.append(chunk)
        if active.cancelled:
            raise ResponsesCancelled()
        return b"".join(chunks)

    def _discard_error_body(
        self,
        response: http.client.HTTPResponse,
        active: _ActiveCall,
    ) -> None:
        remaining = min(self._config.max_response_bytes, 64 * 1024)
        while remaining > 0:
            if active.cancelled:
                raise ResponsesCancelled()
            chunk = response.read(min(_READ_CHUNK_BYTES, remaining))
            if not chunk:
                return
            remaining -= len(chunk)

    def _iter_sse(
        self,
        response: http.client.HTTPResponse,
        active: _ActiveCall,
        provider_request_id: str,
    ) -> Iterator[tuple[str | None, str]]:
        total_bytes = 0
        event_bytes = 0
        event_name: str | None = None
        data_lines: list[str] = []
        while True:
            if active.cancelled:
                raise ResponsesCancelled()
            raw_line = response.readline(self._config.max_stream_event_bytes + 2)
            if active.cancelled:
                raise ResponsesCancelled()
            if raw_line == b"":
                if data_lines:
                    yield event_name, "\n".join(data_lines)
                return
            total_bytes += len(raw_line)
            event_bytes += len(raw_line)
            if total_bytes > self._config.max_response_bytes:
                raise self._error(
                    OpenAIResponsesBackendErrorCode.RESPONSE_TOO_LARGE,
                    request_id=provider_request_id,
                )
            if (
                event_bytes > self._config.max_stream_event_bytes
                or (
                    len(raw_line) > self._config.max_stream_event_bytes
                    and not raw_line.endswith(b"\n")
                )
            ):
                raise self._error(
                    OpenAIResponsesBackendErrorCode.RESPONSE_TOO_LARGE,
                    request_id=provider_request_id,
                )
            if raw_line.endswith(b"\n"):
                raw_line = raw_line[:-1]
                if raw_line.endswith(b"\r"):
                    raw_line = raw_line[:-1]
            if b"\r" in raw_line or b"\x00" in raw_line:
                raise self._error(
                    OpenAIResponsesBackendErrorCode.INVALID_SSE,
                    request_id=provider_request_id,
                )
            if raw_line == b"":
                if data_lines:
                    yield event_name, "\n".join(data_lines)
                event_bytes = 0
                event_name = None
                data_lines = []
                continue
            if raw_line.startswith(b":"):
                continue
            raw_field, separator, raw_value = raw_line.partition(b":")
            if separator and raw_value.startswith(b" "):
                raw_value = raw_value[1:]
            try:
                field_name = raw_field.decode("ascii")
                value = raw_value.decode("utf-8")
            except UnicodeDecodeError:
                raise self._error(
                    OpenAIResponsesBackendErrorCode.INVALID_SSE,
                    request_id=provider_request_id,
                ) from None
            if field_name == "event":
                if event_name is not None or not value or not _IDENTIFIER_RE.fullmatch(
                    value
                ):
                    raise self._error(
                        OpenAIResponsesBackendErrorCode.INVALID_SSE,
                        request_id=provider_request_id,
                    )
                event_name = value
            elif field_name == "data":
                data_lines.append(value)
            else:
                raise self._error(
                    OpenAIResponsesBackendErrorCode.INVALID_SSE,
                    request_id=provider_request_id,
                )

    def _request_body(
        self,
        request: ResponsesBackendRequest,
        *,
        streaming: bool,
    ) -> bytes:
        self._validate_request(request)
        payload = request.payload
        if payload.get("model") != request.provider_model:
            raise self._error(OpenAIResponsesBackendErrorCode.INVALID_REQUEST)
        if payload.get("stream") is not streaming:
            raise self._error(OpenAIResponsesBackendErrorCode.INVALID_REQUEST)
        try:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            raise self._error(
                OpenAIResponsesBackendErrorCode.INVALID_REQUEST
            ) from None
        if len(body) > self._config.max_request_bytes:
            raise self._error(OpenAIResponsesBackendErrorCode.INVALID_REQUEST)
        return body

    def _validate_request(self, request: ResponsesBackendRequest) -> None:
        if not isinstance(request, ResponsesBackendRequest):
            raise TypeError("request must be ResponsesBackendRequest")
        if (
            not isinstance(request.provider_request_id, str)
            or not _IDENTIFIER_RE.fullmatch(request.provider_request_id)
            or not isinstance(request.gateway_request_id, str)
            or not _IDENTIFIER_RE.fullmatch(request.gateway_request_id)
            or not isinstance(request.provider_model, str)
            or not _MODEL_RE.fullmatch(request.provider_model)
            or not isinstance(request.payload, Mapping)
            or not isinstance(request.cancel_event, threading.Event)
        ):
            raise self._error(OpenAIResponsesBackendErrorCode.INVALID_REQUEST)

    def _bearer_credential(self, credential: str | bytes) -> str:
        if isinstance(credential, bytes):
            try:
                value = credential.decode("ascii")
            except UnicodeDecodeError:
                raise self._error(
                    OpenAIResponsesBackendErrorCode.INVALID_CREDENTIAL
                ) from None
        elif isinstance(credential, str):
            value = credential
        else:
            raise self._error(OpenAIResponsesBackendErrorCode.INVALID_CREDENTIAL)
        try:
            encoded = value.encode("ascii")
        except UnicodeEncodeError:
            raise self._error(
                OpenAIResponsesBackendErrorCode.INVALID_CREDENTIAL
            ) from None
        if (
            not value
            or len(encoded) > self._config.max_credential_bytes
            or value != value.strip()
            or not _HEADER_VALUE_RE.fullmatch(value)
            or any(character.isspace() for character in value)
        ):
            raise self._error(OpenAIResponsesBackendErrorCode.INVALID_CREDENTIAL)
        return value

    def _backend_response(
        self,
        payload: Mapping[str, Any],
        *,
        request: ResponsesBackendRequest,
        provider_request_id: str,
    ) -> ResponsesBackendResponse:
        response = dict(payload)
        response_id = response.get("id")
        model = response.get("model")
        usage = response.get("usage")
        if (
            not isinstance(response_id, str)
            or not _IDENTIFIER_RE.fullmatch(response_id)
            or not isinstance(model, str)
            or model != request.provider_model
            or not isinstance(usage, Mapping)
        ):
            raise self._error(
                OpenAIResponsesBackendErrorCode.INVALID_RESPONSE,
                request_id=provider_request_id,
            )
        input_tokens = self._usage_integer(usage, "input_tokens", provider_request_id)
        output_tokens = self._usage_integer(
            usage,
            "output_tokens",
            provider_request_id,
        )
        total_tokens = self._usage_integer(usage, "total_tokens", provider_request_id)
        if total_tokens != input_tokens + output_tokens:
            raise self._error(
                OpenAIResponsesBackendErrorCode.INVALID_RESPONSE,
                request_id=provider_request_id,
            )
        return ResponsesBackendResponse(
            provider_id=self._config.provider_id,
            model=model,
            request_id=provider_request_id,
            response=response,
            usage=ProviderUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_microusd=0,
            ),
        )

    def _usage_integer(
        self,
        usage: Mapping[str, Any],
        name: str,
        provider_request_id: str,
    ) -> int:
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise self._error(
                OpenAIResponsesBackendErrorCode.INVALID_RESPONSE,
                request_id=provider_request_id,
            )
        return value

    def _strict_json_mapping(
        self,
        body: bytes,
        *,
        code: OpenAIResponsesBackendErrorCode,
        request_id: str | None,
    ) -> dict[str, Any]:
        def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate JSON key")
                result[key] = value
            return result

        def reject_constant(_value: str) -> Any:
            raise ValueError("non-finite JSON number")

        try:
            text = body.decode("utf-8")
            value = json.loads(
                text,
                object_pairs_hook=object_pairs,
                parse_constant=reject_constant,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            OverflowError,
            RecursionError,
        ):
            raise self._error(code, request_id=request_id) from None
        if not isinstance(value, dict):
            raise self._error(code, request_id=request_id)
        return value

    def _raise_transport_error(
        self,
        active: _ActiveCall,
        code: OpenAIResponsesBackendErrorCode,
        *,
        retryable: bool,
        cause: BaseException,
    ) -> NoReturn:
        if active.cancelled:
            raise ResponsesCancelled() from None
        del cause
        raise self._error(
            code,
            retryable=retryable,
            request_id=active.provider_request_id,
        ) from None

    @staticmethod
    def _error(
        code: OpenAIResponsesBackendErrorCode,
        *,
        retryable: bool = False,
        request_id: str | None = None,
        status_code: int | None = None,
    ) -> OpenAIResponsesBackendError:
        return OpenAIResponsesBackendError(
            code=code,
            retryable=retryable,
            request_id=request_id,
            status_code=status_code,
        )


def _parse_endpoint(config: OpenAIResponsesBackendConfig) -> _Endpoint:
    parsed = urlsplit(config.base_url)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("base_url must be an origin URL without credentials or suffix")
    host = parsed.hostname
    if host is None:
        raise ValueError("base_url must include a hostname")
    try:
        host.encode("ascii")
    except UnicodeEncodeError as exc:
        raise ValueError("base_url hostname must be ASCII") from exc
    if any(character.isspace() for character in host) or "%" in host:
        raise ValueError("base_url hostname is invalid")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("base_url port is invalid") from exc
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if parsed.scheme == "http" and not (
        config.allow_insecure_http_loopback and _is_loopback_literal(host)
    ):
        raise ValueError("HTTP is permitted only for explicitly enabled loopback tests")
    return _Endpoint(scheme=parsed.scheme, host=host, port=port)


def _is_loopback_literal(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_ssl_context(context: ssl.SSLContext) -> None:
    if not isinstance(context, ssl.SSLContext):
        raise TypeError("ssl_context must be an SSLContext")
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise ValueError("ssl_context must verify certificates and hostnames")
    if context.minimum_version < ssl.TLSVersion.TLSv1_2:
        raise ValueError("ssl_context must require TLS 1.2 or newer")
    if (
        context.maximum_version is not ssl.TLSVersion.MAXIMUM_SUPPORTED
        and context.maximum_version < ssl.TLSVersion.TLSv1_2
    ):
        raise ValueError("ssl_context must permit TLS 1.2 or newer")


def _build_ssl_context(
    config: OpenAIResponsesBackendConfig,
) -> ssl.SSLContext | None:
    if config.base_url.startswith("http://"):
        return None
    if config.ssl_context is not None:
        _validate_ssl_context(config.ssl_context)
        return config.ssl_context
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    _validate_ssl_context(context)
    return context
