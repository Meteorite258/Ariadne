"""Explicit opt-in live transport probe; never included in offline pytest collection."""

import argparse
import asyncio
import json
import os
import tomllib
from pathlib import Path

import httpx


async def stream_probe() -> None:
    from tau_agent import UserMessage
    from tau_agent.provider_events import AssistantDoneEvent, AssistantErrorEvent
    from tau_ai.env import OpenAICompatibleConfig
    from tau_ai.openai_compatible import OpenAICompatibleProvider

    catalog = Path(__file__).resolve().parents[1] / "examples/incident/longcat/catalog.toml"
    settings = tomllib.loads(catalog.read_text(encoding="utf-8"))["providers"][0]
    provider = OpenAICompatibleProvider(
        OpenAICompatibleConfig(
            api_key=os.environ["LONGCAT_API_KEY"],
            base_url=settings["base_url"],
            compat=settings["compat"],
            thinking_format=settings["compat"]["thinkingFormat"],
            max_tokens=128,
            max_retries=0,
            timeout_seconds=json.loads(catalog.with_name("providers.json").read_text())[
                "provider_preferences"
            ]["longcat"]["timeout_seconds"],
            provider_name="longcat",
        )
    )
    try:
        terminal = None
        async for event in provider.stream_response(
            model=settings["default_model"],
            system="Reply with exactly OK.",
            messages=[UserMessage(content="Verify streaming transport.")],
            tools=[],
        ):
            if isinstance(event, (AssistantDoneEvent, AssistantErrorEvent)):
                terminal = event
        if not isinstance(terminal, AssistantDoneEvent):
            print(
                json.dumps(
                    {
                        "streaming": "failed",
                        "terminal": type(terminal).__name__,
                        "error": terminal.error.error_message
                        if isinstance(terminal, AssistantErrorEvent)
                        else None,
                    }
                )
            )
            raise SystemExit(1)
        print(
            json.dumps(
                {
                    "streaming": "succeeded",
                    "model": settings["default_model"],
                    "stop_reason": terminal.message.stop_reason,
                    "usage": terminal.message.usage.model_dump(),
                    "text_matches": terminal.message.text.strip() == "OK",
                }
            )
        )
        if terminal.message.usage.total_tokens <= 0 or terminal.message.text.strip() != "OK":
            raise SystemExit(1)
    finally:
        await provider.aclose()


async def main() -> None:
    headers = {"Authorization": "Bearer " + os.environ["LONGCAT_API_KEY"]}
    async with httpx.AsyncClient(timeout=60, trust_env=True) as client:
        response = await client.post(
            "https://api.longcat.chat/openai/v1/chat/completions",
            headers=headers,
            json={
                "model": "LongCat-2.5-Preview",
                "messages": [{"role": "user", "content": "Call probe with value ok."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "probe",
                            "description": "Transport validation only",
                            "parameters": {
                                "type": "object",
                                "properties": {"value": {"type": "string"}},
                                "required": ["value"],
                            },
                        },
                    }
                ],
                "max_tokens": 128,
                "thinking": {"type": "disabled"},
                "stream": False,
            },
        )
        result = {"http_status": response.status_code, "model": "LongCat-2.5-Preview"}
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.is_success:
            result["usage"] = body.get("usage")
            result["tool_calls"] = [
                call.get("function", {}).get("name")
                for choice in body.get("choices", [])
                for call in choice.get("message", {}).get("tool_calls", [])
            ]
        else:
            error = body.get("error", {})
            if isinstance(error, dict):
                result["error_code"] = error.get("code")
                result["error_type"] = error.get("type")
        print(json.dumps(result))
        if not response.is_success:
            raise SystemExit(1)


async def wire_stream_probe() -> None:
    """Report transport metadata without printing prompts, responses, or credentials."""
    async with httpx.AsyncClient(timeout=20) as client:
        status = None
        events = 0
        usage = None
        try:
            async with client.stream(
                "POST",
                "https://api.longcat.chat/openai/v1/chat/completions",
                headers={"Authorization": "Bearer " + os.environ["LONGCAT_API_KEY"]},
                json={
                    "model": "LongCat-2.5-Preview",
                    "messages": [{"role": "user", "content": "Say OK."}],
                    "max_tokens": 128,
                    "thinking": {"type": "disabled"},
                    "stream": True,
                    "stream_options": {"include_usage": True},
                },
            ) as response:
                status = response.status_code
                async for line in response.aiter_lines():
                    if line.startswith("data:") and line[5:].strip() != "[DONE]":
                        events += 1
                        item = json.loads(line[5:])
                        usage = item.get("usage") or usage
            print(json.dumps({"http_status": status, "events": events, "usage": usage}))
        except httpx.HTTPError as exc:
            print(
                json.dumps(
                    {"http_status": status, "events": events, "error_type": type(exc).__name__}
                )
            )
            raise SystemExit(1) from None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--wire-stream", action="store_true")
    args = parser.parse_args()
    asyncio.run(
        wire_stream_probe() if args.wire_stream else stream_probe() if args.stream else main()
    )
