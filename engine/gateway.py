"""The secure gateway: authentication, limits, quota, and attribution.

Ties together the key store, the in-memory limiter (rate + concurrency), and the
compute-unit quota over the usage store. Every inference request is authenticated
(when required), limited, quota-checked, and recorded against a key + request id.

State that must be shared across replicas (rate/quota) is in-process here; moving
it to Redis is a later, scale-time block.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from fastapi import Request

from engine.api.errors import OpenAIError
from engine.auth.keys import KeyStore
from engine.config import Settings
from engine.quota.compute import ComputeModel, estimate_prompt_tokens
from engine.quota.store import UsageStore, WindowUsage

LOCAL_KEY_ID = "local"  # unauthenticated loopback attribution


def extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[len("bearer ") :].strip()
    x_api_key = request.headers.get("x-api-key")
    if x_api_key:
        return x_api_key.strip()
    return None


class Limiter:
    """In-memory per-key rate (fixed 1-minute window) and concurrency limiter."""

    def __init__(self, rate_per_min: int, max_concurrent: int) -> None:
        self._rate = rate_per_min
        self._max_concurrent = max_concurrent
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[int, int]] = {}  # key -> (minute_bucket, count)
        self._inflight: dict[str, int] = {}

    def check_rate(self, key_id: str, now: float | None = None) -> bool:
        if self._rate <= 0:
            return True
        bucket = int((now if now is not None else time.time()) // 60)
        with self._lock:
            cur_bucket, count = self._windows.get(key_id, (bucket, 0))
            if cur_bucket != bucket:
                cur_bucket, count = bucket, 0
            if count >= self._rate:
                self._windows[key_id] = (cur_bucket, count)
                return False
            self._windows[key_id] = (cur_bucket, count + 1)
            return True

    def acquire(self, key_id: str) -> bool:
        if self._max_concurrent <= 0:
            with self._lock:
                self._inflight[key_id] = self._inflight.get(key_id, 0) + 1
            return True
        with self._lock:
            current = self._inflight.get(key_id, 0)
            if current >= self._max_concurrent:
                return False
            self._inflight[key_id] = current + 1
            return True

    def release(self, key_id: str) -> None:
        with self._lock:
            current = self._inflight.get(key_id, 0)
            if current <= 1:
                self._inflight.pop(key_id, None)
            else:
                self._inflight[key_id] = current - 1


@dataclass(slots=True)
class ApiAccess:
    """Handle for one authorized request; finalize records usage + frees a slot."""

    gateway: Gateway
    key_id: str
    endpoint: str
    headers: dict[str, str] = field(default_factory=dict)
    _slot_held: bool = False
    _finalized: bool = False

    def finalize(
        self,
        *,
        request_id: str,
        model: str | None,
        prompt_tokens: int,
        completion_tokens: int,
        status: int,
    ) -> None:
        if self._finalized:
            return
        self._finalized = True
        try:
            cu = self.gateway.compute.actual(prompt_tokens, completion_tokens)
            self.gateway.usage.record(
                key_id=self.key_id,
                request_id=request_id,
                endpoint=self.endpoint,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cu=cu,
                status=status,
            )
        finally:
            if self._slot_held:
                self.gateway.limiter.release(self.key_id)
                self._slot_held = False

    def abort(self) -> None:
        """Release resources without recording usage (e.g. pre-generation error)."""
        if self._finalized:
            return
        self._finalized = True
        if self._slot_held:
            self.gateway.limiter.release(self.key_id)
            self._slot_held = False


class Gateway:
    def __init__(self, settings: Settings, key_store: KeyStore, usage_store: UsageStore) -> None:
        self.settings = settings
        self.keys = key_store
        self.usage = usage_store
        self.compute = ComputeModel(
            prompt_weight=settings.cu_prompt_weight,
            completion_weight=settings.cu_completion_weight,
        )
        self.limiter = Limiter(settings.rate_limit_per_min, settings.max_concurrent_per_key)

    def _authenticate(self, request: Request) -> str:
        token = extract_token(request)
        required = self.settings.effective_require_auth()
        if token:
            record = self.keys.verify(token)
            if record is not None:
                return record.id
            # A recognized key wins; an unrecognized token is rejected only when
            # auth is required. On loopback dev (auth off) any token is accepted
            # as anonymous, so OpenAI SDK clients (which always send a key) work.
            if required:
                raise OpenAIError(
                    "invalid API key",
                    status_code=401,
                    type="invalid_request_error",
                    code="invalid_api_key",
                )
            return LOCAL_KEY_ID
        if required:
            raise OpenAIError(
                "missing API key",
                status_code=401,
                type="invalid_request_error",
                code="missing_api_key",
            )
        return LOCAL_KEY_ID

    def _quota_headers(self, u5h: WindowUsage, uweek: WindowUsage) -> dict[str, str]:
        headers: dict[str, str] = {}
        if u5h.limit > 0:
            headers["x-ratelimit-limit-cu-5h"] = str(int(u5h.limit))
            headers["x-ratelimit-remaining-cu-5h"] = str(int(u5h.remaining))
            headers["x-ratelimit-reset-cu-5h"] = str(int(u5h.reset_at))
        if uweek.limit > 0:
            headers["x-ratelimit-limit-cu-week"] = str(int(uweek.limit))
            headers["x-ratelimit-remaining-cu-week"] = str(int(uweek.remaining))
            headers["x-ratelimit-reset-cu-week"] = str(int(uweek.reset_at))
        return headers

    def authorize(
        self, request: Request, *, prompt_text: str, max_tokens: int, endpoint: str
    ) -> ApiAccess:
        key_id = self._authenticate(request)
        access = ApiAccess(gateway=self, key_id=key_id, endpoint=endpoint)

        # Unauthenticated loopback ("local") is attributed but not limited/quota'd.
        if key_id == LOCAL_KEY_ID:
            return access

        now = time.time()
        if not self.limiter.check_rate(key_id, now):
            raise OpenAIError(
                "rate limit exceeded",
                status_code=429,
                type="rate_limit_error",
                code="rate_limit_exceeded",
                headers={"retry-after": "60"},
            )
        if not self.limiter.acquire(key_id):
            raise OpenAIError(
                "too many concurrent requests",
                status_code=429,
                type="rate_limit_error",
                code="concurrency_limit_exceeded",
                headers={"retry-after": "5"},
            )
        access._slot_held = True

        # Quota pre-check (fail-closed on either window).
        estimate = self.compute.estimate(estimate_prompt_tokens(prompt_text), max_tokens)
        u5h = self.usage.usage_5h(key_id, self.settings.quota_5h_cu, now)
        uweek = self.usage.usage_weekly(key_id, self.settings.quota_weekly_cu, now)
        headers = self._quota_headers(u5h, uweek)
        if uweek.would_exceed(estimate):
            access.abort()
            raise OpenAIError(
                "weekly compute quota exceeded",
                status_code=429,
                type="rate_limit_error",
                code="quota_exceeded",
                headers={**headers, "retry-after": str(int(uweek.reset_at - now))},
            )
        if u5h.would_exceed(estimate):
            access.abort()
            raise OpenAIError(
                "5-hour compute quota exceeded",
                status_code=429,
                type="rate_limit_error",
                code="quota_exceeded",
                headers={**headers, "retry-after": "60"},
            )
        access.headers = headers
        return access
