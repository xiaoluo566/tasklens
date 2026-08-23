from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from browser_harness.browser_adapter import (
    Action,
    BrowserAdapter,
    BrowserAdapterError,
    CDPBrowserAdapter,
    EvidenceRef,
    HarnessBrowserAdapter,
    InvalidActionError,
    classify_failure,
    normalize_action,
)


def run(coro):
    """Run one adapter coroutine without requiring pytest-asyncio."""

    return asyncio.run(coro)


class FakeHelpers:
    """A deterministic stand-in for browser_harness.helpers.

    It deliberately exposes no daemon/browser startup operation. Tests fail if
    the adapter reaches outside this object for browser control.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.page = {
            "url": "https://shop.example/checkout?token=secret",
            "title": "Checkout",
            "w": 1200,
            "h": 800,
        }
        self.dom = {
            "summary": "Checkout Submit order",
            "interactive_elements": [
                {
                    "tag": "button",
                    "role": "button",
                    "name": "Submit order",
                    "selector": "#submit",
                    "disabled": False,
                }
            ],
        }
        self.element_center: dict[str, float] | None = {"x": 150.0, "y": 225.0}

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))

    def ensure_real_tab(self):
        self._record("ensure_real_tab")
        return {"target_id": "tab-1", "url": self.page["url"], "title": self.page["title"]}

    def current_tab(self):
        self._record("current_tab")
        return {"target_id": "tab-1", "url": self.page["url"], "title": self.page["title"]}

    def page_info(self):
        self._record("page_info")
        return dict(self.page)

    def js(self, expression: str):
        self._record("js", expression)
        if "TASKLENS_ELEMENT_CENTER" in expression:
            return None if self.element_center is None else dict(self.element_center)
        return {
            "summary": self.dom["summary"],
            "interactive_elements": [dict(item) for item in self.dom["interactive_elements"]],
        }

    def goto_url(self, url: str):
        self._record("goto_url", url)
        self.page = {**self.page, "url": url}
        return {"frameId": "frame-1"}

    def click_at_xy(self, x: float, y: float, **kwargs: Any):
        self._record("click_at_xy", x, y, **kwargs)

    def fill_input(self, selector: str, text: str, **kwargs: Any):
        self._record("fill_input", selector, text, **kwargs)

    def scroll(self, x: float, y: float, **kwargs: Any):
        self._record("scroll", x, y, **kwargs)

    def wait(self, seconds: float):
        self._record("wait", seconds)

    def wait_for_element(self, selector: str, **kwargs: Any):
        self._record("wait_for_element", selector, **kwargs)
        return selector != "#never"

    def wait_for_load(self, **kwargs: Any):
        self._record("wait_for_load", **kwargs)
        return True

    def press_key(self, key: str, **kwargs: Any):
        self._record("press_key", key, **kwargs)

    def capture_screenshot(self, **kwargs: Any):
        self._record("capture_screenshot", **kwargs)
        return "C:/tmp/tasklens-shot.png"


def test_normalize_action_accepts_mapping_aliases_without_mutating_input():
    raw = {
        "name": "fill_input",
        "selector": "#email",
        "text": "student@example.com",
        "params": {"clear_first": False},
        "metadata": {"source": "fake-model"},
    }

    action = normalize_action(raw)

    assert action.kind == "type"
    assert action.name == "type"
    assert action.target == "#email"
    assert action.value == "student@example.com"
    assert action.args == {"clear_first": False}
    assert action.idempotent is False
    assert raw["name"] == "fill_input"
    assert raw["params"] == {"clear_first": False}


@dataclass
class ModelAction:
    name: str
    params: dict[str, Any]
    idempotent: bool = True


def test_normalize_action_accepts_fake_model_objects():
    action = normalize_action(ModelAction("navigate", {"url": "https://example.test"}))

    assert action == Action(
        kind="goto",
        url="https://example.test",
        args={},
        idempotent=True,
    )


@pytest.mark.parametrize("raw", [{}, {"target": "#submit"}, 42, None])
def test_normalize_action_rejects_missing_or_non_structured_action(raw):
    with pytest.raises(InvalidActionError):
        normalize_action(raw)


def test_adapter_construction_is_lazy_and_satisfies_protocol():
    helpers = FakeHelpers()

    adapter = HarnessBrowserAdapter(helpers=helpers)

    assert isinstance(adapter, BrowserAdapter)
    assert isinstance(adapter, CDPBrowserAdapter)
    assert helpers.calls == []


def test_attach_uses_existing_tab_without_starting_or_activating_browser():
    helpers = FakeHelpers()
    adapter = HarnessBrowserAdapter(helpers=helpers)

    assert run(adapter.attach()) is None

    assert helpers.calls == [("ensure_real_tab", (), {})]
    assert adapter.attached is True
    assert adapter.tab_id == "tab-1"


def test_observe_returns_bounded_structured_page_state():
    helpers = FakeHelpers()
    helpers.dom = {
        "summary": "A" * 200,
        "interactive_elements": [
            {"tag": "button", "name": f"button-{index}"} for index in range(5)
        ],
    }
    adapter = HarnessBrowserAdapter(
        helpers=helpers,
        max_summary_chars=80,
        max_interactive_elements=2,
    )

    observation = run(adapter.observe())

    assert observation.url == "https://shop.example/checkout"
    assert observation.title == "Checkout"
    assert observation.page_summary == "A" * 80
    assert len(observation.interactive_elements) == 2
    assert observation.viewport == {"width": 1200, "height": 800}
    assert observation.raw_page_info["w"] == 1200


def test_execute_goto_dispatches_normalized_action_and_returns_timed_outcome():
    helpers = FakeHelpers()
    ticks = iter([10.0, 10.25])
    adapter = HarnessBrowserAdapter(helpers=helpers, clock=lambda: next(ticks))

    outcome = run(adapter.execute({"kind": "navigate", "url": "https://example.test"}))

    assert outcome.success is True
    assert outcome.status == "success"
    assert outcome.failure_kind == "none"
    assert outcome.duration_seconds == pytest.approx(0.25)
    assert outcome.value == {"frameId": "frame-1"}
    assert ("goto_url", ("https://example.test",), {}) in helpers.calls


def test_execute_click_resolves_selector_to_coordinates():
    helpers = FakeHelpers()
    adapter = HarnessBrowserAdapter(helpers=helpers)

    outcome = run(adapter.execute({"kind": "click", "target": "#submit"}))

    assert outcome.success is True
    assert any(call[0] == "js" and "TASKLENS_ELEMENT_CENTER" in call[1][0] for call in helpers.calls)
    assert ("click_at_xy", (150.0, 225.0), {"button": "left", "clicks": 1}) in helpers.calls


def test_execute_click_accepts_coordinates_without_dom_lookup():
    helpers = FakeHelpers()
    adapter = HarnessBrowserAdapter(helpers=helpers)

    outcome = run(
        adapter.execute(
            {
                "kind": "click",
                "target": {"x": 10, "y": 20},
                "args": {"button": "right", "clicks": 2},
            }
        )
    )

    assert outcome.success is True
    assert ("click_at_xy", (10.0, 20.0), {"button": "right", "clicks": 2}) in helpers.calls
    assert not any(call[0] == "js" for call in helpers.calls)


def test_execute_type_uses_framework_aware_fill_helper():
    helpers = FakeHelpers()
    adapter = HarnessBrowserAdapter(helpers=helpers)

    outcome = run(
        adapter.execute(
            {
                "kind": "type",
                "target": "#password",
                "value": "do-not-copy-to-outcome",
                "args": {"clear_first": False, "timeout": 2},
            }
        )
    )

    assert outcome.success is True
    assert outcome.value is None
    assert (
        "fill_input",
        ("#password", "do-not-copy-to-outcome"),
        {"clear_first": False, "timeout": 2.0},
    ) in helpers.calls
    assert "do-not-copy-to-outcome" not in (outcome.error_summary or "")


def test_execute_scroll_resolves_direction_and_viewport_center():
    helpers = FakeHelpers()
    adapter = HarnessBrowserAdapter(helpers=helpers)

    outcome = run(adapter.execute({"kind": "scroll", "args": {"direction": "down", "amount": 640}}))

    assert outcome.success is True
    assert ("scroll", (600.0, 400.0), {"dy": 640.0, "dx": 0.0}) in helpers.calls


def test_execute_wait_for_missing_element_returns_timeout_outcome():
    helpers = FakeHelpers()
    adapter = HarnessBrowserAdapter(helpers=helpers)

    outcome = run(
        adapter.execute(
            {"kind": "wait", "target": "#never", "args": {"timeout": 0.01, "visible": True}}
        )
    )

    assert outcome.success is False
    assert outcome.status == "failed"
    assert outcome.failure_kind == "timeout"
    assert outcome.retryable is True
    assert "#never" in (outcome.error_summary or "")


def test_execute_finish_is_a_control_action_not_a_browser_helper_call():
    helpers = FakeHelpers()
    adapter = HarnessBrowserAdapter(helpers=helpers)

    outcome = run(adapter.execute({"kind": "finish", "value": "done"}))

    assert outcome.success is True
    assert outcome.value == {"reason": "done"}
    assert helpers.calls == []


def test_unknown_action_is_reported_as_model_failure_instead_of_raising():
    helpers = FakeHelpers()
    adapter = HarnessBrowserAdapter(helpers=helpers)

    outcome = run(adapter.execute({"kind": "execute_python", "value": "dangerous()"}))

    assert outcome.success is False
    assert outcome.failure_kind == "model"
    assert outcome.retryable is False
    assert helpers.calls == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("operation timed out"), "timeout"),
        (ConnectionResetError("WebSocket disconnected"), "connection"),
        (FileNotFoundError("Chrome executable not found"), "environment"),
        (RuntimeError("No session with given id"), "connection"),
        (RuntimeError("element not found: '#submit'"), "action"),
        (RuntimeError("unclassified lower-level failure"), "unknown"),
    ],
)
def test_failure_classifier_maps_stable_failure_kinds(error, expected):
    assert classify_failure(error) == expected


def test_execute_converts_helper_exception_to_outcome_and_preserves_idempotency():
    helpers = FakeHelpers()

    def disconnected(_url: str):
        raise RuntimeError("CDP WebSocket connection closed")

    helpers.goto_url = disconnected
    adapter = HarnessBrowserAdapter(helpers=helpers)

    outcome = run(adapter.execute({"kind": "goto", "url": "https://example.test"}))

    assert outcome.success is False
    assert outcome.failure_kind == "connection"
    assert outcome.retryable is True
    assert "CDP WebSocket" in (outcome.error_summary or "")


def test_malformed_action_is_converted_to_model_failure_at_execute_boundary():
    adapter = HarnessBrowserAdapter(helpers=FakeHelpers())

    outcome = run(adapter.execute({"target": "#missing-kind"}))

    assert outcome.success is False
    assert outcome.failure_kind == "model"
    assert outcome.retryable is False


def test_capture_evidence_returns_reference_without_reading_image_contents():
    helpers = FakeHelpers()
    adapter = HarnessBrowserAdapter(helpers=helpers)

    evidence = run(adapter.capture_evidence())

    assert evidence == EvidenceRef(
        kind="screenshot",
        location="C:/tmp/tasklens-shot.png",
        metadata={},
    )
    assert evidence.ref == "C:/tmp/tasklens-shot.png"
    assert helpers.calls == [("capture_screenshot", (), {"full": False, "max_dim": 1800})]


def test_close_releases_adapter_state_but_never_closes_users_tab():
    helpers = FakeHelpers()
    adapter = HarnessBrowserAdapter(helpers=helpers)
    run(adapter.attach())
    helpers.calls.clear()

    assert run(adapter.close()) is None

    assert adapter.attached is False
    assert adapter.tab_id is None
    assert helpers.calls == []


def test_execute_aliases_support_fake_runtime_implementations():
    helpers = FakeHelpers()
    adapter = HarnessBrowserAdapter(helpers=helpers)

    first = run(adapter.execute_action({"kind": "finish", "value": "first"}))
    second = run(adapter.act({"kind": "finish", "value": "second"}))

    assert first.success is True
    assert second.success is True
    assert first.value == {"reason": "first"}
    assert second.value == {"reason": "second"}


def test_async_injected_helpers_are_supported_without_a_real_browser():
    class AsyncHelpers(FakeHelpers):
        async def goto_url(self, url: str):
            self._record("goto_url", url)
            return {"async": True}

    adapter = HarnessBrowserAdapter(helpers=AsyncHelpers())

    outcome = run(adapter.execute({"kind": "goto", "url": "https://async.test"}))

    assert outcome.success is True
    assert outcome.value == {"async": True}


def test_minimal_type_fallback_encodes_selector_as_data_not_javascript() -> None:
    class MinimalHelpers(FakeHelpers):
        fill_input = None

        def type_text(self, text: str):
            self._record("type_text", text)

    helpers = MinimalHelpers()
    adapter = HarnessBrowserAdapter(helpers=helpers)
    malicious_selector = "#email'); window.__tasklens_injected = true; //"

    outcome = run(
        adapter.execute(
            {"kind": "type", "target": malicious_selector, "value": "hello"}
        )
    )

    assert outcome.success is True
    expressions = [call[1][0] for call in helpers.calls if call[0] == "js"]
    assert expressions
    assert "__tasklens_injected" in expressions[0]
    assert "document.querySelector(\"#email'); window.__tasklens_injected = true; //\")" in expressions[0]


def test_observe_wraps_connection_error_with_stable_failure_kind():
    helpers = FakeHelpers()

    def disconnected():
        raise RuntimeError("daemon socket connection refused")

    helpers.page_info = disconnected
    adapter = HarnessBrowserAdapter(helpers=helpers)

    with pytest.raises(BrowserAdapterError) as exc_info:
        run(adapter.observe())

    assert exc_info.value.failure_kind == "connection"
