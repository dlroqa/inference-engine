# API compatibility matrix

The engine exposes a **supported subset** of two provider dialects and aims to be
**drop-in** for their official SDKs:

- **OpenAI** — `POST /v1/chat/completions`, `GET /v1/models`
- **Anthropic** — `POST /v1/messages`

Both are **edge translations** onto one internal generation contract; no
provider-specific type reaches the inference worker. Requests **accept and ignore
unknown/benign optional fields** (so newer SDKs keep working) but **reject**
features whose behavior the caller depends on and that we cannot yet honor — a
clear error, never silently wrong output.

_Status as of Block 8._

## Endpoints

| Endpoint | Status | Notes |
|---|---|---|
| `POST /v1/chat/completions` | ✅ Supported | OpenAI; streaming (SSE) + non-streaming |
| `GET /v1/models` | ✅ Supported | Lists the single loaded model |
| `POST /v1/messages` | ✅ Supported | Anthropic; streaming (SSE) + non-streaming |
| `POST /v1/completions` | ❌ Not yet | Legacy OpenAI completions |
| `POST /v1/embeddings` | ❌ Not yet | Later block |

---

## OpenAI `POST /v1/chat/completions`

| Field | Status | Notes |
|---|---|---|
| `model` | ✅ | Must equal the loaded model id (else `404 model_not_found`) |
| `messages` | ✅ | Roles `system`/`user`/`assistant`; `content` is a string |
| `stream` | ✅ | SSE chunk framing; ends with `data: [DONE]` |
| `max_tokens` / `max_completion_tokens` | ✅ | The latter takes precedence |
| `temperature`, `top_p` | ✅ | 0–2 / 0–1 |
| `frequency_penalty`, `presence_penalty` | ✅ | −2–2; mapped to the backend sampler |
| `stop` | ✅ | String or array |
| `seed` | ✅ | Best-effort determinism (backend-dependent) |
| `response_format: {"type":"text"}` | ✅ | Default; produces text |
| `response_format: {"type":"json_object"}` | ✅¹ | Constrained JSON output |
| `response_format: {"type":"json_schema", …}` | ✅¹ | Constrained to `json_schema.schema` |
| `n` | ⚠️ | Only `n=1`; else `400` |
| `tools` / `functions` | ❌ | `400` — function calling is Block 12 |
| `response_format` unknown `type` | ❌ | `400` (would change the output contract) |
| `reasoning_effort`, `logit_bias`, `logprobs`, `user`, `metadata`, `stream_options`, `tool_choice`, unknown | ⚠️ Ignored | Accepted for drop-in compatibility; no effect |

¹ **Structured output requires backend support.** When the loaded backend cannot
constrain decoding, a JSON `response_format` is rejected with
`400 structured_output_unsupported` — never silently returned as free text.

### OpenAI responses

- Non-streaming: `chat.completion` with `choices[].message`, `finish_reason`
  (`stop` | `length`), and `usage`.
- Streaming: `chat.completion.chunk` — an opening `delta.role="assistant"`,
  content deltas, a final chunk with `finish_reason`, then `data: [DONE]`.
- Every response carries `x-request-id` (`chatcmpl-…`).

### OpenAI errors — envelope `{"error": {"message","type","param","code"}}`

| Condition | Status | `type` / `code` |
|---|---|---|
| Bad value / rejected feature (`tools`, `n>1`, unknown `response_format`) | `400` | `invalid_request_error` |
| Structured output unsupported by backend | `400` | `structured_output_unsupported` |
| Unknown model | `404` | `model_not_found` |
| No model loaded | `503` | `model_not_loaded` |
| Engine saturated (admission) | `429` | `engine_saturated` (+ `retry-after`) |
| Generation failed | `500` | `server_error` |

---

## Anthropic `POST /v1/messages`

| Field | Status | Notes |
|---|---|---|
| `model` | ✅ | Must equal the loaded model id (else `404 not_found_error`) |
| `messages` | ✅ | Roles `user`/`assistant`; `content` is a string **or** a list of `{"type":"text"}` blocks |
| `system` | ✅ | String or a list of text blocks; folded in as a system message |
| `max_tokens` | ✅ | Required (as in the Anthropic API) |
| `temperature`, `top_p`, `top_k` | ✅ | 0–1 / 0–1 / ≥0 |
| `stop_sequences` | ✅ | Array of strings |
| `stream` | ✅ | Anthropic SSE event sequence (below) |
| `metadata` and unknown fields | ⚠️ Ignored | Accepted for drop-in compatibility |
| `tools` / `tool_choice` | ❌ | `400` — tool use is Block 12 |
| non-text content blocks (image, document, tool_result) | ❌ | `400` — text only |

### Anthropic responses

