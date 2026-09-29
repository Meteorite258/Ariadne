# LongCat validation configuration

Status: live authentication, tool calls and streaming usage have been exercised in WSL2.
Complete incident scenarios remain under validation; a successful provider probe does not
establish investigation acceptance.

Use `catalog.toml` and `providers.json` in an isolated validation profile's `TAU_HOME`.
The runtime preferences set a 180-second request timeout and zero automatic retries.
The catalog selects only
`LongCat-2.5-Preview` and uses the existing provider-neutral OpenAI-compatible adapter.
Set `LONGCAT_API_KEY` outside the repository. No credentials belong in this directory.
Select `--provider longcat --model LongCat-2.5-Preview` in incident commands.

The base URL includes `/v1` because Tau appends `/chat/completions`. The official
[chat API](https://longcat.chat/platform/docs/zh/api/chat) specifies the complete URL as
`https://api.longcat.chat/openai/v1/chat/completions`, SSE streaming and `max_tokens`.
`store` and OpenAI's `reasoning_effort` are disabled in this profile. Runtime output limits
must be supplied through the incident budget; the catalog maximum is a model capability,
not an allowance for validation calls.

This profile fixes thinking to `off`, using LongCat's `thinking: {"type": "disabled"}`
parameter. With thinking enabled, the initial small output allowance was consumed by
reasoning before a structured finding was returned. See the official
[thinking configuration](https://longcat.chat/platform/docs/zh/OpenCode.html).

Before live execution, record the approved total spend limit, call/token limits, retries,
context/output allowance and actual provider usage in the Stage 7 validation record.
No model comparison or benchmark is configured.
