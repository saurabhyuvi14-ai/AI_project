"""
Read-only bridge from the SQLite trace store to the diagnosis component.

TraceStoreDiagnosisTools implements agentops.diagnosis.ReadOnlyDiagnosisTools, so
diagnosis reads persisted runs without knowing about SQLite:

    tools = TraceStoreDiagnosisTools(db_path, case_expectations_from_cases(build_canonical_case_list()))
    diagnose(DiagnosisRequest(run_ids=(run_id,), goal="Explain the failure."), tools)

Every query goes through the read-only functions in src/tracer.py.
"""
import hashlib
import json
from typing import Any, Dict, Iterable, Mapping, Optional

from agentops.diagnosis import (
    CaseExpectation,
    FailureCategory as DiagnosisCategory,
    FailureSummary,
    RunInspection,
    TraceEvent as DiagnosisTraceEvent,
)
from src.tracer import RunRecord, TraceEvent, get_run_trace, list_evaluations

# Stage labels written by src/trace_recorder.py
_TOOL_RESULT_STAGES = {"tool_success", "tool_failure"}

# Run-level error codes recorded by the agent -> diagnosis categories.
_CATEGORY_BY_ERROR_CODE = {
    "TIMEOUT_ERROR": DiagnosisCategory.TOOL_TIMEOUT,
    "MISSING_ORDER_ID": DiagnosisCategory.MISSING_ORDER_ID,
    "ORDER_NOT_FOUND": DiagnosisCategory.UNKNOWN_ORDER,
    "INCOMPLETE_TOOL_RESULT": DiagnosisCategory.INCOMPLETE_TOOL_RESULT,
    "CONFLICTING_INFORMATION": DiagnosisCategory.CONFLICTING_INFORMATION,
}

# Evaluator failure categories (src.evaluation.FailureCategory values) -> diagnosis categories.
_CATEGORY_BY_EVALUATOR = {
    "TIMEOUT": DiagnosisCategory.TOOL_TIMEOUT,
    "MISSING_INFORMATION": DiagnosisCategory.MISSING_ORDER_ID,
    "MALFORMED_RESULT": DiagnosisCategory.INCOMPLETE_TOOL_RESULT,
    "CONFLICTING_EVIDENCE": DiagnosisCategory.CONFLICTING_INFORMATION,
}

_TOOL_CONFIG_KEYS = ("max_tool_calls", "tool_timeout_ms", "retry_count", "validate_response")


def _loads(value: Optional[str]) -> Optional[Any]:
    return json.loads(value) if value is not None else None


def tool_config_version(run: RunRecord) -> str:
    """Stable short fingerprint of the tool-related settings a run used."""
    snapshot = _loads(run.config_snapshot) or {}
    tool_settings = {key: snapshot.get(key) for key in _TOOL_CONFIG_KEYS}
    digest = hashlib.sha256(json.dumps(tool_settings, sort_keys=True).encode()).hexdigest()
    return f"tools-{digest[:8]}"


def to_diagnosis_event(event: TraceEvent) -> DiagnosisTraceEvent:
    """
    Map a stored event to the diagnosis event shape. Tool calls expose their parameters,
    tool results expose the tool's data, and every event exposes its error code.
    """
    stored_input = _loads(event.input)
    stored_output = _loads(event.output)
    diag_input = None
    diag_output = None
    if event.stage == "tool_start" and isinstance(stored_input, dict):
        diag_input = stored_input.get("parameters")
    elif event.stage in _TOOL_RESULT_STAGES and isinstance(stored_output, dict):
        diag_output = stored_output.get("data")
    elif event.stage == "validation":
        diag_output = stored_output
    return DiagnosisTraceEvent(
        event_id=event.event_id,
        event_type=event.event_type,
        step=event.step_index or 0,
        tool_name=event.tool_name,
        input=diag_input,
        output=diag_output,
        error=event.error_code,
        duration_ms=int(round(event.duration_ms)) if event.duration_ms is not None else None,
    )


