"""Contract tests for the TaskLens domain models and single-agent runtime.

These tests intentionally use deterministic async fakes.  The runtime must be
testable without Chrome, CDP, a network connection, or an LLM API.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from browser_harness.agent_runtime import AgentRuntime
from browser_harness.model_provider import ProviderError
from browser_harness.tasklens_domain import (
    Action,
    ActionOutcome,
    AgentResult,
    Assertion,
    Observation,
    RetryPolicy,
    TaskSpec,
    evaluate_assertion,
)


def run(coro):
    """Run an async contract test without requiring pytest-asyncio."""

    return asyncio.run(coro)


def test_task_spec_accepts_natural_language_and_safe_defaults() -> None:
    task = TaskSpec(goal="check the order", max_steps=3)

    assert task.goal == "check the order"
    assert task.max_steps == 3
    assert task.timeout_seconds > 0
    assert task.retry_policy.idempotent_actions_only is True
    assert task.assertions == []


def test_text_assertion_uses_page_text_and_reports_evidence() -> None:
    observation = Observation(url="https://shop.test/order", text="Order status: Submitted")
    assertion = Assertion(type="text", expected="Submitted", operator="contains")

    result = evaluate_assertion(assertion, observation)

    assert result.passed is True
    assert result.actual == "Order status: Submitted"
    assert "Submitted" in result.evidence


def test_element_state_assertion_checks_visibility_and_value() -> None:
    observation = Observation(
        elements={
            "[data-testid=order-status]": {
                "visible": True,
                "text": "Submitted",
                "enabled": False,
            }
        }
    )
    assertion = Assertion(
        type="element_state",
        selector="[data-testid=order-status]",
        field="visible",
        expected=True,
        operator="equals",
    )

    result = evaluate_assertion(assertion, observation)

    assert result.passed is True
    assert result.actual is True


def test_structured_assertion_resolves_dotted_path() -> None:
    observation = Observation(structured={"order": {"status": "SUBMITTED", "id": "T-1"}})
    assertion = Assertion(
        type="structured",
        field="order.status",
        expected="SUBMITTED",
        operator="equals",
    )

    result = evaluate_assertion(assertion, observation)

    assert result.passed is True
    assert result.actual == "SUBMITTED"


def test_failed_assertion_is_not_hidden_by_successful_helper() -> None:
    observation = Observation(text="Order status: Pending")
    assertion = Assertion(type="text", expected="Submitted", operator="contains")

    result = evaluate_assertion(assertion, observation)

    assert result.passed is False
    assert result.failure_kind == "assertion"


@dataclass
class FakeBrowser:
    observations: list[Observation]
    outcomes: list[Any]
    observed: int = 0
    executed: list[Action] | None = None

    def __post_init__(self) -> None:
        self.executed = []

    async def observe(self) -> Observation:
        self.observed += 1
        if len(self.observations) > 1:
            return self.observations.pop(0)
        return self.observations[0]

    async def execute(self, action: Action) -> Any:
        assert self.executed is not None
        self.executed.append(action)
        if not self.outcomes:
            return ActionOutcome(success=True)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@dataclass
class FakeModel:
    actions: list[Any]
    calls: int = 0

    async def next_action(self, task: TaskSpec, observation: Observation, history: list[Any]) -> Any:
        self.calls += 1
        if self.actions:
            return self.actions.pop(0)
        return Action(kind="finish", value="done")


def test_runtime_completes_after_action_and_business_assertion() -> None:
    browser = FakeBrowser(
        observations=[
            Observation(url="https://shop.test/checkout", text="Checkout"),
            Observation(url="https://shop.test/order", text="Order status: Submitted"),
        ],
        outcomes=[ActionOutcome(success=True)],
    )
    model = FakeModel(actions=[Action(kind="click", target="submit")])
    task = TaskSpec(
        goal="submit the order",
        max_steps=3,
        assertions=[Assertion(type="text", expected="Submitted", operator="contains")],
    )

    result = run(AgentRuntime(browser, model).run(task))

    assert isinstance(result, AgentResult)
    assert result.status == "success"
    assert result.failure_kind == "none"
    assert result.steps_used == 1
    assert result.first_deviation_step is None
    assert result.assertions[0].passed is True
    assert result.state_history[-1] == "succeeded"


def test_runtime_retries_idempotent_action_but_counts_each_attempt() -> None:
    browser = FakeBrowser(
        observations=[Observation(text="Ready")],
        outcomes=[
            ActionOutcome(success=False, failure_kind="connection", error="temporary"),
            ActionOutcome(success=True, observation=Observation(text="Ready")),
        ],
    )
    model = FakeModel(actions=[Action(kind="wait", condition="ready", idempotent=True)])
    task = TaskSpec(
        goal="wait until ready",
        max_steps=3,
        assertions=[Assertion(type="text", expected="Ready", operator="contains")],
        retry_policy=RetryPolicy(max_retries=1, idempotent_actions_only=True),
    )

    result = run(AgentRuntime(browser, model).run(task))

    assert result.status == "success"
    assert result.steps_used == 2
    assert len(result.steps) == 2
    assert result.steps[0].attempt == 1
    assert result.steps[1].attempt == 2
    assert "retrying" in result.state_history


def test_runtime_does_not_retry_non_idempotent_action() -> None:
    browser = FakeBrowser(
        observations=[Observation(text="Checkout")],
        outcomes=[ActionOutcome(success=False, failure_kind="action", error="submit failed")],
    )
    model = FakeModel(actions=[Action(kind="submit", target="order", idempotent=False)])
    task = TaskSpec(
        goal="submit order",
        max_steps=3,
        retry_policy=RetryPolicy(max_retries=3, idempotent_actions_only=True),
    )

    result = run(AgentRuntime(browser, model).run(task))

    assert result.status == "failed"
    assert result.failure_kind == "action"
    assert result.steps_used == 1
    assert len(browser.executed or []) == 1


def test_runtime_reports_model_failure_for_invalid_action() -> None:
    browser = FakeBrowser(observations=[Observation(text="Home")], outcomes=[])
    model = FakeModel(actions=[{"not_an_action": True}])
    task = TaskSpec(goal="do something", max_steps=2)

    result = run(AgentRuntime(browser, model).run(task))

    assert result.status == "failed"
    assert result.failure_kind == "model"
    assert result.first_deviation_step == 1
    assert result.steps_used == 0


def test_runtime_stops_at_step_budget_and_does_not_loop_forever() -> None:
    browser = FakeBrowser(observations=[Observation(text="Still here")], outcomes=[])
    model = FakeModel(actions=[Action(kind="scroll", idempotent=True)] * 10)
    task = TaskSpec(goal="leave the page", max_steps=2)

    result = run(AgentRuntime(browser, model).run(task))

    assert result.status == "failed"
    assert result.steps_used == 2
    assert result.termination_reason == "max_steps"
    assert result.failure_kind == "model"


def test_runtime_converts_slow_provider_to_timeout_result() -> None:
    class SlowModel:
        async def next_action(self, task, observation, history):
            await asyncio.sleep(0.05)
            return Action(kind="finish")

    browser = FakeBrowser(observations=[Observation(text="Home")], outcomes=[])
    task = TaskSpec(goal="finish quickly", timeout_seconds=0.01)

    result = run(AgentRuntime(browser, SlowModel()).run(task))

    assert result.status == "timeout"
    assert result.failure_kind == "timeout"
    assert result.termination_reason == "timeout"
    assert result.first_deviation_step == 1
    assert result.steps
    assert result.steps[0].failure_kind == "timeout"


def test_runtime_maps_browser_connection_exception_and_first_deviation() -> None:
    browser = FakeBrowser(
        observations=[Observation(text="Home")],
        outcomes=[ConnectionError("CDP disconnected")],
    )
    model = FakeModel(actions=[Action(kind="click", target="menu")])
    task = TaskSpec(goal="open menu", max_steps=3)

    result = run(AgentRuntime(browser, model).run(task))

    assert result.status == "failed"
    assert result.failure_kind == "connection"
    assert result.first_deviation_step == 1
    assert result.steps[0].failure_kind == "connection"


def test_runtime_preserves_provider_failure_kind() -> None:
    class BrokenModel:
        async def next_action(self, task, observation, history):
            raise ProviderError(
                "upstream timed out",
                failure_kind="connection",
                retryable=True,
            )

    browser = FakeBrowser(observations=[Observation(text="Home")], outcomes=[])
    result = run(AgentRuntime(browser, BrokenModel()).run(TaskSpec(goal="check")))

    assert result.status == "failed"
    assert result.failure_kind == "connection"
    assert result.first_deviation_step == 1
    assert result.steps[0].failure_kind == "connection"


def test_runtime_accepts_sync_provider_methods() -> None:
    class SyncBrowser:
        def __init__(self):
            self.done = False

        def observe(self):
            return Observation(text="Done" if self.done else "Ready")

        def execute(self, action):
            self.done = True
            return ActionOutcome(success=True)

    class SyncModel:
        def next_action(self, task, observation, history):
            return Action(kind="click", target="go")

    task = TaskSpec(
        goal="go",
        assertions=[Assertion(type="text", expected="Done", operator="contains")],
    )
    result = run(AgentRuntime(SyncBrowser(), SyncModel()).run(task))

    assert result.status == "success"


def test_runtime_accepts_keyword_only_provider_methods() -> None:
    class KeywordOnlyModel:
        def next_action(self, *, task, observation, history):
            return Action(kind="finish")

    browser = FakeBrowser(observations=[Observation(text="Ready")], outcomes=[])
    result = run(AgentRuntime(browser, KeywordOnlyModel()).run(TaskSpec(goal="finish")))

    assert result.status == "success"


def test_runtime_accepts_provider_object_name_and_params_aliases() -> None:
    @dataclass
    class LegacyAction:
        name: str
        params: dict[str, Any]

    browser = FakeBrowser(
        observations=[Observation(text="Before"), Observation(text="After")],
        outcomes=[ActionOutcome(success=True, observation=Observation(text="After"))],
    )
    model = FakeModel(actions=[LegacyAction("click", {"selector": "#go"})])
    task = TaskSpec(
        goal="click go",
        assertions=[Assertion(type="text", expected="After", operator="contains")],
    )

    result = run(AgentRuntime(browser, model).run(task))

    assert result.status == "success"
    assert result.steps[0].action is not None
    assert result.steps[0].action.kind == "click"
    assert result.steps[0].action.target == "#go"


def test_runtime_marks_first_observable_assertion_deviation() -> None:
    browser = FakeBrowser(
        observations=[Observation(text="Before")],
        outcomes=[ActionOutcome(success=True, observation=Observation(text="Pending"))],
    )
    model = FakeModel(actions=[Action(kind="click", target="submit"), Action(kind="finish")])
    task = TaskSpec(
        goal="submit",
        max_steps=3,
        assertions=[Assertion(type="text", expected="Submitted", operator="contains")],
    )

    result = run(AgentRuntime(browser, model).run(task))

    assert result.status == "failed"
    assert result.failure_kind == "assertion"
    assert result.first_deviation_step == 1
    assert result.steps[0].status == "failed"
