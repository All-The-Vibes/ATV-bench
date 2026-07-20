"""Hermetic HTTP tests for the strict compatible Responses backend."""

from __future__ import annotations

import json
import ssl
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from atv_bench.security import (
    OpenAIResponsesBackend,
    OpenAIResponsesBackendConfig,
    OpenAIResponsesBackendError,
    OpenAIResponsesBackendErrorCode,
    ResponsesBackendRequest,
    ResponsesCancelled,
)


PROVIDER_ID = "operator-provider"
MODEL = "provider-model-snapshot-2026-07-20"
CREDENTIAL = "provider-secret-canary-7c4d40"
LOCAL_REQUEST_ID = "local-provider-request-1"
GATEWAY_REQUEST_ID = "gateway-request-1"
PROVIDER_REQUEST_ID = "provider-request-exact-1"


@dataclass
class RecordedRequest:
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class ServerState:
    responder: Callable[[BaseHTTPRequestHandler, "ServerState"], None]
    requests: list[RecordedRequest] = field(default_factory=list)
    handler_errors: list[BaseException] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)


class _TestHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


@contextmanager
def fake_server(
    responder: Callable[[BaseHTTPRequestHandler, ServerState], None],
) -> Iterator[tuple[str, ServerState]]:
    state = ServerState(responder=responder)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            try:
                raw_length = self.headers.get("Content-Length", "0")
                body = self.rfile.read(int(raw_length))
                with state.lock:
                    state.requests.append(
                        RecordedRequest(
                            path=self.path,
                            headers={name.lower(): value for name, value in self.headers.items()},
                            body=body,
                        )
                    )
                state.responder(self, state)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except BaseException as exc:
                with state.lock:
                    state.handler_errors.append(exc)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    server = _TestHttpServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def backend_for(
    base_url: str,
    **overrides: Any,
) -> OpenAIResponsesBackend:
    values: dict[str, Any] = {
        "base_url": base_url,
        "provider_id": PROVIDER_ID,
        "allow_insecure_http_loopback": True,
        "connect_timeout_seconds": 1,
        "write_timeout_seconds": 1,
        "read_timeout_seconds": 1,
    }
    values.update(overrides)
    return OpenAIResponsesBackend(OpenAIResponsesBackendConfig(**values))


def request_for(
    *,
    stream: bool = False,
    payload_overrides: dict[str, Any] | None = None,
    cancel_event: threading.Event | None = None,
    provider_request_id: str = LOCAL_REQUEST_ID,
) -> ResponsesBackendRequest:
    payload: dict[str, Any] = {
        "model": MODEL,
        "input": [{"role": "user", "content": "Inspect the repository."}],
        "max_output_tokens": 32,
        "stream": stream,
        "store": False,
    }
    if payload_overrides:
        payload.update(payload_overrides)
    return ResponsesBackendRequest(
        gateway_request_id=GATEWAY_REQUEST_ID,
        provider_request_id=provider_request_id,
        provider_model=MODEL,
        payload=payload,
        cancel_event=cancel_event or threading.Event(),
    )


def response_payload(
    *,
    response_id: str = "resp-exact-1",
    model: str = MODEL,
    input_tokens: int = 11,
    output_tokens: int = 7,
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": 1_721_500_000,
        "status": "completed",
        "model": model,
        "output": [
            {
                "id": "msg-1",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "done",
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 2},
        },
    }


def send_json(
    handler: BaseHTTPRequestHandler,
    payload: dict[str, Any],
    *,
    status: int = 200,
    request_id_header: str = "x-request-id",
    request_id: str = PROVIDER_REQUEST_ID,
    content_type: str = "application/json; charset=utf-8",
) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header(request_id_header, request_id)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


def send_sse_headers(
    handler: BaseHTTPRequestHandler,
    *,
    request_id: str = PROVIDER_REQUEST_ID,
) -> None:
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("x-request-id", request_id)
    handler.send_header("Connection", "close")
    handler.end_headers()


def write_sse(
    handler: BaseHTTPRequestHandler,
    event: dict[str, Any],
    *,
    event_name: str | None = None,
) -> None:
    if event_name is not None:
        handler.wfile.write(f"event: {event_name}\n".encode())
    data = json.dumps(event, separators=(",", ":"))
    handler.wfile.write(f"data: {data}\n\n".encode())
    handler.wfile.flush()


