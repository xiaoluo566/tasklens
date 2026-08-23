"""Tests for deterministic and OpenAI-compatible TaskLens model providers."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import pytest

from browser_harness.model_provider import (
    OpenAICompatibleProvider,
    ProviderError,
    ScriptedModelProvider,
)
from browser_harness.tasklens_domain import Action, Observation, StepTrace, TaskSpec


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_scripted_provider_uses_history_without_mutating_actions() -> None:
    provider = ScriptedModelProvider(
        actions=(
            Action(kind="goto", url="https://example.test"),
            Action(kind="finish", value="done"),
        )
    )
    task = TaskSpec(goal="open the page")

    first = run(provider.next_action(task, Observation(), ()))
    second = run(provider.next_action(task, Observation(), (StepTrace(sequence=1),)))

    assert first.kind == "goto"
    assert second.kind == "finish"
    assert provider.actions[0].kind == "goto"


def test_scripted_provider_exhaustion_is_a_bounded_model_failure() -> None:
    provider = ScriptedModelProvider(actions=(Action(kind="finish"),))
    task = TaskSpec(goal="finish")

    run(provider.next_action(task, Observation(), ()))
    with pytest.raises(ProviderError) as caught:
        run(provider.next_action(task, Observation(), (StepTrace(sequence=1),)))

    assert caught.value.failure_kind == "model"
    assert caught.value.retryable is False


@dataclass
class FakeResponse:
    status_code: int
    body: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self.body


class FakeClient:
    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> Any:
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.response, BaseException):
            raise self.response
        return self.response


def _provider(response: Any) -> tuple[OpenAICompatibleProvider, FakeClient]:
    client = FakeClient(response)
    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:9000/v1",
        api_key="test-secret-key",
        model="demo-model",
        http_client=client,
    )
    return provider, client


def _response(content: Any) -> FakeResponse:
    return FakeResponse(
        200,
        {"choices": [{"message": {"content": content}}]},
    )


def test_openai_provider_parses_json_and_sends_bounded_redacted_context() -> None:
    provider, client = _provider(_response('{"kind":"finish","value":"ok"}'))
    task = TaskSpec(goal="inspect https://shop.test/order?token=secret")
    observation = Observation(
        url="https://shop.test/order?token=secret",
        text="password=secret page",
        metadata={"cookie": "do-not-send"},
    )

    action = run(provider.next_action(task, observation, ()))

    assert action.kind == "finish"
    request = client.calls[0]
    assert request["url"].endswith("/chat/completions")
    payload = request["json"]
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "test-secret-key" not in encoded
    assert "do-not-send" not in encoded
    assert "?token=secret" not in encoded
    assert payload["model"] == "demo-model"
    assert payload["response_format"] == {"type": "json_object"}


def test_openai_provider_parses_fenced_json() -> None:
    provider, _ = _provider(_response("```json\n{\"kind\":\"goto\",\"url\":\"https://example.test\"}\n```"))

    action = run(provider.next_action(TaskSpec(goal="open"), Observation(), ()))

    assert action.kind == "goto"
    assert action.url == "https://example.test"


def test_openai_provider_parses_tool_call_arguments() -> None:
    response = FakeResponse(
        200,
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"function": {"arguments": '{"kind":"finish"}'}}
                        ],
                    }
                }
            ]
        },
    )
    provider, _ = _provider(response)

    assert run(provider.next_action(TaskSpec(goal="finish"), Observation(), ())).kind == "finish"


@pytest.mark.parametrize(
    "response,kind",
    [
        (_response("not json"), "model"),
        (FakeResponse(401, {}), "environment"),
        (FakeResponse(500, {}), "connection"),
        (TimeoutError("request timed out"), "timeout"),
    ],
)
def test_openai_provider_maps_protocol_failures(response: Any, kind: str) -> None:
    provider, _ = _provider(response)

    with pytest.raises(ProviderError) as caught:
        run(provider.next_action(TaskSpec(goal="finish"), Observation(), ()))

    assert caught.value.failure_kind == kind
    assert "test-secret-key" not in str(caught.value)


def test_openai_provider_requires_key_and_restricts_non_loopback_http() -> None:
    with pytest.raises(ProviderError) as missing:
        OpenAICompatibleProvider.from_env({"TASKLENS_LLM_BASE_URL": "https://example.test/v1"})
    assert missing.value.failure_kind == "environment"

    with pytest.raises(ProviderError) as insecure:
        OpenAICompatibleProvider(
            base_url="http://example.test/v1",
            api_key="secret",
            model="demo",
        )
    assert insecure.value.failure_kind == "environment"


def test_openai_provider_accepts_keyword_only_provider_signature() -> None:
    provider, _ = _provider(_response('{"kind":"finish"}'))
    task = TaskSpec(goal="finish")
    assert run(provider.next_action(task=task, observation=Observation(), history=())).kind == "finish"
