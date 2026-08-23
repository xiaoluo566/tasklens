"""Composition tests for the default TaskLens runtime wiring."""

from __future__ import annotations

import asyncio

from browser_harness.tasklens_domain import Observation, TaskSpec
from browser_harness.tasklens_runtime import create_runtime


class FakeBrowser:
    async def observe(self) -> Observation:
        return Observation(text="demo page")

    async def execute(self, _action):
        raise AssertionError("demo provider should finish without browser actions")


def test_demo_runtime_is_zero_network_and_completes_terminal_decision() -> None:
    runtime = create_runtime(
        environ={"TASKLENS_MODEL_PROVIDER": "demo"},
        browser=FakeBrowser(),
    )

    result = asyncio.run(runtime.run(TaskSpec(goal="show the demo")))

    assert result.status == "success"
    assert result.failure_kind == "none"
    assert result.termination_reason == "success"