def assert_sanitized(error: OpenAIResponsesBackendError) -> None:
    rendered = f"{error!s} {error!r}"
    assert CREDENTIAL not in rendered
    assert "provider-internal-diagnostic" not in rendered
    assert error.args == ("responses backend call failed",)


def test_config_requires_strict_origin_tls_and_explicit_loopback_exception():
    config = OpenAIResponsesBackendConfig(
        base_url="https://compatible.example.test:8443",
        provider_id=PROVIDER_ID,
    )
    backend = OpenAIResponsesBackend(config)
    assert backend.config.base_url == "https://compatible.example.test:8443"

    invalid_urls = (
        "http://compatible.example.test",
        "https://user:secret@compatible.example.test",
        "https://compatible.example.test/proxy",
        "https://compatible.example.test?debug=1",
        "https://compatible.example.test#fragment",
    )
    for url in invalid_urls:
        with pytest.raises(ValueError):
            OpenAIResponsesBackendConfig(base_url=url, provider_id=PROVIDER_ID)

    with pytest.raises(ValueError):
        OpenAIResponsesBackendConfig(
            base_url="http://127.0.0.1:8000",
            provider_id=PROVIDER_ID,
        )
    loopback = OpenAIResponsesBackendConfig(
        base_url="http://127.0.0.1:8000",
        provider_id=PROVIDER_ID,
        allow_insecure_http_loopback=True,
    )
    assert loopback.allow_insecure_http_loopback is True
    with pytest.raises(ValueError):
        OpenAIResponsesBackendConfig(
            base_url="http://localhost:8000",
            provider_id=PROVIDER_ID,
            allow_insecure_http_loopback=True,
        )
    with pytest.raises(ValueError):
        OpenAIResponsesBackendConfig(
            base_url="https://compatible.example.test",
            provider_id=PROVIDER_ID,
            request_id_header_names=("authorization",),
        )


def test_config_rejects_unverified_or_legacy_tls_context():
    insecure = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    insecure.check_hostname = False
    insecure.verify_mode = ssl.CERT_NONE
    with pytest.raises(ValueError):
        OpenAIResponsesBackendConfig(
            base_url="https://compatible.example.test",
            provider_id=PROVIDER_ID,
            ssl_context=insecure,
        )

    legacy = ssl.create_default_context()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        legacy.minimum_version = ssl.TLSVersion.TLSv1
    with pytest.raises(ValueError):
        OpenAIResponsesBackendConfig(
            base_url="https://compatible.example.test",
            provider_id=PROVIDER_ID,
            ssl_context=legacy,
        )


