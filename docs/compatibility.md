# OpenAI API compatibility matrix

The engine exposes a **deliberately supported subset** of the OpenAI API. An
official `openai` SDK client pointed at `<base_url>/v1` works without custom
protocol code for the surface below. Anything not listed is **not** silently
accepted — unsupported request fields are rejected with a clear
`400 invalid_request_error`.

_Status as of Block 2._

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
| `n` | ⚠️ Partial | Only `n=1`; other values → `400` |
| `tools` / `functions` / `tool_choice` | ❌ Rejected | Block 12 (function calling / MCP) |
| `response_format` / `json_schema` | ❌ Rejected | Structured output is Block 8 |
| `logprobs` / `top_logprobs` | ❌ Rejected | Not implemented |
| `logit_bias` | ❌ Rejected | Block 8 |
| `frequency_penalty` / `presence_penalty` | ❌ Rejected | Sampler surface is Block 8 |
| `user`, `metadata`, `stream_options`, other | ❌ Rejected | Unknown fields → `400 invalid_request_error` |

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
| Unsupported / unknown field, bad value | `400` | `invalid_request_error` |
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
