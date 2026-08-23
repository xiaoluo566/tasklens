"""Bounded single-agent execution runtime used by TaskLens.

The runtime intentionally depends on protocols rather than the concrete CDP
implementation.  This keeps the state machine deterministic in unit tests and
lets the Browser Adapter evolve independently from the task/verification
contracts.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import (
    Any,
    Protocol,
    runtime_checkable,
)

from pydantic import ValidationError

from .tasklens_domain import (
    Action,
    ActionOutcome,
    AgentResult,
    AssertionResult,
    FailureKind,
    Observation,
    RetryPolicy,
    RunStatus,
    RuntimeState,
    StepTrace,
    TaskSpec,
    evaluate_assertions,
)


@runtime_checkable
class BrowserProtocol(Protocol):
    """Minimal browser surface required by the runtime.

    Implementations may expose synchronous or asynchronous methods.  The
    runtime executes calls serially; adapters that wrap blocking CDP helpers
    can decide whether to move that work to a worker thread.
    """

    def observe(self) -> Observation | Mapping[str, Any] | Awaitable[Observation | Mapping[str, Any]]: ...

    def execute(self, action: Action) -> ActionOutcome | Mapping[str, Any] | Awaitable[ActionOutcome | Mapping[str, Any]]: ...


@runtime_checkable
class ModelProtocol(Protocol):
    """Model provider surface.  ``decide`` is accepted as a compatibility alias."""

    def next_action(
        self,
        task: TaskSpec,
        observation: Observation,
        history: Sequence[StepTrace],
    ) -> Action | Mapping[str, Any] | str | Awaitable[Action | Mapping[str, Any] | str]: ...


@dataclass
class _RunContext:
    run_id: str
    started_at: float
    started_datetime: Any
    steps: list[StepTrace] = field(default_factory=list)
    assertions: list[AssertionResult] = field(default_factory=list)
    state_history: list[str] = field(default_factory=list)
    current_observation: Observation | None = None
    steps_used: int = 0
    first_deviation_step: int | None = None
    termination_reason: str | None = None
    terminal_failure_kind: str = FailureKind.NONE.value
    pending_sequence: int | None = None
    pending_attempt: int = 1
    pending_action: Action | None = None
    pending_observation: Observation | None = None


class AgentRuntime:
    """Execute one TaskSpec with one stateful model provider.

    The public ``run`` method is async even when injected fakes are sync.  A
    small ``run_sync`` convenience method is provided for CLI callers.
    """

    def __init__(
        self,
        browser: Any,
        model: Any,
        *,
        run_id_factory: Callable[[], str] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.browser = browser
        self.model = model
        self.run_id_factory = run_id_factory or (lambda: str(uuid.uuid4()))
        self.clock = clock or time.monotonic

    async def run(self, task: TaskSpec | Mapping[str, Any] | str) -> AgentResult:
        """Run a task and always convert terminal errors into AgentResult."""

        task_spec = _coerce_task(task)
        from .tasklens_domain import utc_now

        context = _RunContext(
            run_id=str(self.run_id_factory()),
            started_at=self.clock(),
            started_datetime=utc_now(),
        )
        self._set_state(context, RuntimeState.RECEIVED)
        try:
            await asyncio.wait_for(self._run_loop(task_spec, context), timeout=task_spec.timeout_seconds)
            # _run_loop returns only after setting a terminal reason.
            if context.termination_reason is None:
                context.termination_reason = "completed"
            status = RunStatus.SUCCESS.value if context.termination_reason == "success" else RunStatus.FAILED.value
            failure_kind = (
                FailureKind.NONE.value
                if status == RunStatus.SUCCESS.value
                else context.terminal_failure_kind or FailureKind.UNKNOWN.value
            )
            return self._result(task_spec, context, status, failure_kind)
        except TimeoutError:
            context.termination_reason = "timeout"
            context.terminal_failure_kind = FailureKind.TIMEOUT.value
            self._set_state(context, RuntimeState.TIMEOUT)
            self._record_pending_timeout(context)
            return self._result(task_spec, context, RunStatus.TIMEOUT.value, FailureKind.TIMEOUT.value)
        except asyncio.CancelledError:
            # A caller cancelling the task still receives a useful record.  We
            # deliberately do not re-raise because the dashboard contract needs
            # a terminal ``cancelled`` result for interrupted browser runs.
            context.termination_reason = "cancelled"
            context.terminal_failure_kind = FailureKind.CANCELLED.value
            self._set_state(context, RuntimeState.CANCELLED)
            self._record_pending_timeout(context, failure_kind=FailureKind.CANCELLED.value)
            return self._result(task_spec, context, RunStatus.CANCELLED.value, FailureKind.CANCELLED.value)
        except Exception as exc:  # noqa: BLE001 - boundary must produce result
            kind = classify_failure(exc)
            context.termination_reason = "exception"
            context.terminal_failure_kind = kind
            self._set_state(context, RuntimeState.FAILED)
            if context.first_deviation_step is None:
                context.first_deviation_step = max(1, len(context.steps) + 1)
            self._record_pending_timeout(context, failure_kind=kind, error=str(exc))
            return self._result(task_spec, context, RunStatus.FAILED.value, kind, answer_summary=_failure_summary(kind, exc))

    async def execute(self, task: TaskSpec | Mapping[str, Any] | str) -> AgentResult:
        """Alias used by service/CLI integrations."""

        return await self.run(task)

    async def run_task(self, task: TaskSpec | Mapping[str, Any] | str) -> AgentResult:
        return await self.run(task)

    async def __call__(self, task: TaskSpec | Mapping[str, Any] | str) -> AgentResult:
        return await self.run(task)

    def run_sync(self, task: TaskSpec | Mapping[str, Any] | str) -> AgentResult:
        """Synchronous bridge for a CLI process without an active event loop."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.run(task))
        raise RuntimeError("run_sync() cannot be called from a running event loop; await run() instead")

    async def _run_loop(self, task: TaskSpec, context: _RunContext) -> None:
        self._set_state(context, RuntimeState.OBSERVING)
        try:
            context.current_observation = await self._observe()
        except Exception as exc:  # noqa: BLE001 - classify adapter boundary
            kind = classify_failure(exc)
            context.termination_reason = "observation_failed"
            context.terminal_failure_kind = kind
            context.first_deviation_step = 1
            self._set_state(context, RuntimeState.FAILED)
            context.steps.append(
                StepTrace(
                    sequence=1,
                    attempt=1,
                    state=RuntimeState.OBSERVING.value,
                    status="failed",
                    failure_kind=kind,
                    error=_short_error(exc),
                )
            )
            return

        retry_counts: dict[str, int] = {}
        pending_action: Action | None = None
        pending_observation: Observation | None = context.current_observation

        while context.steps_used < task.max_steps:
            # A retry reuses the same model action; ordinary iterations ask the
            # single provider for exactly one new structured action.
            if pending_action is None:
                self._set_state(context, RuntimeState.OBSERVING)
                if pending_observation is None:
                    try:
                        pending_observation = await self._observe()
                        context.current_observation = pending_observation
                    except Exception as exc:  # noqa: BLE001
                        kind = classify_failure(exc)
                        context.termination_reason = "observation_failed"
                        self._fail_context(context, kind, str(exc), sequence=len(context.steps) + 1)
                        return
                self._set_state(context, RuntimeState.ACTING)
                sequence = len(context.steps) + 1
                # Mark the planning slot before awaiting the provider.  If a
                # model request times out, the terminal result still carries
                # a concrete first-deviation step instead of an empty trace.
                context.pending_sequence = sequence
                context.pending_attempt = 1
                context.pending_observation = pending_observation
                context.pending_action = None
                try:
                    raw_action = await self._choose_action(task, pending_observation, context.steps)
                    action = _coerce_action(raw_action)
                except Exception as exc:  # noqa: BLE001 - model output boundary
                    context.termination_reason = "invalid_action"
                    self._fail_context(context, classify_failure(exc), str(exc), sequence=sequence)
                    return
                pending_action = action
                context.pending_action = action
                retry_counts.setdefault(_action_key(action), 0)
            else:
                action = pending_action
                sequence = len(context.steps) + 1
                self._set_state(context, RuntimeState.RETRYING)

            # ``finish`` is a model decision, not a browser side effect.  It
            # still gets a trace so the dashboard can explain the terminal
            # decision, but does not consume the action execution budget.
            if action.is_terminal:
                assertion_results = evaluate_assertions(task.assertions, pending_observation or Observation())
                context.assertions = assertion_results
                passed = not assertion_results or all(item.passed for item in assertion_results)
                trace = StepTrace(
                    sequence=sequence,
                    attempt=retry_counts.get(_action_key(action), 0) + 1,
                    state=RuntimeState.VERIFYING.value,
                    observation=pending_observation,
                    action=action,
                    assertions=assertion_results,
                    status="success" if passed else "failed",
                    failure_kind=FailureKind.NONE.value if passed else FailureKind.ASSERTION.value,
                )
                context.steps.append(trace)
                if passed:
                    context.termination_reason = "success"
                    self._set_state(context, RuntimeState.SUCCEEDED)
                else:
                    context.termination_reason = "assertion_failed"
                    self._fail_context(context, FailureKind.ASSERTION.value, "terminal assertions did not pass", sequence=sequence, append=False)
                return

            context.steps_used += 1
            context.pending_sequence = sequence
            context.pending_attempt = retry_counts.get(_action_key(action), 0) + 1
            context.pending_action = action
            context.pending_observation = pending_observation
            try:
                raw_outcome = await self._execute(action)
                outcome = ActionOutcome.model_validate(raw_outcome)
            except Exception as exc:  # noqa: BLE001 - adapter boundary
                kind = classify_failure(exc)
                outcome = ActionOutcome(success=False, failure_kind=kind, error=_short_error(exc))

            if not outcome.success:
                trace = StepTrace(
                    sequence=sequence,
                    attempt=context.pending_attempt,
                    state=RuntimeState.ACTING.value,
                    observation=pending_observation,
                    action=action,
                    outcome=outcome,
                    status="failed",
                    failure_kind=outcome.failure_kind,
                    error=outcome.error,
                )
                context.steps.append(trace)
                can_retry = self._can_retry(task.retry_policy, action, outcome, retry_counts[_action_key(action)])
                if can_retry and context.steps_used < task.max_steps:
                    retry_counts[_action_key(action)] += 1
                    self._set_state(context, RuntimeState.RETRYING)
                    # Keep pending_observation; a retry should not require an
                    # extra page read after a failed helper call.
                    continue
                context.termination_reason = "action_failed"
                self._fail_context(context, outcome.failure_kind, outcome.error or "action failed", sequence=sequence, append=False)
                return

            # Successful helper invocation is not yet business success.  Use a
            # supplied post-action observation when available; otherwise fetch
            # one serially from the adapter before evaluating assertions.
            post_observation = outcome.observation
            if post_observation is None:
                try:
                    post_observation = await self._observe()
                except Exception as exc:  # noqa: BLE001
                    kind = classify_failure(exc)
                    trace = StepTrace(
                        sequence=sequence,
                        attempt=context.pending_attempt,
                        state=RuntimeState.VERIFYING.value,
                        observation=pending_observation,
                        action=action,
                        outcome=outcome,
                        status="failed",
                        failure_kind=kind,
                        error=_short_error(exc),
                    )
                    context.steps.append(trace)
                    context.termination_reason = "verification_observation_failed"
                    self._fail_context(context, kind, str(exc), sequence=sequence, append=False)
                    return
            post_observation = _coerce_observation(post_observation)
            context.current_observation = post_observation
            assertion_results = evaluate_assertions(task.assertions, post_observation)
            context.assertions = assertion_results
            passed = not assertion_results or all(item.passed for item in assertion_results)
            trace = StepTrace(
                sequence=sequence,
                attempt=context.pending_attempt,
                state=RuntimeState.VERIFYING.value,
                observation=pending_observation,
                action=action,
                outcome=outcome,
                assertions=assertion_results,
                status="success" if passed else "failed",
                failure_kind=FailureKind.NONE.value if passed else FailureKind.ASSERTION.value,
                duration_seconds=outcome.duration_seconds,
            )
            context.steps.append(trace)
            if not passed and context.first_deviation_step is None:
                # This is the earliest *observable* business deviation.  It
                # may later recover, but retaining the location preserves the
                # evidence chain for the dashboard.
                context.first_deviation_step = sequence
            context.pending_sequence = None
            context.pending_action = None
            context.pending_observation = None
            pending_action = None
            pending_observation = post_observation

            # No assertion means a regular action is not enough to prove
            # success; the model must explicitly return finish.
            if passed and assertion_results:
                context.termination_reason = "success"
                self._set_state(context, RuntimeState.SUCCEEDED)
                return

        # The budget ended without a terminal decision.  This is a model
        # control failure, not an assertion failure guessed from stale state.
        context.termination_reason = "max_steps"
        context.terminal_failure_kind = FailureKind.MODEL.value
        self._set_state(context, RuntimeState.FAILED)

    async def _observe(self) -> Observation:
        method = _find_method(self.browser, ("observe", "get_observation", "snapshot", "page_state"))
        if method is None:
            raise RuntimeError("browser adapter does not provide observe()")
        return _coerce_observation(await _maybe_await(method()))

    async def _execute(self, action: Action) -> ActionOutcome | Mapping[str, Any] | Any:
        method = _find_method(self.browser, ("execute", "execute_action", "act", "perform"))
        if method is None:
            raise RuntimeError("browser adapter does not provide execute()")
        return await _maybe_await(method(action))

    async def _choose_action(self, task: TaskSpec, observation: Observation, history: Sequence[StepTrace]) -> Any:
        method = _find_method(self.model, ("next_action", "decide", "choose_action", "act"))
        if method is None:
            raise RuntimeError("model provider does not provide next_action()")
        return await _maybe_await(_call_model(method, task, observation, history))

    @staticmethod
    def _can_retry(policy: RetryPolicy, action: Action, outcome: ActionOutcome, retries: int) -> bool:
        if retries >= policy.max_retries:
            return False
        if policy.idempotent_actions_only and not bool(action.idempotent):
            return False
        if outcome.retryable is False:
            return False
        return outcome.failure_kind in set(policy.retryable_failure_kinds)

    @staticmethod
    def _set_state(context: _RunContext, state: RuntimeState | str) -> None:
        value = state.value if isinstance(state, RuntimeState) else str(state)
        if not context.state_history or context.state_history[-1] != value:
            context.state_history.append(value)

    def _fail_context(
        self,
        context: _RunContext,
        failure_kind: str,
        error: str,
        *,
        sequence: int,
        append: bool = True,
    ) -> None:
        if context.first_deviation_step is None:
            context.first_deviation_step = sequence
        context.terminal_failure_kind = failure_kind
        self._set_state(context, RuntimeState.FAILED)
        if append:
            context.steps.append(
                StepTrace(
                    sequence=sequence,
                    attempt=1,
                    state=RuntimeState.FAILED.value,
                    status="failed",
                    failure_kind=failure_kind,
                    error=error[:1_000],
                )
            )

    def _record_pending_timeout(
        self,
        context: _RunContext,
        *,
        failure_kind: str = FailureKind.TIMEOUT.value,
        error: str = "task deadline exceeded",
    ) -> None:
        if context.pending_sequence is None:
            return
        if any(step.sequence == context.pending_sequence and step.attempt == context.pending_attempt for step in context.steps):
            return
        context.steps.append(
            StepTrace(
                sequence=context.pending_sequence,
                attempt=context.pending_attempt,
                state=RuntimeState.ACTING.value,
                observation=context.pending_observation,
                action=context.pending_action,
                status="failed",
                failure_kind=failure_kind,
                error=error[:1_000],
            )
        )
        if context.first_deviation_step is None:
            context.first_deviation_step = context.pending_sequence

    def _result(
        self,
        task: TaskSpec,
        context: _RunContext,
        status: str,
        failure_kind: str,
        *,
        answer_summary: str | None = None,
    ) -> AgentResult:
        from .tasklens_domain import utc_now

        finished_at = utc_now()
        duration = max(0.0, self.clock() - context.started_at)
        if answer_summary is None:
            answer_summary = _success_summary(task) if status == RunStatus.SUCCESS.value else _failure_summary(failure_kind)
        return AgentResult(
            run_id=context.run_id,
            task_id=task.task_id,
            status=status,
            failure_kind=failure_kind,
            answer_summary=answer_summary[:1_000],
            steps=context.steps,
            assertions=context.assertions,
            first_deviation_step=context.first_deviation_step,
            state_history=context.state_history,
            termination_reason=context.termination_reason,
            steps_used=context.steps_used,
            started_at=context.started_datetime,
            finished_at=finished_at,
            duration_seconds=duration,
        )


