"""A minimal fake OpenAI-compatible server for remote-backend tests (vLLM/SGLang).

Runs as a real Uvicorn server (in a thread) so the remote backend exercises real
HTTP transport — streaming, mid-stream disconnects, and status codes — which the
in-process ASGI transport cannot reproduce (it buffers streams).
"""

from __future__ import annotations

import contextlib
import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

TOKENS = ["Hello", ", ", "world", "!"]


@dataclass
class FakeOpenAIServer:
    """Mutable behavior + call log for one fake OpenAI-compatible instance."""

    model: str = "meta-llama/fake"
    max_model_len: int = 8192
    mode: str = "ok"  # ok | prestream_error | http_400 | drop_after_first
    fail_times: int = 1  # how many leading attempts return an error (prestream_error)
    prompt_tokens: int = 7

    chat_calls: int = 0
    completion_calls: int = 0
    models_calls: int = 0
    last_auth: str | None = None
    last_request_id: str | None = None
    last_body: dict = field(default_factory=dict)

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                Route("/v1/models", self._models, methods=["GET"]),
                Route("/v1/chat/completions", self._chat, methods=["POST"]),
                Route("/v1/completions", self._completion, methods=["POST"]),
            ]
        )

    async def _models(self, request: Request) -> JSONResponse:
        self.models_calls += 1
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"id": self.model, "object": "model", "max_model_len": self.max_model_len}
                ],
            }
        )

    async def _chat(self, request: Request) -> object:
        self.chat_calls += 1
        self.last_auth = request.headers.get("authorization")
        self.last_request_id = request.headers.get("x-request-id")
        self.last_body = await request.json()
        early = self._maybe_error(self.chat_calls)
        if early is not None:
            return early
        return StreamingResponse(self._sse(chat=True), media_type="text/event-stream")

    async def _completion(self, request: Request) -> object:
        self.completion_calls += 1
        self.last_auth = request.headers.get("authorization")
        self.last_body = await request.json()
        early = self._maybe_error(self.completion_calls)
        if early is not None:
            return early
        return StreamingResponse(self._sse(chat=False), media_type="text/event-stream")

    def _maybe_error(self, call_n: int) -> JSONResponse | None:
        if self.mode == "prestream_error" and call_n <= self.fail_times:
            return JSONResponse({"error": "overloaded"}, status_code=503)
        if self.mode == "http_400":
            return JSONResponse({"error": "bad request"}, status_code=400)
        return None

    async def _sse(self, *, chat: bool) -> Iterator[str]:
        for i, tok in enumerate(TOKENS):
            if chat:
                chunk = {
                    "choices": [{"index": 0, "delta": {"content": tok}, "finish_reason": None}]
                }
            else:
                chunk = {"choices": [{"index": 0, "text": tok, "finish_reason": None}]}
            yield f"data: {json.dumps(chunk)}\n\n"
            if self.mode == "drop_after_first" and i == 0:
                # Abort mid-stream after one token: the client sees a transport error.
                raise RuntimeError("upstream dropped the connection")
        if chat:
            final = {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
        else:
            final = {"choices": [{"index": 0, "text": "", "finish_reason": "stop"}]}
        yield f"data: {json.dumps(final)}\n\n"
        usage = {
            "choices": [],
            "usage": {"prompt_tokens": self.prompt_tokens, "completion_tokens": len(TOKENS)},
        }
        yield f"data: {json.dumps(usage)}\n\n"
        yield "data: [DONE]\n\n"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


@contextlib.contextmanager
def serve(fake: FakeOpenAIServer) -> Iterator[str]:
    """Run ``fake`` on a real Uvicorn server; yield its base URL."""
    port = _free_port()
    config = uvicorn.Config(fake.app(), host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.05)
        if not server.started:
            raise RuntimeError("fake server did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
