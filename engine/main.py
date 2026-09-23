"""FastAPI application factory and lifecycle.

Wires configuration, structured logging, SQLite/WAL migrations, health endpoints
(Block 0), and the OpenAI-compatible API edge over the internal inference backend
(Blocks 1–2). The API edge never calls llama.cpp directly — it translates onto the
internal ``GenerationRequest``/token-stream contract.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from engine import __version__
from engine.api import health
from engine.api.admin_router import router as admin_router
from engine.api.anthropic_router import router as anthropic_router
from engine.api.billing_router import router as billing_router
from engine.api.errors import error_response, register_exception_handlers
from engine.api.models_router import router as models_router
from engine.api.openai_router import router as openai_router
from engine.api.ops_router import router as ops_router
from engine.api.ws_router import router as ws_router
from engine.audit import AuditLog
from engine.auth.keys import KeyStore
from engine.billing.store import BillingStore
from engine.config import Settings, load_config
from engine.gateway import Gateway
from engine.inference.base import InferenceBackend
from engine.inference.factory import build_backend, build_remote_worker
from engine.inference.registry import BackendEntry, BackendRegistry
from engine.inference.router import Router, VirtualModel
from engine.inference.scheduler import Scheduler
from engine.inference.types import BackendError
from engine.logging_setup import configure_logging, get_logger
from engine.models.registry import ModelRegistry
from engine.models.service import ModelService
from engine.quota.store import UsageStore
from engine.store.db import connect
from engine.store.migrations import apply_migrations, migrations_at_head
from engine.telemetry.counters import Counters
from engine.telemetry.events import EventBus
from engine.telemetry.logbuffer import LogCollector
from engine.telemetry.route_metrics import RouteMetrics
from engine.telemetry.service import Telemetry

# The active settings for the running app. Set by ``create_app`` so request
# handlers (e.g. readiness) can access configuration without a DI framework.
_active_settings: Settings | None = None


def get_settings() -> Settings:
    if _active_settings is None:
        raise RuntimeError("Application settings are not initialized")
    return _active_settings


def _client_ip(request: Request, *, trust_forwarded_for: bool) -> str | None:
    """Resolve the caller's IP for the trusted-network policy.

    Uses the direct peer by default. Only when ``trust_forwarded_for`` is set (i.e.
    the app sits behind a trusted reverse proxy) is the left-most ``X-Forwarded-For``
    entry used; otherwise a client could spoof the header to bypass the allowlist.
    """
    if trust_forwarded_for:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return request.client.host if request.client else None


def _ip_permitted(
    client_ip: str, networks: Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network]
) -> bool:
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def _fixed_provider(
    backend: InferenceBackend,
) -> Callable[[], InferenceBackend | None]:
    """A registry provider that always returns one fixed backend (remote workers)."""

    def _provider() -> InferenceBackend | None:
        return backend

    return _provider


def create_app(
    settings: Settings | None = None,
    backend: InferenceBackend | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    If ``settings`` is not supplied it is loaded from the standard layered
    sources. If ``backend`` is supplied it is used as-is (tests inject a fake);
    otherwise, when a model is configured, a backend is built and loaded on
    startup. The app always starts even if no model is configured or loading
    fails — the ``/v1`` endpoints then report ``503 model_not_loaded``.
    """
    global _active_settings
    settings = settings or load_config()
    _active_settings = settings
    injected_backend = backend is not None

    configure_logging(settings.log_level)
    log = get_logger("engine.startup")

    # Operability core (Block 4): event bus, counters, telemetry service, and a
    # bounded structured-log mirror installed on the root logger.
    event_bus = EventBus(
        history=settings.event_history_size,
        subscriber_queue=settings.event_subscriber_queue,
    )
    counters = Counters()
    telemetry = Telemetry(
        bus=event_bus,
        counters=counters,
        data_dir=settings.data_dir,
        sample_interval_s=settings.metrics_interval_s,
    )
    log_collector = LogCollector(
        settings.db_path,
        ring_size=settings.log_ring_size,
        max_rows=settings.log_events_max_rows,
    )
    logging.getLogger().addHandler(log_collector)

    # Controlled concurrency (Block 7): admission control in front of the backend.
    scheduler = Scheduler(
        max_concurrency=settings.max_concurrency,
        max_queue_depth=settings.max_concurrency_queue,
        queue_timeout_s=settings.concurrency_queue_timeout_s,
    )
    telemetry.scheduler = scheduler

    # Model lifecycle (Block 6): registry + orchestration service.
    model_registry = ModelRegistry(settings.db_path)  # type: ignore[arg-type]
    model_service = ModelService(settings, model_registry)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        log.info(
            "startup",
            extra={
                "version": __version__,
                "host": settings.host,
                "port": settings.port,
                "db_path": str(settings.db_path),
                "data_dir": str(settings.data_dir),
            },
        )
        conn = connect(settings.db_path)  # type: ignore[arg-type]
        try:
            applied = apply_migrations(conn)
            app.state.migrations_ready = migrations_at_head(conn)
        finally:
            conn.close()
        log.info("migrations_applied", extra={"newly_applied": applied})

        # Seed the default plan (Block 11) once the billing tables exist, mirroring
        # the engine-wide quota limits so an owned key on the default plan behaves
        # like an unowned key. Operators refine plans via /admin/billing/plans.
        if settings.default_plan:
            billing_store.upsert_plan(
                id=settings.default_plan,
                name=settings.default_plan,
                quota_5h_cu=settings.quota_5h_cu,
                quota_weekly_cu=settings.quota_weekly_cu,
            )

        # Register the configured model in the registry (Block 6) so it appears in
        # the dashboard, then build + load it unless a backend was injected.
        if settings.model_path is not None:
            model_service.register_configured(settings.model_path, settings.model_id)
        # A backend is configured either by a local model_path (llama.cpp) or by
        # selecting a remote adapter (Block 10), which needs no local model file.
        backend_configured = settings.model_path is not None or settings.backend_kind != "llamacpp"
        if not injected_backend and backend_configured:
            try:
                built = build_backend(settings)
                await built.load()
                app.state.backend = built
                log.info("model_loaded", extra={"model_id": settings.model_id})
            except BackendError as exc:
                app.state.backend = None
                log.warning("model_load_failed", extra={"error": str(exc)})

        # Load any additional remote workers (Block 10, sub-slice 3). A worker that
        # fails to load is left unavailable; the registry simply routes elsewhere.
        for worker in getattr(app.state, "remote_workers", []):
            try:
                await worker.load()
                log.info("remote_worker_loaded", extra={"backend": worker.name})
            except BackendError as exc:
                log.warning(
                    "remote_worker_load_failed",
                    extra={"backend": worker.name, "error": str(exc)},
                )

        # Periodic backend liveness refresh so routing skips a remote that went down.
        health_task: asyncio.Task[None] | None = None
        if settings.backend_health_interval_s > 0 and (
            settings.remote_workers or settings.external_providers
        ):
            registry = app.state.backend_registry

            async def _health_loop() -> None:
                while True:
                    await asyncio.sleep(settings.backend_health_interval_s)
                    try:
                        await registry.refresh_health()
                    except Exception:  # pragma: no cover - defensive
                        log.warning("backend_health_refresh_failed")

            health_task = asyncio.create_task(_health_loop())

        # Start the metrics sampler once the loop is running (skip if disabled).
        if settings.metrics_interval_s > 0:
            telemetry.start(lambda: getattr(app.state, "backend", None))

        # gRPC edge (Block 10.6): an independently secured service on its own port,
        # sharing this app's state (auth, registry, router, metering).
        grpc_server = None
        if settings.grpc_enabled:
            from engine.grpc.server import create_grpc_server

            grpc_server, bound = await create_grpc_server(
                app,
                host=settings.grpc_host,
                port=settings.grpc_port,
                tls_cert=settings.grpc_tls_cert,
                tls_key=settings.grpc_tls_key,
            )
            await grpc_server.start()
            app.state.grpc_port = bound
            log.info(
                "grpc_started", extra={"port": bound, "tls": settings.grpc_tls_cert is not None}
            )

        try:
            yield
        finally:
            if grpc_server is not None:
                await grpc_server.stop(grace=5.0)
                log.info("grpc_stopped")
            # Graceful drain (Block 9): stop admitting new work and let bounded
            # in-flight generations finish (or time out) before releasing the model,
            # so a rolling restart never severs active requests.
            scheduler.begin_drain()
            drained = await scheduler.wait_drained(settings.drain_timeout_s)
            log.info("drain_complete", extra={"drained": drained})
            if health_task is not None:
                health_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await health_task
            for worker in getattr(app.state, "remote_workers", []):
                with contextlib.suppress(Exception):
                    await worker.unload()
            await telemetry.stop()
            await model_service.shutdown()
            logging.getLogger().removeHandler(log_collector)
            active: InferenceBackend | None = getattr(app.state, "backend", None)
            if active is not None and not injected_backend:
                await active.unload()
            log.info("shutdown")

    app = FastAPI(
        title="Inference Engine",
        version=__version__,
        summary=(
            "Local GGUF inference: OpenAI + Anthropic APIs, dashboard, "
            "model lifecycle (Blocks 0–8)."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.backend = backend
    # Backend registry (Block 10, sub-slice 3): the primary backend is entry 0
    # (tracked live so admin load/unload swaps are reflected); remote_workers add
    # more OpenAI-compatible backends. Requests route to the least-busy healthy one.
    _primary_entry = BackendEntry(
        name="primary",
        kind=settings.backend_kind,
        is_local=settings.backend_kind == "llamacpp",
        provider=lambda: getattr(app.state, "backend", None),
        max_in_flight=settings.primary_max_in_flight,
        prefix_cache=settings.backend_kind != "llamacpp" and settings.remote_prefix_cache,
        cost_per_1k_input=settings.primary_cost_per_1k_input,
        cost_per_1k_output=settings.primary_cost_per_1k_output,
    )
    app.state.remote_workers = []
    _worker_entries = []
    for _spec in settings.remote_workers:
        _wb = build_remote_worker(_spec)
        app.state.remote_workers.append(_wb)
        _worker_entries.append(
            BackendEntry(
                name=_spec.name,
                kind=_spec.kind,
                is_local=False,
                provider=_fixed_provider(_wb),
                max_in_flight=_spec.max_in_flight,
                prefix_cache=_spec.prefix_cache,
                cost_per_1k_input=_spec.cost_per_1k_input,
                cost_per_1k_output=_spec.cost_per_1k_output,
            )
        )
    # External providers (Block 10.7): OpenAI-compatible spillover backends, used
    # only when the local pool is saturated. Egress is already validated (kill
    # switch + allowlist) in config; here they join the pool tagged spillover.
    _external_entries = []
    for _spec in settings.external_providers:
        _eb = build_remote_worker(_spec)
        app.state.remote_workers.append(_eb)  # loaded/unloaded with the other remotes
        _external_entries.append(
            BackendEntry(
                name=_spec.name,
                kind=_spec.kind,
                is_local=False,
                provider=_fixed_provider(_eb),
                max_in_flight=_spec.max_in_flight,
                prefix_cache=_spec.prefix_cache,
                cost_per_1k_input=_spec.cost_per_1k_input,
                cost_per_1k_output=_spec.cost_per_1k_output,
                spillover=True,
                external=True,
            )
        )
    app.state.backend_registry = BackendRegistry(
        [_primary_entry, *_worker_entries, *_external_entries]
    )
    # Virtual auto-models (Block 10, sub-slice 5a): resolve named route/cascade
    # policies over the pool. Referenced backend names must exist, else fail fast.
    _pool_names = {
        "primary",
        *(spec.name for spec in settings.remote_workers),
        *(spec.name for spec in settings.external_providers),
    }
    _vmodels: list[VirtualModel] = []
    for _vm in settings.virtual_models:
        _steps = _vm.normalized_steps()
        _unknown = sorted({n for step in _steps for n in step} - _pool_names)
        if _unknown:
            raise ValueError(
                f"virtual model {_vm.name!r} references unknown backend(s): "
                + ", ".join(_unknown)
                + f"; known backends: {', '.join(sorted(_pool_names))}"
            )
        _vmodels.append(
            VirtualModel(name=_vm.name, policy=_vm.policy, steps=tuple(tuple(s) for s in _steps))
        )
    app.state.router = Router(app.state.backend_registry, _vmodels)
    app.state.route_metrics = RouteMetrics()
    billing_store = BillingStore(
        settings.db_path,  # type: ignore[arg-type]
        default_plan_id=settings.default_plan,
    )
    app.state.billing = billing_store
    app.state.gateway = Gateway(
        settings,
        KeyStore(settings.db_path),  # type: ignore[arg-type]
        UsageStore(settings.db_path),  # type: ignore[arg-type]
        billing_store,
    )
    app.state.event_bus = event_bus
    app.state.counters = counters
    app.state.telemetry = telemetry
    app.state.scheduler = scheduler
    app.state.audit = AuditLog(settings.db_path)  # type: ignore[arg-type]

    # Trusted-network policy (Block 9b): parse the IP allowlist once.
    allow_networks = [ipaddress.ip_network(c, strict=False) for c in settings.ip_allowlist]
    app.state.log_collector = log_collector
    app.state.model_registry = model_registry
    app.state.model_service = model_service

    @app.middleware("http")
    async def _assign_request_id(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        # A correlation id available to error handlers even for requests that fail
        # validation before the endpoint runs. The OpenAI edge overrides this with
        # its ``chatcmpl-`` id once it starts serving.
        incoming = request.headers.get("x-request-id")
        request.state.request_id = incoming or f"req-{uuid.uuid4().hex}"
        return await call_next(request)

    @app.middleware("http")
    async def _limit_body_size(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        max_bytes = settings.max_request_bytes
        if max_bytes > 0:
            length = request.headers.get("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                return error_response(
                    "request body too large",
                    status_code=413,
                    type="invalid_request_error",
                    code="payload_too_large",
                )
        return await call_next(request)

    @app.middleware("http")
    async def _ip_allowlist(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
        # Trusted-network policy (Block 9b): when an allowlist is configured, only
        # permitted client IPs may reach any endpoint. Registered last so it is the
        # outermost middleware and rejects before any other work happens.
        if allow_networks:
            client_ip = _client_ip(request, trust_forwarded_for=settings.trust_forwarded_for)
            if client_ip is None or not _ip_permitted(client_ip, allow_networks):
                return error_response(
                    "client address not permitted",
                    status_code=403,
                    type="invalid_request_error",
                    code="ip_not_allowed",
                )
        return await call_next(request)

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(openai_router)
    app.include_router(anthropic_router)
    app.include_router(ops_router)
    app.include_router(ws_router)
    app.include_router(admin_router)
    app.include_router(models_router)
    app.include_router(billing_router)
    _mount_dashboard(app)
    return app


def _mount_dashboard(app: FastAPI) -> None:
    """Serve the built operator dashboard SPA from ``engine/static`` if present.

    The SPA is a build artifact (not committed); when it has not been built the
    engine still runs headless and the API is unaffected. Access control lives on
    the data endpoints (all operator surfaces are gated), so the static shell is
    served without relying on route obscurity: it is useless without a valid key
    when the server is network-bound.
    """
    static_dir = Path(__file__).parent / "static"
    if not (static_dir / "index.html").is_file():
        return
    app.mount("/dashboard", StaticFiles(directory=static_dir, html=True), name="dashboard")

    @app.get("/", include_in_schema=False)
    def _root() -> RedirectResponse:
        return RedirectResponse(url="/dashboard/")