def _coerce_task(task: TaskSpec | Mapping[str, Any] | str) -> TaskSpec:
    if isinstance(task, TaskSpec):
        return task
    if isinstance(task, str):
        return TaskSpec(goal=task)
    return TaskSpec.model_validate(task)


def _coerce_observation(value: Any) -> Observation:
    if isinstance(value, Observation):
        return value
    if value is None:
        return Observation()
    return Observation.model_validate(value)


def _coerce_action(value: Any) -> Action:
    """Normalise provider output, reusing the adapter's compatibility parser.

    The adapter owns aliases such as ``fill_input``/``navigate`` and fake
    provider objects with ``name``/``params`` attributes.  Keeping this lazy
    avoids importing the CDP helper module for callers that use only domain
    tests, while still giving the Runtime one canonical Action object.
    """

    try:
        from .browser_adapter import normalize_action

        # Legacy providers often put the selector/url in ``params``.  Promote
        # those fields before handing the value to the stricter adapter parser.
        if isinstance(value, Mapping):
            candidate: Any = dict(value)
        elif value is not None and not isinstance(value, (str, bytes, bytearray, int, float, bool)):
            candidate = {key: getattr(value, key) for key in ("kind", "name", "action", "type", "target", "selector", "value", "text", "url", "condition", "args", "params", "arguments", "idempotent", "metadata") if hasattr(value, key)}
        else:
            candidate = value
        if isinstance(candidate, Mapping):
            candidate = dict(candidate)
            raw_args = candidate.get("args", candidate.get("params", candidate.get("arguments", {})))
            if isinstance(raw_args, Mapping):
                args = dict(raw_args)
                if "target" not in candidate and "selector" in args:
                    candidate["target"] = args["selector"]
                if "value" not in candidate and "text" in args:
                    candidate["value"] = args["text"]
                if "url" not in candidate and "url" in args:
                    candidate["url"] = args["url"]
        return normalize_action(candidate)
    except ImportError:
        return Action.model_validate(value)


