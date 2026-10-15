from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from eios_llm import (
    AnthropicProvider,
    LLMError,
    LLMGateway,
    LLMMessage,
    LLMRequest,
    LocalOpenAIProvider,
    OpenAICompatibleProvider,
    ScriptedProvider,
)

REQ = LLMRequest(
    messages=[LLMMessage(role="system", content="be brief"), LLMMessage(role="user", content="hi")],
    max_tokens=50,
)


def transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


async def test_openai_compatible_wire_format_and_usage() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"] = str(request.url), request.headers.get("authorization")
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "model": "m-1", "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2}})  # fmt: skip

    p = OpenAICompatibleProvider(
        base_url="https://api.example.com/v1",
        api_key="k-123",
        default_model="m-1",
        transport=transport(handler),
    )
    out = await p.complete(REQ.model_copy(update={"json_mode": True}))
    assert out.text == "hello" and out.usage.total == 9 and out.provider == "openai-compatible"
    assert seen["url"] == "https://api.example.com/v1/chat/completions"
    assert seen["auth"] == "Bearer k-123"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "be brief"}
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert p.locality == "remote"


async def test_anthropic_wire_format_splits_system_and_reads_text_blocks() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"], seen["body"] = request.headers, json.loads(request.content)
        return httpx.Response(200, json={
            "model": "claude-x", "stop_reason": "end_turn",
            "content": [
                {"type": "text", "text": "a"},
                {"type": "tool_use"},
                {"type": "text", "text": "b"},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 3}})  # fmt: skip

    p = AnthropicProvider(api_key="sk-x", default_model="claude-x", transport=transport(handler))
    out = await p.complete(REQ)
    assert out.text == "ab" and out.usage.input_tokens == 5 and out.finish_reason == "end_turn"
    assert seen["headers"]["x-api-key"] == "sk-x" and "anthropic-version" in seen["headers"]
    assert seen["body"]["system"] == "be brief"
    assert [m["role"] for m in seen["body"]["messages"]] == ["user"]


async def test_local_provider_refuses_public_hosts() -> None:
    LocalOpenAIProvider(base_url="http://localhost:11434/v1", default_model="m")
    LocalOpenAIProvider(base_url="http://192.168.1.20:8080/v1", default_model="m")
    with pytest.raises(ValueError, match="not a local host"):
        LocalOpenAIProvider(base_url="https://api.openai.com/v1", default_model="m")
    with pytest.raises(ValueError, match="invalid base URL"):
        OpenAICompatibleProvider(base_url="file:///etc/passwd", default_model="m")


@pytest.mark.parametrize(
    ("status", "retryable"), [(429, True), (503, True), (400, False), (401, False)]
)
async def test_http_errors_are_classified(status: int, retryable: bool) -> None:
    p = OpenAICompatibleProvider(
        base_url="https://x.example/v1",
        default_model="m",
        transport=transport(lambda r: httpx.Response(status, text="nope")),
    )
    with pytest.raises(LLMError) as exc:
        await p.complete(REQ)
    assert exc.value.retryable is retryable and exc.value.status == status


async def test_malformed_provider_payloads_do_not_crash() -> None:
    bodies: list[Any] = [{"choices": []}, {"nope": 1}, []]
    for body in bodies:
        p = OpenAICompatibleProvider(
            base_url="https://x.example/v1",
            default_model="m",
            transport=transport(lambda r, b=body: httpx.Response(200, json=b)),
        )
        with pytest.raises(LLMError):
            await p.complete(REQ)


async def test_gateway_retries_transient_errors_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    p = OpenAICompatibleProvider(
        base_url="https://x.example/v1", default_model="m", transport=transport(handler)
    )
    gw = LLMGateway({p.name: p}, backoff_seconds=0)
    assert (await gw.complete(REQ)).text == "ok" and calls["n"] == 3


async def test_gateway_gives_up_after_bounded_attempts_and_never_retries_client_errors() -> None:
    calls = {"n": 0}

    def always_busy(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    p = OpenAICompatibleProvider(
        base_url="https://x.example/v1", default_model="m", transport=transport(always_busy)
    )
    gw = LLMGateway({p.name: p}, backoff_seconds=0)
    with pytest.raises(LLMError):
        await gw.complete(REQ)
    assert calls["n"] == 3
    bad = {"n": 0}

    def unauthorized(request: httpx.Request) -> httpx.Response:
        bad["n"] += 1
        return httpx.Response(401)

    q = OpenAICompatibleProvider(
        name="q",
        base_url="https://x.example/v1",
        default_model="m",
        transport=transport(unauthorized),
    )
    with pytest.raises(LLMError):
        await LLMGateway({"q": q}, backoff_seconds=0).complete(REQ)
    assert bad["n"] == 1


async def test_guard_blocks_calls_and_unknown_providers_are_errors() -> None:
    scripted = ScriptedProvider(["fine"])

    async def deny(name: str, locality: str, classification: str) -> tuple[bool, str]:
        return False, f"{classification} data may not go to {name}"

    gw = LLMGateway({"scripted": scripted}, guard=deny)
    with pytest.raises(LLMError, match="policy denied"):
        await gw.complete(REQ)
    assert scripted.requests == []  # the provider was never contacted
    with pytest.raises(LLMError, match="no LLM provider"):
        await LLMGateway().complete(REQ)
    with pytest.raises(LLMError, match="no LLM provider"):
        await LLMGateway({"scripted": scripted}).complete(REQ, provider="other")
    assert gw.describe() == [
        {"name": "scripted", "locality": "local", "default_model": "scripted-1"}
    ]