def inspect_run(db_path: str, run_id: str) -> RunInspection:
    """Read-only view of one stored run, with events in sequence order."""
    trace = get_run_trace(db_path, run_id)
    if trace is None:
        raise KeyError(f"unknown run_id {run_id!r}")
    run = trace.run
    return RunInspection(
        run_id=run.run_id,
        case_id=run.case_id,
        agent_config_version=run.agent_version,
        tool_config_version=tool_config_version(run),
        started_at=run.start_time,
        completed_at=run.end_time or "",
        events=tuple(to_diagnosis_event(event) for event in trace.events),
    )


def failure_summary(db_path: str, run_id: str) -> FailureSummary:
    """Summarise why a stored run failed, from its run record and latest stored evaluation."""
    trace = get_run_trace(db_path, run_id)
    if trace is None:
        raise KeyError(f"unknown run_id {run_id!r}")
    run = trace.run
    evaluations = list_evaluations(db_path, run_id)
    latest = evaluations[-1] if evaluations else None

    category = _CATEGORY_BY_ERROR_CODE.get(run.error_code or "")
    if category is None and latest is not None and latest.failure_category:
        category = _CATEGORY_BY_EVALUATOR.get(latest.failure_category, DiagnosisCategory.OTHER)
    if category is None:
        category = DiagnosisCategory.OTHER

    failed_checks: tuple = ()
    if latest is not None:
        checks = _loads(latest.checks) or {}
        failed_checks = tuple(name for name, passed in checks.items() if not passed)

    expected = (
        f"Evaluation {latest.eval_id} checks pass" if latest is not None
        else "No stored evaluation for this run"
    )
    actual = f"status={run.status}, stop_reason={run.stop_reason}, error_code={run.error_code}"
    if latest is not None:
        actual += f", verdict={latest.verdict}"
    return FailureSummary(
        run_id=run.run_id,
        category=category,
        failed_assertions=failed_checks,
        expected=expected,
        actual=actual,
    )


def case_expectations_from_cases(cases: Iterable[Any]) -> Dict[str, CaseExpectation]:
    """Build diagnosis case expectations from evaluator Case objects (src.evaluation.Case)."""
    expectations = {}
    for case in cases:
        outcome = case.expected_outcome
        status = getattr(outcome.expected_status, "value", outcome.expected_status)
        assertions = [f"status == {status}"]
        if outcome.expected_error_code:
            assertions.append(f"error_code == {outcome.expected_error_code}")
        assertions.extend(f"answer mentions {kw!r}" for kw in outcome.required_keywords)
        assertions.append(f"tool calls between {outcome.min_tool_calls} and {outcome.max_tool_calls}")
        expectations[case.case_id] = CaseExpectation(
            case_id=case.case_id,
            required_behavior=case.description or f"Finish with status {status}.",
            assertions=tuple(assertions),
            safety_constraints=tuple(f"answer must not mention {kw!r}" for kw in outcome.forbidden_keywords)
            + tuple(f"must not call {name}" for name in outcome.forbidden_tool_names),
        )
    return expectations


class TraceStoreDiagnosisTools:
    """ReadOnlyDiagnosisTools backed by the SQLite trace store. Never writes."""

    def __init__(self, db_path: str, case_expectations: Optional[Mapping[str, CaseExpectation]] = None):
        self.db_path = db_path
        self._case_expectations = dict(case_expectations or {})

    def inspect_run(self, run_id: str) -> RunInspection:
        return inspect_run(self.db_path, run_id)

    def get_case_expectation(self, case_id: str) -> CaseExpectation:
        try:
            return self._case_expectations[case_id]
        except KeyError:
            raise KeyError(f"no case expectation registered for {case_id!r}") from None

    def get_failure_summary(self, run_id: str) -> FailureSummary:
        return failure_summary(self.db_path, run_id)
