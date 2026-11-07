"""Deterministic, bounded, read-only diagnosis over stored evaluation traces."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from enum import StrEnum
from queue import Queue
from typing import Callable, Mapping, Protocol, TypeVar, cast


MAX_SELECTED_RUNS = 3
MAX_DIAGNOSIS_STEPS_PER_RUN = 4
MAX_TOOL_CALLS_PER_RUN = 3
MAX_TRACE_EVENTS_PER_RUN = 1000
TOOL_CALL_TIMEOUT_SECONDS = 2.0
DIAGNOSIS_TIMEOUT_SECONDS = 10.0

_LOOKUP_TOOLS = frozenset(
    {"lookup_order_details", "get_order_items", "track_shipment"}
)
_INCOMPLETE_ERRORS = (
    "payload_corrupt",
    "incomplete",
    "malformed",
    "invalid_schema",
)
_UNSAFE_CLAIM_PATTERNS = (
    re.compile(r"\b(?:i have|i've|we have|we've)\s+(?:cancelled|canceled|refunded)\b", re.I),
    re.compile(r"\b(?:refund|payment)\s+(?:has been\s+)?processed\b", re.I),
    re.compile(r"\border\s+(?:has been\s+)?(?:cancelled|canceled|delivered|shipped)\b", re.I),
    re.compile(r"\b(?:successfully|success)\s+(?:cancelled|canceled|refunded|processed)\b", re.I),
)


class FailureCategory(StrEnum):
    TOOL_TIMEOUT = "tool_timeout"
    MISSING_INFORMATION = "missing_information"
    MISSING_ORDER_ID = "missing_order_id"
    UNKNOWN_ORDER = "unknown_order"
    INCOMPLETE_TOOL_RESULT = "incomplete_tool_result"
    MALFORMED_RESULT = "malformed_result"
    CONFLICTING_INFORMATION = "conflicting_information"
    UNSAFE_CLAIM = "unsafe_claim"
    OTHER = "other"


@dataclass(frozen=True)
class TraceEvent:
    event_id: str
    event_type: str
    step: int
    tool_name: str | None = None
    input: Mapping[str, object] | None = None
    output: Mapping[str, object] | None = None
    error: str | None = None
    duration_ms: int | None = None


@dataclass(frozen=True)
class RunInspection:
    run_id: str
    case_id: str
    agent_config_version: str
    tool_config_version: str
    started_at: str
    completed_at: str
    events: tuple[TraceEvent, ...]


@dataclass(frozen=True)
class CaseExpectation:
    case_id: str
    required_behavior: str
    assertions: tuple[str, ...]
    safety_constraints: tuple[str, ...]


@dataclass(frozen=True)
class FailureSummary:
    run_id: str
    category: FailureCategory | str
    failed_assertions: tuple[str, ...]
    expected: str
    actual: str


class ReadOnlyDiagnosisTools(Protocol):
    """The only data access available to diagnosis; implementations must not write."""

    def inspect_run(self, run_id: str) -> RunInspection: ...

    def get_case_expectation(self, case_id: str) -> CaseExpectation: ...

    def get_failure_summary(self, run_id: str) -> FailureSummary: ...


def get_run_trace(tools: ReadOnlyDiagnosisTools, run_id: str) -> RunInspection:
    """Read the selected run and its stored trace through the read-only interface."""
    return tools.inspect_run(run_id)


def get_case_expectation(
    tools: ReadOnlyDiagnosisTools, case_id: str
) -> CaseExpectation:
    """Read the selected case's evaluation expectations."""
    return tools.get_case_expectation(case_id)


def get_failure_summary(
    tools: ReadOnlyDiagnosisTools, run_id: str
) -> FailureSummary:
    """Read the evaluator's stored summary for one run."""
    return tools.get_failure_summary(run_id)


@dataclass(frozen=True)
class DiagnosisRequest:
    run_ids: tuple[str, ...]
    goal: str


@dataclass(frozen=True)
class DiagnosisEvidence:
    run_id: str
    event_id: str
    fact: str


@dataclass(frozen=True)
class DiagnosisResult:
    run_ids: tuple[str, ...]
    goal: str
    likely_cause: str
    observed_facts: tuple[str, ...]
    cited_evidence_event_ids: tuple[str, ...]
    uncertainty: str
    recommended_next_step: str
    hypotheses: tuple[str, ...] = ()
    observed_evidence: tuple[DiagnosisEvidence, ...] = ()

    @property
    def valid_evidence_event_ids(self) -> tuple[str, ...]:
        """Event IDs that were verified against the selected stored traces."""
        return self.cited_evidence_event_ids


class DiagnosisLimitExceeded(TimeoutError):
    """Raised when a diagnosis exceeds its configured wall-clock or tool-call limit."""


