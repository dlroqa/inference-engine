# OpenAI API compatibility matrix

The engine exposes a **supported subset** of the OpenAI API and aims to be
**drop-in**: an official `openai` SDK client (or an agent harness) pointed at
`<base_url>/v1` works without custom protocol code for the surface below.

**Unknown and unsupported-but-benign optional fields are accepted and ignored**
(e.g. `reasoning_effort`, `frequency_penalty`, `user`, `stream_options`,
`logit_bias`, and an extra `name` on a message) so newer clients keep working.
Only fields whose behavior the caller *depends on* — and which we cannot yet
honor — are rejected with a clear `400`, because silently ignoring them would
return wrong output: `tools`/`functions`, JSON `response_format`, and `n > 1`.

_Status as of Block 5 (revised compatibility policy after real-client testing)._

## Endpoints

| Endpoint | Status | Notes |
|---|---|---|
| `POST /v1/chat/completions` | ✅ Supported | Streaming (SSE) and non-streaming |
| `GET /v1/models` | ✅ Supported | Lists the single loaded model |
| `POST /v1/completions` | ❌ Not yet | Legacy completions endpoint |
| `POST /v1/embeddings` | ❌ Not yet | Block 12 |
| Anthropic `POST /v1/messages` | ❌ Not yet | Block 8 |

## `POST /v1/chat/completions` request fields

| Field | Status | Notes |
|---|---|---|
| `model` | ✅ Supported | Must equal the loaded model id (else `404 model_not_found`) |
| `messages` | ✅ Supported | Roles `system`, `user`, `assistant`; `content` is a string |
| `stream` | ✅ Supported | SSE with OpenAI chunk framing; ends with `data: [DONE]` |
| `max_tokens` | ✅ Supported | Output cap |
| `max_completion_tokens` | ✅ Supported | Takes precedence over `max_tokens` |
| `temperature` | ✅ Supported | 0.0–2.0 |
| `top_p` | ✅ Supported | 0.0–1.0 |
| `stop` | ✅ Supported | String or array of strings |
| `seed` | ✅ Supported | Best-effort determinism (backend-dependent) |
| `n` | ⚠️ Partial | Only `n=1`; other values → `400` (would need multiple choices) |
| `response_format: {"type":"text"}` | ✅ Accepted | The OpenAI default; we produce text |
| `tools` / `functions` | ❌ Rejected | `400` — function calling is Block 12 (ignoring would drop the caller's tool calls) |
| `response_format` json / `json_schema` | ❌ Rejected | `400` — structured output is Block 8 (ignoring would return non-JSON) |
| `reasoning_effort`, `frequency_penalty`, `presence_penalty`, `logit_bias`, `logprobs`, `top_logprobs` | ⚠️ Ignored | Accepted for drop-in compatibility; **no effect yet** (samplers land in Block 8) |
| `user`, `metadata`, `stream_options`, `tool_choice`, other unknown fields | ⚠️ Ignored | Accepted and ignored for drop-in compatibility |

Multimodal `content` (arrays of parts / images) is not supported; `content` must
be a string.

## Message templating

Chat messages are rendered with the **model's own chat template** (via
llama.cpp / GGUF metadata). Output quality therefore depends on the loaded
model. Prompt-token counts for chat requests are an estimate over the message
text; completion-token counts are exact (tokens streamed).

## Response shapes

- Non-streaming: `chat.completion` with `choices[].message`, `finish_reason`
  (`stop` | `length`), and `usage` (`prompt_tokens`, `completion_tokens`,
  `total_tokens`).
- Streaming: `chat.completion.chunk` events — an opening chunk with
  `delta.role = "assistant"`, content deltas, a final chunk with
  `finish_reason`, then `data: [DONE]`.
- Every response carries an `x-request-id` header (`chatcmpl-…`).

## Errors

OpenAI error envelope: `{"error": {"message", "type", "param", "code"}}`.

| Condition | Status | `type` / `code` |
|---|---|---|
| Bad value, or a rejected feature (`tools`, json `response_format`, `n>1`) | `400` | `invalid_request_error` |
| Unknown model | `404` | `model_not_found` |
| No model loaded | `503` | `model_not_loaded` |
| Model busy (a generation in progress) | `503` | `model_busy` |
| Generation failed | `500` | `server_error` |

### Authentication & limits (Block 3)

- Send an API key as `Authorization: Bearer sk-ie-…` or `x-api-key:`. Required
  when the server is network-bound; optional on loopback.
- `401 invalid_request_error` (`code` `missing_api_key` / `invalid_api_key`) for
  auth failures; `413 payload_too_large`; `429 rate_limit_error`
  (`rate_limit_exceeded`, `concurrency_limit_exceeded`, `quota_exceeded`) with a
  `Retry-After` header.
- Responses include `X-RateLimit-{Limit,Remaining,Reset}-CU-5h` and `…-CU-Week`
  when a quota is configured.