def test_nonstream_round_trip_extracts_exact_request_model_and_usage():
    def respond(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        send_json(handler, response_payload())

    with fake_server(respond) as (base_url, state):
        backend = backend_for(base_url)
        request = request_for()
        original_payload = json.loads(json.dumps(request.payload))
        result = backend.create(CREDENTIAL, request)

    assert result.provider_id == PROVIDER_ID
    assert result.request_id == PROVIDER_REQUEST_ID
    assert result.model == MODEL
    assert result.response["id"] == "resp-exact-1"
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 7
    assert result.usage.cost_microusd == 0
    assert request.payload == original_payload
    assert len(state.requests) == 1
    recorded = state.requests[0]
    assert recorded.path == "/v1/responses"
    assert recorded.headers["authorization"] == f"Bearer {CREDENTIAL}"
    assert recorded.headers["content-type"] == "application/json"
    assert recorded.headers["accept"] == "application/json"
    assert recorded.headers["connection"] == "close"
    assert json.loads(recorded.body) == original_payload
    assert CREDENTIAL not in repr(backend.__dict__)
    assert not state.handler_errors


def test_request_id_header_is_operator_configurable_and_bytes_credential_works():
    def respond(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        send_json(
            handler,
            response_payload(response_id="resp-apim"),
            request_id_header="apim-request-id",
            request_id="apim-exact-7",
        )

    with fake_server(respond) as (base_url, _state):
        backend = backend_for(
            base_url,
            request_id_header_names=("apim-request-id",),
        )
        result = backend.create(CREDENTIAL.encode("ascii"), request_for())

    assert result.request_id == "apim-exact-7"
    assert result.response["id"] == "resp-apim"


def test_http_error_is_sanitized_typed_and_never_retried():
    def respond(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        body = (
            f'provider-internal-diagnostic credential={CREDENTIAL}'
        ).encode()
        handler.send_response(503)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("x-request-id", "provider-error-request-1")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    with fake_server(respond) as (base_url, state):
        backend = backend_for(base_url)
        with pytest.raises(OpenAIResponsesBackendError) as caught:
            backend.create(CREDENTIAL, request_for())

    error = caught.value
    assert error.code is OpenAIResponsesBackendErrorCode.HTTP_STATUS
    assert error.status_code == 503
    assert error.request_id == "provider-error-request-1"
    assert error.retryable is True
    assert len(state.requests) == 1
    assert_sanitized(error)


def test_redirect_is_not_followed_and_is_not_retryable():
    def respond(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        handler.send_response(307)
        handler.send_header("Location", "http://127.0.0.1/credential-sink")
        handler.send_header("x-request-id", "provider-redirect-1")
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    with fake_server(respond) as (base_url, state):
        backend = backend_for(base_url)
        with pytest.raises(OpenAIResponsesBackendError) as caught:
            backend.create(CREDENTIAL, request_for())

    assert caught.value.code is OpenAIResponsesBackendErrorCode.HTTP_STATUS
    assert caught.value.status_code == 307
    assert caught.value.retryable is False
    assert len(state.requests) == 1


@pytest.mark.parametrize(
    ("credential", "payload_override"),
    (
        ("secret\r\nX-Evil: yes", None),
        ("secret with spaces", None),
        (CREDENTIAL, {"model": "different-model"}),
        (CREDENTIAL, {"stream": True}),
    ),
)
def test_invalid_credentials_and_request_contract_fail_before_network(
    credential: str,
    payload_override: dict[str, Any] | None,
):
    def respond(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        send_json(handler, response_payload())

    with fake_server(respond) as (base_url, state):
        backend = backend_for(base_url)
        request = request_for(payload_overrides=payload_override)
        with pytest.raises(OpenAIResponsesBackendError) as caught:
            backend.create(credential, request)

    assert caught.value.code in {
        OpenAIResponsesBackendErrorCode.INVALID_CREDENTIAL,
        OpenAIResponsesBackendErrorCode.INVALID_REQUEST,
    }
    assert state.requests == []
    assert_sanitized(caught.value)


def test_request_and_response_size_limits_are_enforced():
    def oversized_response(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        body = b"x" * 129
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("x-request-id", PROVIDER_REQUEST_ID)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()

    with fake_server(oversized_response) as (base_url, state):
        backend = backend_for(
            base_url,
            max_response_bytes=128,
            max_stream_event_bytes=64,
        )
        with pytest.raises(OpenAIResponsesBackendError) as caught:
            backend.create(CREDENTIAL, request_for())
        assert caught.value.code is OpenAIResponsesBackendErrorCode.RESPONSE_TOO_LARGE
        assert len(state.requests) == 1

    with fake_server(oversized_response) as (base_url, state):
        backend = backend_for(base_url, max_request_bytes=64)
        with pytest.raises(OpenAIResponsesBackendError) as caught:
            backend.create(
                CREDENTIAL,
                request_for(payload_overrides={"input": "x" * 256}),
            )
        assert caught.value.code is OpenAIResponsesBackendErrorCode.INVALID_REQUEST
        assert state.requests == []


@pytest.mark.parametrize(
    ("content_type", "body", "expected_code"),
    (
        (
            "text/plain",
            b"{}",
            OpenAIResponsesBackendErrorCode.INVALID_CONTENT_TYPE,
        ),
        (
            "application/json",
            b'{"id":"a","id":"b"}',
            OpenAIResponsesBackendErrorCode.INVALID_JSON,
        ),
        (
            "application/json",
            b'{"id":"a","model":"provider-model-snapshot-2026-07-20",'
            b'"usage":{"input_tokens":NaN,"output_tokens":0,"total_tokens":0}}',
            OpenAIResponsesBackendErrorCode.INVALID_JSON,
        ),
    ),
)
def test_nonstream_content_type_and_json_are_strict(
    content_type: str,
    body: bytes,
    expected_code: OpenAIResponsesBackendErrorCode,
):
    def respond(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("x-request-id", PROVIDER_REQUEST_ID)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    with fake_server(respond) as (base_url, _state):
        backend = backend_for(base_url)
        with pytest.raises(OpenAIResponsesBackendError) as caught:
            backend.create(CREDENTIAL, request_for())

    assert caught.value.code is expected_code
    assert_sanitized(caught.value)


@pytest.mark.parametrize(
    "payload",
    (
        response_payload(model="wrong-model"),
        {
            **response_payload(),
            "usage": {
                "input_tokens": 2,
                "output_tokens": 3,
                "total_tokens": 99,
            },
        },
        {
            **response_payload(),
            "usage": {
                "input_tokens": True,
                "output_tokens": 3,
                "total_tokens": 4,
            },
        },
    ),
)
def test_exact_model_and_usage_validation_rejects_provider_drift(
    payload: dict[str, Any],
):
    def respond(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        send_json(handler, payload)

    with fake_server(respond) as (base_url, _state):
        backend = backend_for(base_url)
        with pytest.raises(OpenAIResponsesBackendError) as caught:
            backend.create(CREDENTIAL, request_for())

    assert caught.value.code is OpenAIResponsesBackendErrorCode.INVALID_RESPONSE
    assert caught.value.request_id == PROVIDER_REQUEST_ID


def test_missing_or_ambiguous_provider_request_id_fails_closed():
    def missing(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        body = json.dumps(response_payload()).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    with fake_server(missing) as (base_url, _state):
        with pytest.raises(OpenAIResponsesBackendError) as caught:
            backend_for(base_url).create(CREDENTIAL, request_for())
    assert caught.value.code is OpenAIResponsesBackendErrorCode.INVALID_HEADERS

    def duplicate(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        body = json.dumps(response_payload()).encode()
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("x-request-id", "first-request")
        handler.send_header("x-request-id", "second-request")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    with fake_server(duplicate) as (base_url, _state):
        with pytest.raises(OpenAIResponsesBackendError) as caught:
            backend_for(base_url).create(CREDENTIAL, request_for())
    assert caught.value.code is OpenAIResponsesBackendErrorCode.INVALID_HEADERS


def test_read_timeout_is_typed_sanitized_and_not_retried():
    release = threading.Event()
    headers_sent = threading.Event()

    def respond(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("x-request-id", PROVIDER_REQUEST_ID)
        handler.send_header("Content-Length", "10")
        handler.end_headers()
        handler.wfile.flush()
        headers_sent.set()
        release.wait(2)

    try:
        with fake_server(respond) as (base_url, state):
            backend = backend_for(base_url, read_timeout_seconds=0.1)
            outcome: list[BaseException | object] = []

            def invoke() -> None:
                try:
                    outcome.append(backend.create(CREDENTIAL, request_for()))
                except BaseException as exc:
                    outcome.append(exc)

            caller = threading.Thread(target=invoke, daemon=True)
            caller.start()
            assert headers_sent.wait(2)
            caller.join(timeout=2)
            assert not caller.is_alive()
            assert len(outcome) == 1
            assert isinstance(outcome[0], OpenAIResponsesBackendError)
            error = outcome[0]
            assert error.code is OpenAIResponsesBackendErrorCode.TIMEOUT
            assert error.retryable is True
            assert error.request_id == PROVIDER_REQUEST_ID
            assert len(state.requests) == 1
            assert_sanitized(error)
    finally:
        release.set()


def test_sse_stream_extracts_terminal_response_request_model_and_usage():
    def respond(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        send_sse_headers(handler)
        created = {
            "type": "response.created",
            "sequence_number": 0,
            "response": {
                "id": "resp-stream-1",
                "object": "response",
                "created_at": 1_721_500_000,
                "status": "in_progress",
                "model": MODEL,
                "output": [],
            },
        }
        completed = {
            "type": "response.completed",
            "sequence_number": 1,
            "response": response_payload(
                response_id="resp-stream-1",
                input_tokens=13,
                output_tokens=5,
            ),
        }
        write_sse(handler, created, event_name="response.created")
        write_sse(handler, completed, event_name="response.completed")
        handler.wfile.write(b"data: [DONE]\n\n")
        handler.wfile.flush()

    with fake_server(respond) as (base_url, state):
        backend = backend_for(base_url)
        chunks = list(backend.stream(CREDENTIAL, request_for(stream=True)))

    assert len(chunks) == 2
    assert chunks[0].event["type"] == "response.created"
    assert chunks[0].final_response is None
    final = chunks[1].final_response
    assert final is not None
    assert final.request_id == PROVIDER_REQUEST_ID
    assert final.model == MODEL
    assert final.response["id"] == "resp-stream-1"
    assert final.usage.input_tokens == 13
    assert final.usage.output_tokens == 5
    assert state.requests[0].headers["accept"] == "text/event-stream"
    assert json.loads(state.requests[0].body)["stream"] is True
    assert not state.handler_errors


@pytest.mark.parametrize(
    "mode",
    ("event_name_mismatch", "unknown_field", "oversized_event"),
)
def test_sse_framing_and_event_size_fail_closed(mode: str):
    def respond(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        send_sse_headers(handler)
        if mode == "event_name_mismatch":
            write_sse(
                handler,
                {"type": "response.created", "sequence_number": 0},
                event_name="response.completed",
            )
        elif mode == "unknown_field":
            handler.wfile.write(b"retry: 1000\n")
            handler.wfile.write(
                b'data: {"type":"response.created","sequence_number":0}\n\n'
            )
            handler.wfile.flush()
        else:
            handler.wfile.write(b"data: " + (b"x" * 300) + b"\n\n")
            handler.wfile.flush()

    with fake_server(respond) as (base_url, _state):
        backend = backend_for(
            base_url,
            max_stream_event_bytes=256,
            max_response_bytes=512,
        )
        with pytest.raises(OpenAIResponsesBackendError) as caught:
            list(backend.stream(CREDENTIAL, request_for(stream=True)))

    expected = (
        OpenAIResponsesBackendErrorCode.RESPONSE_TOO_LARGE
        if mode == "oversized_event"
        else OpenAIResponsesBackendErrorCode.INVALID_SSE
    )
    assert caught.value.code is expected
    assert_sanitized(caught.value)


def test_pre_cancelled_request_never_connects():
    def respond(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        send_json(handler, response_payload())

    cancelled = threading.Event()
    cancelled.set()
    with fake_server(respond) as (base_url, state):
        backend = backend_for(base_url)
        with pytest.raises(ResponsesCancelled):
            backend.create(
                CREDENTIAL,
                request_for(cancel_event=cancelled),
            )
    assert state.requests == []


def test_active_stream_cancel_closes_connection_and_unblocks_reader():
    first_sent = threading.Event()
    release_server = threading.Event()

    def respond(handler: BaseHTTPRequestHandler, _state: ServerState) -> None:
        send_sse_headers(handler)
        write_sse(
            handler,
            {"type": "response.created", "sequence_number": 0},
            event_name="response.created",
        )
        first_sent.set()
        release_server.wait(3)
        write_sse(
            handler,
            {
                "type": "response.completed",
                "sequence_number": 1,
                "response": response_payload(response_id="resp-cancelled"),
            },
            event_name="response.completed",
        )

    try:
        with fake_server(respond) as (base_url, state):
            backend = backend_for(base_url, read_timeout_seconds=5)
            request = request_for(stream=True)
            iterator = iter(backend.stream(CREDENTIAL, request))
            first = next(iterator)
            assert first.event["type"] == "response.created"
            assert first_sent.wait(1)

            outcome: list[BaseException | object] = []

            def read_next() -> None:
                try:
                    outcome.append(next(iterator))
                except BaseException as exc:
                    outcome.append(exc)

            reader = threading.Thread(target=read_next, daemon=True)
            reader.start()
            time.sleep(0.05)
            backend.cancel(LOCAL_REQUEST_ID)
            reader.join(timeout=2)

            assert not reader.is_alive()
            assert len(outcome) == 1
            assert isinstance(outcome[0], ResponsesCancelled)
            assert request.cancel_event.is_set()
            assert len(state.requests) == 1
    finally:
        release_server.set()