def _find_method(instance: Any, names: Sequence[str]) -> Callable[..., Any] | None:
    for name in names:
        method = getattr(instance, name, None)
        if callable(method):
            return method
    return None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _call_model(method: Callable[..., Any], task: TaskSpec, observation: Observation, history: Sequence[StepTrace]) -> Any:
    """Call common provider signatures without coupling to one SDK."""

    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return method(task, observation, history)
    parameters = list(signature.parameters.values())
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return method(task=task, observation=observation, history=history)
    keyword_only = [
        parameter
        for parameter in parameters
        if parameter.kind == inspect.Parameter.KEYWORD_ONLY
    ]
    if keyword_only and not any(
        parameter.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for parameter in parameters
    ):
        values = {
            "task": task,
            "observation": observation,
            "history": history,
        }
        return method(**{parameter.name: values[parameter.name] for parameter in keyword_only if parameter.name in values})
    positional = [parameter for parameter in parameters if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)]
    required = [parameter for parameter in positional if parameter.default is inspect.Parameter.empty]
    if len(required) <= 1 and len(positional) <= 1:
        return method(observation)
    if len(required) <= 2 and len(positional) <= 2:
        return method(task, observation)
    return method(task, observation, history)


def _action_key(action: Action) -> str:
    return f"{action.kind}|{action.target or ''}|{action.url or ''}|{action.condition or ''}"