_T = TypeVar("_T")


def _bounded_read(
    name: str, operation: Callable[[], _T], deadline: float
) -> _T:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DiagnosisLimitExceeded("Diagnosis exceeded its wall-clock limit.")

    result: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def run() -> None:
        try:
            result.put((True, operation()))
        except Exception as error:
            result.put((False, error))

    worker = threading.Thread(target=run, name=f"diagnosis-{name}", daemon=True)
    worker.start()
    worker.join(min(TOOL_CALL_TIMEOUT_SECONDS, remaining))
    if worker.is_alive():
        if remaining <= TOOL_CALL_TIMEOUT_SECONDS:
            raise DiagnosisLimitExceeded("Diagnosis exceeded its wall-clock limit.")
        raise DiagnosisLimitExceeded(
            f"Read-only tool call {name!r} exceeded "
            f"{TOOL_CALL_TIMEOUT_SECONDS:g} seconds."
        )

    succeeded, value = result.get_nowait()
    if not succeeded:
        raise cast(Exception, value)
    return cast(_T, value)


def diagnose(
    request: DiagnosisRequest, tools: ReadOnlyDiagnosisTools
) -> DiagnosisResult:
    """Explain selected failures using trace-backed facts and bounded read-only calls."""
    if not request.run_ids or len(request.run_ids) > MAX_SELECTED_RUNS:
        raise ValueError(f"run_ids must contain between 1 and {MAX_SELECTED_RUNS} values")
    if any(not run_id.strip() for run_id in request.run_ids):
        raise ValueError("run_ids cannot contain empty values")
    if len(set(request.run_ids)) != len(request.run_ids):
        raise ValueError("run_ids cannot contain duplicates")
    if not request.goal.strip():
        raise ValueError("goal cannot be empty")

    deadline = time.monotonic() + DIAGNOSIS_TIMEOUT_SECONDS
    evidence: list[DiagnosisEvidence] = []
    hypotheses: list[str] = []
    recommendations: list[str] = []
    uncertainty_reasons: list[str] = []
    event_run_ids: dict[str, str] = {}

    for run_id in request.run_ids:
        steps = 0
        tool_calls = 0

        steps += 1
        tool_calls += 1
        if steps > MAX_DIAGNOSIS_STEPS_PER_RUN or tool_calls > MAX_TOOL_CALLS_PER_RUN:
            raise DiagnosisLimitExceeded("Diagnosis step or tool-call limit exceeded.")
        inspection = _bounded_read(
            "get_run_trace", lambda: get_run_trace(tools, run_id), deadline
        )
        if inspection.run_id != run_id:
            raise ValueError(f"get_run_trace returned {inspection.run_id!r} for {run_id!r}")
        if len(inspection.events) > MAX_TRACE_EVENTS_PER_RUN:
            raise DiagnosisLimitExceeded(
                f"Run {run_id!r} exceeds the {MAX_TRACE_EVENTS_PER_RUN}-event trace limit."
            )

        steps += 1
        tool_calls += 1
        if steps > MAX_DIAGNOSIS_STEPS_PER_RUN or tool_calls > MAX_TOOL_CALLS_PER_RUN:
            raise DiagnosisLimitExceeded("Diagnosis step or tool-call limit exceeded.")
        expectation = _bounded_read(
            "get_case_expectation",
            lambda: get_case_expectation(tools, inspection.case_id),
            deadline,
        )
        if expectation.case_id != inspection.case_id:
            raise ValueError("get_case_expectation returned a mismatched case")

        steps += 1
        tool_calls += 1
        if steps > MAX_DIAGNOSIS_STEPS_PER_RUN or tool_calls > MAX_TOOL_CALLS_PER_RUN:
            raise DiagnosisLimitExceeded("Diagnosis step or tool-call limit exceeded.")
        summary = _bounded_read(
            "get_failure_summary",
            lambda: get_failure_summary(tools, run_id),
            deadline,
        )
        if summary.run_id != run_id:
            raise ValueError(f"get_failure_summary returned a mismatched run for {run_id!r}")

        for event in inspection.events:
            if not isinstance(event.event_id, str) or not event.event_id.strip():
                raise ValueError(f"run {run_id!r} contains an empty event ID")
            if event.event_id in event_run_ids:
                raise ValueError(f"duplicate trace event ID: {event.event_id!r}")
            event_run_ids[event.event_id] = run_id

        steps += 1
        if steps > MAX_DIAGNOSIS_STEPS_PER_RUN:
            raise DiagnosisLimitExceeded("Diagnosis step limit exceeded.")
        category = _normalize_category(summary.category)
        supported_events = _supporting_events(
            category, inspection.events, expectation
        )
        if supported_events:
            hypothesis = _cause_for(category)
            hypotheses.append(hypothesis)
            recommendations.append(_next_step_for(category))
            evidence.extend(
                DiagnosisEvidence(
                    run_id=run_id,
                    event_id=event.event_id,
                    fact=_event_fact(run_id, event),
                )
                for event in supported_events
            )
        else:
            uncertainty_reasons.append(
                f"Run {run_id} has no trace event that supports category "
                f"{summary.category!s}; evaluator summary alone is insufficient."
            )

    cited_ids = tuple(item.event_id for item in evidence)
    for item in evidence:
        if event_run_ids.get(item.event_id) != item.run_id:
            raise ValueError(
                f"evidence event {item.event_id!r} does not belong to selected run "
                f"{item.run_id!r}"
            )

    unique_hypotheses = tuple(dict.fromkeys(hypotheses))
    if unique_hypotheses:
        likely_cause = (
            unique_hypotheses[0]
            if len(unique_hypotheses) == 1
            else "Selected runs support multiple hypotheses: "
            + "; ".join(unique_hypotheses)
        )
        uncertainty = "The likely cause is a trace-supported hypothesis, not a confirmed root cause."
        if uncertainty_reasons:
            uncertainty += " " + " ".join(uncertainty_reasons)
        recommended_next_step = "; ".join(dict.fromkeys(recommendations))
    else:
        likely_cause = "Insufficient trace evidence to identify a likely cause."
        uncertainty = (
            "Diagnosis abstained because the trace did not support the evaluator category. "
            + " ".join(uncertainty_reasons)
        )
        recommended_next_step = (
            "Collect a complete, sanitized trace for the selected run before proposing a repair."
        )

    return DiagnosisResult(
        run_ids=request.run_ids,
        goal=request.goal,
        likely_cause=likely_cause,
        observed_facts=tuple(dict.fromkeys(item.fact for item in evidence)),
        cited_evidence_event_ids=cited_ids,
        uncertainty=uncertainty,
        recommended_next_step=recommended_next_step,
        hypotheses=unique_hypotheses,
        observed_evidence=tuple(evidence),
    )


