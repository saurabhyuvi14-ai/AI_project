"""Deterministic, read-only diagnosis over stored evaluation traces."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping, Protocol


MAX_SELECTED_RUNS = 3


class FailureCategory(StrEnum):
    TOOL_TIMEOUT = "tool_timeout"
    MISSING_ORDER_ID = "missing_order_id"
    UNKNOWN_ORDER = "unknown_order"
    INCOMPLETE_TOOL_RESULT = "incomplete_tool_result"
    CONFLICTING_INFORMATION = "conflicting_information"
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


@dataclass(frozen=True)
class DiagnosisRequest:
    run_ids: tuple[str, ...]
    goal: str


@dataclass(frozen=True)
class DiagnosisResult:
    run_ids: tuple[str, ...]
    goal: str
    likely_cause: str
    observed_facts: tuple[str, ...]
    cited_evidence_event_ids: tuple[str, ...]
    uncertainty: str
    recommended_next_step: str


def diagnose(
    request: DiagnosisRequest, tools: ReadOnlyDiagnosisTools
) -> DiagnosisResult:
    """Inspect selected runs once each and explain failures using trace-backed facts."""
    if not request.run_ids or len(request.run_ids) > MAX_SELECTED_RUNS:
        raise ValueError(f"run_ids must contain between 1 and {MAX_SELECTED_RUNS} values")
    if any(not run_id.strip() for run_id in request.run_ids):
        raise ValueError("run_ids cannot contain empty values")
    if len(set(request.run_ids)) != len(request.run_ids):
        raise ValueError("run_ids cannot contain duplicates")
    if not request.goal.strip():
        raise ValueError("goal cannot be empty")

    all_facts: list[str] = []
    cited_ids: list[str] = []
    causes: list[str] = []
    recommendations: list[str] = []
    uncertainty_reasons: list[str] = []
    seen_event_ids: set[str] = set()

    for run_id in request.run_ids:
        inspection = tools.inspect_run(run_id)
        if inspection.run_id != run_id:
            raise ValueError(f"inspect_run returned {inspection.run_id!r} for {run_id!r}")
        expectation = tools.get_case_expectation(inspection.case_id)
        if expectation.case_id != inspection.case_id:
            raise ValueError("get_case_expectation returned a mismatched case")
        summary = tools.get_failure_summary(run_id)
        if summary.run_id != run_id:
            raise ValueError(f"get_failure_summary returned a mismatched run for {run_id!r}")

        for event in inspection.events:
            if not event.event_id.strip():
                raise ValueError(f"run {run_id!r} contains an empty event ID")
            if event.event_id in seen_event_ids:
                raise ValueError(f"duplicate trace event ID: {event.event_id!r}")
            seen_event_ids.add(event.event_id)

        category = _normalize_category(summary.category)
        supported_events = _supporting_events(category, inspection.events)
        if supported_events:
            causes.append(_cause_for(category))
            recommendations.append(_next_step_for(category))
            for event in supported_events:
                cited_ids.append(event.event_id)
                all_facts.append(_event_fact(run_id, event))
        else:
            uncertainty_reasons.append(
                f"Run {run_id} has no trace event that supports category "
                f"{summary.category!s}; evaluator summary alone is insufficient."
            )
            if inspection.events:
                event = inspection.events[0]
                cited_ids.append(event.event_id)
                all_facts.append(_event_fact(run_id, event))

    if causes:
        unique_causes = tuple(dict.fromkeys(causes))
        likely_cause = (
            unique_causes[0]
            if len(unique_causes) == 1
            else "Selected runs show multiple supported failure causes: "
            + "; ".join(unique_causes)
        )
        recommended_next_step = "; ".join(dict.fromkeys(recommendations))
        uncertainty = (
            "The cause is a trace-supported hypothesis, not a confirmed root cause."
        )
        if uncertainty_reasons:
            uncertainty += " " + " ".join(uncertainty_reasons)
    else:
        likely_cause = "Insufficient trace evidence to identify a likely cause."
        recommended_next_step = (
            "Collect a complete, sanitized trace for the selected run before proposing a repair."
        )
        uncertainty = "Diagnosis abstained because the trace did not support the evaluator category."

    return DiagnosisResult(
        run_ids=request.run_ids,
        goal=request.goal,
        likely_cause=likely_cause,
        observed_facts=tuple(dict.fromkeys(all_facts)),
        cited_evidence_event_ids=tuple(dict.fromkeys(cited_ids)),
        uncertainty=uncertainty,
        recommended_next_step=recommended_next_step,
    )


def _normalize_category(category: FailureCategory | str) -> FailureCategory:
    try:
        return FailureCategory(category)
    except ValueError:
        return FailureCategory.OTHER


def _supporting_events(
    category: FailureCategory, events: tuple[TraceEvent, ...]
) -> tuple[TraceEvent, ...]:
    if category is FailureCategory.TOOL_TIMEOUT:
        return tuple(
            event
            for event in events
            if event.error and "timeout" in event.error.casefold()
        )
    if category is FailureCategory.UNKNOWN_ORDER:
        return tuple(
            event
            for event in events
            if event.error and "order_not_found" in event.error.casefold()
        )
    if category is FailureCategory.INCOMPLETE_TOOL_RESULT:
        return tuple(event for event in events if _has_incomplete_output(event))
    if category is FailureCategory.CONFLICTING_INFORMATION:
        return _conflicting_status_events(events)
    if category is FailureCategory.MISSING_ORDER_ID:
        return tuple(
            event
            for event in events
            if event.input is not None
            and ("order_id" not in event.input or not event.input["order_id"])
        )
    return ()


def _has_incomplete_output(event: TraceEvent) -> bool:
    if event.error and ("payload_corrupt" in event.error.casefold() or "incomplete" in event.error.casefold()):
        return True
    if event.output is None:
        return False
    if event.output.get("missing_fields"):
        return True
    return "items" in event.output and event.output["items"] in (None, [])


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


def _cause_for(category: FailureCategory) -> str:
    causes = {
        FailureCategory.TOOL_TIMEOUT: "A tool timeout is associated with the failed run.",
        FailureCategory.MISSING_ORDER_ID: "A lookup-related event has no order ID.",
        FailureCategory.UNKNOWN_ORDER: "The order lookup returned ORDER_NOT_FOUND.",
        FailureCategory.INCOMPLETE_TOOL_RESULT: "A tool returned an incomplete result.",
        FailureCategory.CONFLICTING_INFORMATION: "Trace events report conflicting status values.",
        FailureCategory.OTHER: "The trace does not match a supported failure pattern.",
    }
    return causes[category]


def _next_step_for(category: FailureCategory) -> str:
    next_steps = {
        FailureCategory.TOOL_TIMEOUT: "Review whether a bounded retry is safe for the timed-out read-only tool.",
        FailureCategory.MISSING_ORDER_ID: "Verify the agent asks for the missing order ID before lookup.",
        FailureCategory.UNKNOWN_ORDER: "Verify the supplied order ID and retain the current read-only behavior.",
        FailureCategory.INCOMPLETE_TOOL_RESULT: "Inspect the tool result schema and keep the agent from asserting missing facts.",
        FailureCategory.CONFLICTING_INFORMATION: "Preserve both source values and route the discrepancy for human review.",
        FailureCategory.OTHER: "Review the cited trace and evaluator assertion; do not propose an unsupported repair.",
    }
    return next_steps[category]


def _event_fact(run_id: str, event: TraceEvent) -> str:
    source = event.tool_name or event.event_type
    if event.error:
        detail = f"reported error {event.error!r}"
    elif event.output is not None:
        status = event.output.get("status", event.output.get("shipping_status"))
        detail = f"reported status {status!r}" if status is not None else (
            "returned fields " + ", ".join(sorted(event.output))
        )
    else:
        detail = "has no recorded output"
    return f"Run {run_id}: event {event.event_id} ({source}) {detail}."