Non-streaming: `{"id":"msg_…","type":"message","role":"assistant","model":…,`
`"content":[{"type":"text","text":…}],"stop_reason":…,"stop_sequence":null,`
`"usage":{"input_tokens":…,"output_tokens":…}}`.

Streaming event sequence (`event: <type>` + `data: <json>`):

1. `message_start` — the message shell + `usage.input_tokens`
2. `content_block_start` (index 0, empty text block)
3. `ping`
4. `content_block_delta` × N — `delta.type = "text_delta"`
5. `content_block_stop`
6. `message_delta` — `delta.stop_reason` + `usage.output_tokens`
7. `message_stop`

A mid-stream failure emits a terminal `error` event.

`stop_reason` mapping: natural end → `end_turn`; token cap → `max_tokens`.
(We cannot currently distinguish a matched stop sequence from a natural end, so
`stop_sequence` is always reported as `null` and the reason as `end_turn` — an
honest limitation rather than a guess.)

### Anthropic errors — envelope `{"type":"error","error":{"type","message"}}`

| Condition | Status | `type` |
|---|---|---|
| Bad value / rejected feature (`tools`, non-text content) | `400` | `invalid_request_error` |
| Unknown model | `404` | `not_found_error` |
| No model loaded / generation failed | `503` / `500` | `api_error` |
| Engine saturated (admission) | `529` | `overloaded_error` (+ `retry-after`) |

---

## Sampling controls

Both dialects map onto one internal sampler set. **Order is fixed by llama.cpp**
and, for a given model, sampling proceeds in this order:

`repetition penalties (repeat/frequency/presence) → top_k → top_p → min_p →
temperature`, or, when **Mirostat** is enabled, Mirostat replaces the
top_k/top_p/min_p truncation step (this exclusivity is documented, not silently
reinterpreted).

| Internal field | OpenAI source | Anthropic source | Backend mapping |
|---|---|---|---|
| `temperature` | `temperature` | `temperature` | `temperature` |
| `top_p` | `top_p` | `top_p` | `top_p` |
| `top_k` | — | `top_k` | `top_k` |
| `min_p` | — | — | `min_p` |
| `repeat_penalty` | — | — | `repeat_penalty` |
| `frequency_penalty` | `frequency_penalty` | — | `frequency_penalty` |
| `presence_penalty` | `presence_penalty` | — | `presence_penalty` |
| `mirostat_mode`/`tau`/`eta` | — | — | Mirostat v1/v2 |
| `seed` | `seed` | — | `seed` |
| `stop` | `stop` | `stop_sequences` | `stop` |
| `grammar` / `json_schema` / `json_object` | `response_format` | — | GBNF grammar / constrained JSON |

**Not yet exposed** (retained on the roadmap, added only once the installed
binding is confirmed to support them): typical-p, TFS, DRY, XTC, and speculative
decoding. Persisted sampler **presets** are also later work.

Structured output is honored only when the loaded backend reports
`supports_structured_output` (the `llama-cpp-python` backend does, via
`LlamaGrammar`). Schema *conformance* for supported schemas is verified against a
real model in CI (`scripts/structured_output_smoke.py`).

## Authentication, limits & quota (Block 3)

- Send an API key as `Authorization: Bearer …` or `x-api-key:` (Anthropic clients
  send `x-api-key` natively). Required when network-bound; optional on loopback.
- `401` for auth failures; `413` for oversized bodies; `429`/`529` for
  rate/concurrency/quota/saturation with a `Retry-After` header. Responses include
  `X-RateLimit-*-CU-*` headers when a quota is configured.

## Message templating

Chat messages are rendered with the **model's own chat template** (via
llama.cpp / GGUF metadata), so output quality depends on the loaded model. Prompt
token counts are an estimate over the message text; completion counts are exact.

## Model & feature support by route (Block 12.1)

With a heterogeneous pool, model and feature support are **per route**, not
global. A feature is "supported on a route" only when placement enforces it — a
request is never sent to an engine that lacks the requested model or feature. The
matrix below is an example; keep it accurate for your deployment (see
[backends.md](backends.md#heterogeneous-pools--model-eligibility-block-121)).

| Client model | Policy | Eligible engines | Streaming | Structured output |
|---|---|---|---|---|
| `chat-8b` | model map | vLLM + SGLang (homogeneous sub-pool) | ✅ | per engine |
| `coder-7b` | model map | SGLang only | ✅ | per engine |
| `<virtual>` | route / cascade | operator-chosen subset | ✅ | only if every selectable engine supports it |

- **Unknown model** → `404 model_not_found` (even when all backends are down).
- **Known model, no ready engine** → `503 model_not_loaded` (its id still appears
  in `GET /v1/models`).
- **Eligible engines all at capacity** → `429`/`529` retriable saturation.
- **Model served, but no engine supports the required feature** → `400
  structured_output_unsupported`.