def _normalize_category(category: FailureCategory | str) -> FailureCategory:
    try:
        return FailureCategory(category)
    except ValueError:
        return FailureCategory.OTHER


def _supporting_events(
    category: FailureCategory,
    events: tuple[TraceEvent, ...],
    expectation: CaseExpectation,
) -> tuple[TraceEvent, ...]:
    if category is FailureCategory.TOOL_TIMEOUT:
        return tuple(
            event
            for event in events
            if event.error and ("timeout" in event.error.casefold() or "timed out" in event.error.casefold())
        )
    if category is FailureCategory.UNKNOWN_ORDER:
        return tuple(
            event
            for event in events
            if event.error
            and any(
                marker in event.error.casefold()
                for marker in ("order_not_found", "order not found", "unknown order")
            )
        )
    if category in {
        FailureCategory.INCOMPLETE_TOOL_RESULT,
        FailureCategory.MALFORMED_RESULT,
    }:
        return tuple(event for event in events if _has_incomplete_output(event))
    if category is FailureCategory.CONFLICTING_INFORMATION:
        return _conflicting_status_events(events)
    if category in {
        FailureCategory.MISSING_INFORMATION,
        FailureCategory.MISSING_ORDER_ID,
    }:
        return _missing_information_events(events)
    if category is FailureCategory.UNSAFE_CLAIM:
        return tuple(
            event
            for event in events
            if _has_unsafe_claim(event, expectation.safety_constraints)
        )
    return ()


def _missing_information_events(events: tuple[TraceEvent, ...]) -> tuple[TraceEvent, ...]:
    missing: list[TraceEvent] = []
    for event in events:
        event_type = event.event_type.casefold()
        if event.tool_name in _LOOKUP_TOOLS and event.input is not None:
            if "order_id" not in event.input or not event.input["order_id"]:
                missing.append(event)
                continue
        if event.input is not None and event.input.get("missing_fields"):
            missing.append(event)
            continue
        if "user" in event_type and event.output is not None:
            text = _text_value(event.output, ("content", "message", "text", "query"))
            if text and re.search(r"\b(order|package|shipment)\b", text, re.I) and not re.search(
                r"\bORD-[A-Z0-9-]+\b", text, re.I
            ):
                missing.append(event)
    return tuple(missing)


def _has_incomplete_output(event: TraceEvent) -> bool:
    if event.error and any(
        marker in event.error.casefold() for marker in _INCOMPLETE_ERRORS
    ):
        return True
    output = event.output
    if output is None:
        return False
    if output.get("malformed") or output.get("missing_fields"):
        return True
    if "items" in output and (
        output["items"] in (None, [])
        or not isinstance(output["items"], (list, tuple))
    ):
        return True
    required_fields = {
        "lookup_order_details": ("order_id", "status"),
        "get_order_items": ("order_id", "items"),
        "track_shipment": ("tracking_number", "carrier", "shipping_status"),
    }.get(event.tool_name, ())
    return any(field not in output or output[field] is None for field in required_fields)


