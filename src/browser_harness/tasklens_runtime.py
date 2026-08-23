"""Composition helpers for running TaskLens outside dependency-injected tests.

The core runtime remains constructor-injected.  This module is the narrow
application assembly layer used by the CLI and Dashboard: it selects an
explicit zero-network demo mode or an OpenAI-compatible provider and pairs it
with the upstream CDP adapter.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from .agent_runtime import AgentRuntime
from .browser_adapter import CDPBrowserAdapter, normalize_action
from .model_provider import DemoModelProvider, ProviderError, create_model_provider
from .tasklens_domain import ActionOutcome, Observation


class DemoBrowserAdapter:
    """Small in-memory browser used only for explicit local demo mode."""

    def __init__(
        self,
        *,
        url: str = "https://demo.tasklens.local/",
        text: str = "TaskLens demo page",
    ) -> None:
        self._url = url
        self._text = text
        self._title = "TaskLens Demo"

    async def observe(self) -> Observation:
        return Observation(url=self._url, title=self._title, text=self._text)

    async def execute(self, raw_action: Any) -> ActionOutcome:
        try:
            action = normalize_action(raw_action)
        except Exception as exc:  # noqa: BLE001 - demo boundary mirrors CDP adapter
            return ActionOutcome(success=False, failure_kind="model", error=str(exc)[:500])

        if action.kind == "goto" and action.url:
            self._url = action.url
            self._text = f"TaskLens demo page: {action.url}"
        elif action.kind == "click":
            self._text = f"{self._text} clicked {action.target or 'point'}"
        elif action.kind == "type":
            self._text = f"{self._text} {action.value or ''}".strip()
        elif action.kind not in {"scroll", "wait", "key", "screenshot", "finish"}:
            return ActionOutcome(
                success=False,
                failure_kind="model",
                error=f"unsupported demo action: {action.kind}",
            )
        return ActionOutcome(success=True, observation=await self.observe(), value={"demo": True})


def create_runtime(
    *,
    environ: Mapping[str, str] | None = None,
    browser: Any | None = None,
    model_provider: Any | None = None,
) -> AgentRuntime:
    """Build an ``AgentRuntime`` from explicit environment configuration."""

    values = dict(os.environ if environ is None else environ)
    mode = str(values.get("TASKLENS_MODEL_PROVIDER", "openai")).strip().lower()
    if values.get("TASKLENS_DEMO", "").strip().lower() in {"1", "true", "yes", "on"}:
        mode = "demo"
        values["TASKLENS_MODEL_PROVIDER"] = "demo"

    provider = model_provider
    if provider is None:
        provider = DemoModelProvider() if mode in {"demo", "scripted"} else create_model_provider(values)

    selected_browser = browser
    if selected_browser is None:
        selected_browser = DemoBrowserAdapter() if mode in {"demo", "scripted"} else CDPBrowserAdapter()
    return AgentRuntime(selected_browser, provider)


def runtime_from_environment(_task: Any | None = None) -> AgentRuntime:
    """Factory suitable for FastAPI's lazy task submission path."""

    return create_runtime()


__all__ = [
    "DemoBrowserAdapter",
    "ProviderError",
    "create_runtime",
    "runtime_from_environment",
]
