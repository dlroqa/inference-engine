# gRPC edge (Block 10, sub-slice 6)

The engine can expose an **independently secured gRPC service** on its own port,
alongside the HTTP APIs. It reuses the *same* serving pipeline — API-key auth,
admission control, virtual-model routing, spillover, generation, and per-route
metering — so nothing bypasses the gateway or registry; only the transport (and
the API-key source: call metadata) differs.

It is **off by default**. Enable it and pick a port:

```toml
grpc_enabled = true
grpc_host    = "127.0.0.1"     # non-loopback requires allow_network_bind=true
grpc_port    = 50051           # 0 = OS-assigned
# grpc_tls_cert = "/etc/ie/grpc.crt"   # set BOTH for a secure port
# grpc_tls_key  = "/etc/ie/grpc.key"
```

Install the optional extra (native, prebuilt wheels): `pip install "inference-engine[grpc]"`.

## Service

`InferenceService` (see [`engine/grpc/inference.proto`](../engine/grpc/inference.proto)):

- `Generate(GenerateRequest) returns (stream GenerateChunk)` — server-streaming
  token deltas, then a terminal chunk (`done=true`) carrying `finish_reason` and
  token usage.
- `ListModels(ListModelsRequest) returns (ListModelsResponse)` — the models a
  client may request (base served id + virtual model names).

`GenerateRequest.model` accepts the base served id **or** a virtual model name
(route/cascade policies apply). Send **either** `messages` (chat) or `prompt`.

## Auth & security

- **API key** travels in call metadata: `authorization: Bearer <key>` (or
  `x-api-key`). The same policy as HTTP applies — required when not loopback-bound
  or when `require_auth=true`.
- **TLS**: provide both `grpc_tls_cert` and `grpc_tls_key` for a secure port
  (mTLS-ready via client certs at your proxy/mesh); otherwise the port is
  insecure — front it with a TLS-terminating proxy, exactly as for HTTP.
- **Errors** map to gRPC status codes: `UNAUTHENTICATED` (401), `RESOURCE_EXHAUSTED`
  (429/413 saturation), `UNAVAILABLE` (no model ready), `NOT_FOUND` (unknown model),
  `INVALID_ARGUMENT` (bad request).
- **No silent failover after partial tokens**: the backend is chosen once at
  admission; the stream never re-routes (same rule as the HTTP edges).

## Notes

- proto3 scalars can't distinguish unset from zero, so `0` means "use the default"
  for `max_tokens`/`temperature`/`top_p`/`top_k` (use a tiny temperature for greedy
  decoding). A presence-aware revision can use `optional` fields later.
- The generated stubs (`engine/grpc/inference_pb2*.py`) are committed. Regenerate
  after editing the proto:

  ```bash
  python -m grpc_tools.protoc -I engine/grpc \
    --python_out=engine/grpc --grpc_python_out=engine/grpc engine/grpc/inference.proto
  # then make the grpc stub import package-relative:
  #   from engine.grpc import inference_pb2 as inference__pb2
  ```

## Deferred (benchmark-gated)

Kubernetes topology, MoE, and prefill/decode disaggregation are explicitly gated
on real benchmarks and are **not** part of this slice. The gRPC service shares the
process with HTTP here; running it as a separate deployment is a packaging/k8s
concern for when those are benchmarked.