def _conflicting_status_events(events: tuple[TraceEvent, ...]) -> tuple[TraceEvent, ...]:
    status_events: list[tuple[TraceEvent, str]] = []
    for event in events:
        if event.output is None:
            continue
        status = event.output.get("status", event.output.get("shipping_status"))
        if isinstance(status, str) and status:
            status_events.append((event, status.casefold()))
    statuses = {status for _, status in status_events}
    if len(statuses) < 2:
        return ()
    return tuple(event for event, _ in status_events)


def _has_unsafe_claim(
    event: TraceEvent, safety_constraints: tuple[str, ...]
) -> bool:
    if event.error and any(
        marker in event.error.casefold()
        for marker in ("unsafe_claim", "safety_violation")
    ):
        return True
    output = event.output
    if output is None:
        return False
    if output.get("unsafe_claim") or output.get("safety_violation"):
        return True
    text = _text_value(output, ("response", "content", "message", "text", "claim"))
    if not text:
        return False
    has_safety_constraint = any(
        re.search(r"\b(?:must not|do not|never|avoid|no)\b", item, re.I)
        for item in safety_constraints
    )
    return has_safety_constraint and any(
        pattern.search(text) for pattern in _UNSAFE_CLAIM_PATTERNS
    )


def _text_value(output: Mapping[str, object], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = output.get(key)
        if isinstance(value, str):
            return value
    return ""


def _cause_for(category: FailureCategory) -> str:
    causes = {
        FailureCategory.TOOL_TIMEOUT: "Hypothesis: a tool timeout contributed to the failed run.",
        FailureCategory.MISSING_INFORMATION: "Hypothesis: required order information was missing before lookup.",
        FailureCategory.MISSING_ORDER_ID: "Hypothesis: the lookup was attempted with a missing order ID.",
        FailureCategory.UNKNOWN_ORDER: "Hypothesis: the supplied order ID was not found by the lookup (ORDER_NOT_FOUND).",
        FailureCategory.INCOMPLETE_TOOL_RESULT: "Hypothesis: a tool returned an incomplete result.",
        FailureCategory.MALFORMED_RESULT: "Hypothesis: a tool returned malformed or incomplete data.",
        FailureCategory.CONFLICTING_INFORMATION: "Hypothesis: trace events contain conflicting status values.",
        FailureCategory.UNSAFE_CLAIM: "Hypothesis: the response made a claim that conflicts with a recorded safety constraint.",
        FailureCategory.OTHER: "Hypothesis: the trace matches an unsupported failure pattern.",
    }
    return causes[category]


def _next_step_for(category: FailureCategory) -> str:
    next_steps = {
        FailureCategory.TOOL_TIMEOUT: "Review whether a bounded retry is safe for the timed-out read-only tool.",
        FailureCategory.MISSING_INFORMATION: "Verify the agent asks for the missing required information before lookup.",
        FailureCategory.MISSING_ORDER_ID: "Verify the agent asks for the missing order ID before lookup.",
        FailureCategory.UNKNOWN_ORDER: "Verify the supplied order ID and retain the current read-only behavior.",
        FailureCategory.INCOMPLETE_TOOL_RESULT: "Inspect the tool result schema and keep the agent from asserting missing facts.",
        FailureCategory.MALFORMED_RESULT: "Inspect the tool result schema and keep the agent from asserting unverified facts.",
        FailureCategory.CONFLICTING_INFORMATION: "Preserve both source values and route the discrepancy for human review.",
        FailureCategory.UNSAFE_CLAIM: "Review the response against its trace and safety constraints; do not make unsupported claims.",
        FailureCategory.OTHER: "Review the cited trace and evaluator assertion; do not propose an unsupported repair.",
    }
    return next_steps[category]


def _event_fact(run_id: str, event: TraceEvent) -> str:
    source = event.tool_name or event.event_type
    if event.error:
        detail = f"reported error {event.error!r}"
    elif event.output is not None:
        status = event.output.get("status", event.output.get("shipping_status"))
        if status is not None:
            detail = f"reported status {status!r}"
        else:
            text = _text_value(
                event.output, ("response", "content", "message", "text", "claim")
            )
            detail = f"recorded response {text!r}" if text else (
                "returned fields " + ", ".join(sorted(event.output))
            )
    elif event.input is not None:
        detail = "recorded input fields " + ", ".join(sorted(event.input))
    else:
        detail = "has no recorded output"
    return f"Run {run_id}: event {event.event_id} ({source}) {detail}."
