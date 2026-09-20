"""Async token-stream bridge.

Blocking backends (llama.cpp is a blocking C call) run their generation loop in a
**dedicated worker thread**; tokens are handed to the asyncio event loop through a
bounded queue so the loop is never blocked. Backpressure is applied with a
semaphore (the producer thread waits when the consumer falls behind, rather than
buffering without bound). Closing the stream cancels the worker and releases it.

The producer is any callable ``producer(cancel) -> Iterator[str]`` — a generator
that yields token text and may ``return`` a :class:`FinishReason`. It should poll
``cancel.is_set()`` between tokens so cancellation is prompt.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from engine.inference.base import GenerationStream
from engine.inference.types import (
    FinishReason,
    GenerationChunk,
    GenerationFailedError,
    GenerationRequest,
    GenerationResult,
    GenerationTimings,
    TokenGenerator,
)

Producer = Callable[[threading.Event], TokenGenerator]


@dataclass(slots=True)
class _Token:
    text: str


@dataclass(slots=True)
class _Terminal:
    finish: FinishReason
    error: BaseException | None


class QueueGenerationStream(GenerationStream):
    """Bridges a blocking token producer (in a thread) to an async iterator."""

    def __init__(
        self,
        *,
        request: GenerationRequest,
        prompt_tokens: int,
        producer: Producer,
        on_done: Callable[[], None] | None = None,
        maxsize: int = 64,
    ) -> None:
        self._request = request
        self._prompt_tokens = prompt_tokens
        self._producer = producer
        self._on_done = on_done
        self._loop = asyncio.get_event_loop()
        self._queue: asyncio.Queue[_Token | _Terminal] = asyncio.Queue(maxsize=maxsize)
        self._capacity = threading.Semaphore(maxsize)
        self._cancel = threading.Event()

        self._index = 0
        self._parts: list[str] = []
        self._timings = GenerationTimings(started_at=time.monotonic())
        self._result: GenerationResult | None = None
        self._exhausted = False
        self._closed = False
        self._on_done_fired = False

        self._thread = threading.Thread(
            target=self._run,
            name=f"gen-{request.request_id or 'anon'}",
            daemon=True,
        )
        self._thread.start()

    # -- producer thread ---------------------------------------------------

    def _run(self) -> None:
        finish = FinishReason.STOP
        error: BaseException | None = None
        try:
            gen = self._producer(self._cancel)
            while True:
                if self._cancel.is_set():
                    finish = FinishReason.CANCELLED
                    gen.close()
                    break
                self._capacity.acquire()
                if self._cancel.is_set():
                    self._capacity.release()
                    finish = FinishReason.CANCELLED
                    gen.close()
                    break
                try:
                    token = next(gen)
                except StopIteration as stop:
                    self._capacity.release()
                    finish = (
                        stop.value if isinstance(stop.value, FinishReason) else FinishReason.STOP
                    )
                    break
                except BaseException as exc:  # producer failed mid-stream
                    self._capacity.release()
                    error = exc
                    finish = FinishReason.ERROR
                    break
                self._offer(_Token(text=token))
        except BaseException as exc:  # defensive: keep the thread from dying silently
            error = exc
            finish = FinishReason.ERROR
        self._offer(_Terminal(finish=finish, error=error))
        # The worker is now finished. Release the backend from the loop thread so
        # state resets even if the consumer was cancelled (client disconnect) and
        # never awaits the terminal or aclose().
        try:
            self._loop.call_soon_threadsafe(self._fire_on_done)
        except RuntimeError:
            pass

    def _offer(self, item: _Token | _Terminal) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:
            # Event loop is gone; nothing to deliver to.
            self._cancel.set()

    # -- async consumer ----------------------------------------------------

    async def __anext__(self) -> GenerationChunk:
        if self._exhausted:
            raise StopAsyncIteration
        item = await self._queue.get()
        if isinstance(item, _Terminal):
            self._finish(item)
            if item.error is not None:
                raise GenerationFailedError(str(item.error)) from item.error
            raise StopAsyncIteration
        self._capacity.release()
        if self._timings.first_token_at is None:
            self._timings.first_token_at = time.monotonic()
        chunk = GenerationChunk(text=item.text, index=self._index)
        self._index += 1
        self._parts.append(item.text)
        return chunk

    def _finish(self, terminal: _Terminal) -> None:
        if self._result is None:
            self._timings.finished_at = time.monotonic()
            self._result = GenerationResult(
                request_id=self._request.request_id,
                text="".join(self._parts),
                finish_reason=terminal.finish,
                prompt_tokens=self._prompt_tokens,
                completion_tokens=self._index,
                timings=self._timings,
            )
        self._exhausted = True
        self._join()
        self._fire_on_done()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel.set()
        self._capacity.release()  # unblock the producer if it waits on capacity
        # Join off the event loop so we never block it.
        await self._loop.run_in_executor(None, self._join)
        if self._result is None:
            self._timings.finished_at = time.monotonic()
            self._result = GenerationResult(
                request_id=self._request.request_id,
                text="".join(self._parts),
                finish_reason=FinishReason.CANCELLED,
                prompt_tokens=self._prompt_tokens,
                completion_tokens=self._index,
                timings=self._timings,
            )
        self._exhausted = True
        self._fire_on_done()

    def _join(self) -> None:
        if self._thread.is_alive():
            self._thread.join(timeout=5.0)

    def _fire_on_done(self) -> None:
        if not self._on_done_fired:
            self._on_done_fired = True
            if self._on_done is not None:
                self._on_done()

    @property
    def result(self) -> GenerationResult | None:
        return self._result