def classify_failure(exc: BaseException) -> str:
    """Map adapter/provider exceptions to stable dashboard categories."""

    explicit = getattr(exc, "failure_kind", None)
    if explicit:
        candidate = str(explicit).strip().lower().replace("-", "_")
        if candidate in {kind.value for kind in FailureKind}:
            return candidate
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return FailureKind.TIMEOUT.value
    if isinstance(exc, asyncio.CancelledError):
        return FailureKind.CANCELLED.value
    text = str(exc).lower()
    if isinstance(exc, (ConnectionError, ConnectionResetError, BrokenPipeError)) or any(token in text for token in ("cdp", "websocket", "socket", "disconnect", "connection")):
        return FailureKind.CONNECTION.value
    if isinstance(exc, (FileNotFoundError, PermissionError)) or any(token in text for token in ("chrome", "browser executable", "environment")):
        return FailureKind.ENVIRONMENT.value
    if any(
        token in text
        for token in ("element", "selector", "click", "input", "action", "helper", "not found", "navigation")
    ):
        return FailureKind.ACTION.value
    if isinstance(exc, (ValidationError, ValueError, TypeError)):
        return FailureKind.MODEL.value
    return FailureKind.UNKNOWN.value


def _short_error(exc: BaseException) -> str:
    return str(exc).strip()[:1_000] or exc.__class__.__name__


def _failure_summary(kind: str, exc: BaseException | None = None) -> str:
    suffix = f": {_short_error(exc)}" if exc is not None else ""
    return f"Task failed ({kind}){suffix}"


def _success_summary(task: TaskSpec) -> str:
    return f"Task completed: {task.goal[:240]}"


__all__ = [
    "AgentRuntime",
    "BrowserProtocol",
    "ModelProtocol",
    "classify_failure",
]
