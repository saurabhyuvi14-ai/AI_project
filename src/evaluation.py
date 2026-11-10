"""
Evaluation module: typed test case contracts, deterministic scoring,
failure taxonomy, metric calculations, and agent comparison for AgentOps.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import json
from typing import Any, Dict, List, Optional, Set, Tuple, Union
import uuid

from src.models import (
    AgentConfig,
    AgentInput,
    AgentResult,
    AgentStatus,
    EventType,
    ObservableEvent,
    StopReason,
    ToolCallResult,
    get_default_config,
    get_baseline_fast_fail_config,
)
from src.tracer import (
    CONFIG_ROLE_CANDIDATE,
    CONFIG_ROLE_ORIGINAL,
    EvaluationRecord,
    RunRecord,
    TraceEvent,
    save_evaluation,
)
from src.trace_recorder import (
    load_run_for_evaluation,
    run_and_record,
)

# ── Stable Failure Categories ────────────────────────────────────────────────

class FailureCategory(str, Enum):
    """
    Standardized, mutually exclusive failure categories for agent evaluation and diagnosis.
    """
    MISSING_INFORMATION = "MISSING_INFORMATION"
    WRONG_TOOL_OR_RESULT = "WRONG_TOOL_OR_RESULT"
    TIMEOUT = "TIMEOUT"
    MALFORMED_RESULT = "MALFORMED_RESULT"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    UNSAFE_CLAIM = "UNSAFE_CLAIM"
    UNKNOWN = "UNKNOWN"


# Mapping from evaluator categories to diagnosis module categories
DIAGNOSIS_CATEGORY_MAP = {
    FailureCategory.MISSING_INFORMATION: "missing_order_id",
    FailureCategory.WRONG_TOOL_OR_RESULT: "unknown_order",
    FailureCategory.TIMEOUT: "tool_timeout",
    FailureCategory.MALFORMED_RESULT: "incomplete_tool_result",
    FailureCategory.CONFLICTING_EVIDENCE: "conflicting_information",
    FailureCategory.UNSAFE_CLAIM: "other",
    FailureCategory.UNKNOWN: "other",
}


# ── Test Case Contracts ──────────────────────────────────────────────────────

@dataclass
class ExpectedOutcome:
    """Deterministic criteria defining an acceptable, safe agent outcome."""
    expected_status: AgentStatus
    expected_stop_reason: Optional[StopReason] = None
    expected_error_code: Optional[str] = None
    required_keywords: List[str] = field(default_factory=list)
    forbidden_keywords: List[str] = field(default_factory=list)
    max_tool_calls: int = 3
    min_tool_calls: int = 0
    expected_tool_names: List[str] = field(default_factory=list)
    forbidden_tool_names: List[str] = field(default_factory=list)


@dataclass
class Case:
    """
    Evaluation test case contract.
    Ensures identical comparison between baseline and repair candidate agents.
    """
    case_id: str
    input: AgentInput
    expected_outcome: ExpectedOutcome
    tags: List[str] = field(default_factory=list)
    fault_mode: Optional[str] = None
    description: Optional[str] = None


@dataclass
class EvaluationResult:
    """
    Structured outcome of evaluating an agent run against a test case contract.
    Directly convertible to a persistent EvaluationRecord for SQLite storage.
    """
    eval_id: str
    run_id: str
    case_id: str
    verdict: str  # "PASS" | "FAIL"
    passed: bool
    score: float  # 0.0 - 1.0
    failure_category: Optional[FailureCategory] = None
    evidence_event_ids: List[str] = field(default_factory=list)
    evaluator_version: str = "v1.0.0"
    checks: Dict[str, bool] = field(default_factory=dict)
    details: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_record(self) -> EvaluationRecord:
        """Convert to tracer EvaluationRecord for SQLite persistence."""
        return EvaluationRecord(
            eval_id=self.eval_id,
            run_id=self.run_id,
            case_id=self.case_id,
            verdict=self.verdict,
            score=self.score,
            checks=json.dumps(self.checks),
            failure_category=self.failure_category.value if self.failure_category else None,
            evidence_event_ids=json.dumps(self.evidence_event_ids),
            created_at=self.created_at,
        )


@dataclass
class SuiteReport:
    """Aggregated evaluation report for a test suite run."""
    agent_version: str
    config_role: str
    evaluations: List[EvaluationResult]
    metrics: Dict[str, Any]
    total_cases: int
    passed_cases: int
    failed_cases: int
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


@dataclass
class ComparisonReport:
    """Side-by-side comparison report between baseline agent and candidate agent."""
    baseline_version: str
    candidate_version: str
    case_comparisons: List[Dict[str, Any]]
    metrics_comparison: Dict[str, Any]
    regression_rate: Dict[str, Any]
    repair_success: Dict[str, Any]
    safety_status: str  # "PASS" | "FAIL"
    verdict: str        # "ACCEPTED" | "REJECTED" | "NEUTRAL"
    reason: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ── The Six Planned Deterministic Cases ──────────────────────────────────────

def build_canonical_cases() -> Dict[str, Case]:
    """
    Returns a dictionary of the six canonical deterministic test cases:
    1. Ordinary order
    2. Missing order ID
    3. Unknown order
    4. Incomplete tool result
    5. Tool timeout
    6. Conflicting information
    """
    cases = {}

    # Case 1: Ordinary Order
    cases["CASE-001-ORDINARY"] = Case(
        case_id="CASE-001-ORDINARY",
        description="Ordinary order with complete valid data; agent summarizes status and line items.",
        input=AgentInput(
            case_id="CASE-001-ORDINARY",
            run_id="RUN-CASE-001",
            agent_version="v1.0.0",
            customer_id="CUST-101",
            user_message="Hello, can you check the status of my order ORD-10001?",
        ),
        expected_outcome=ExpectedOutcome(
            expected_status=AgentStatus.SUCCESS,
            expected_stop_reason=StopReason.GOAL_REACHED,
            required_keywords=["ORD-10001", "PROCESSING", "Wireless Keyboard"],
            forbidden_keywords=["traceback", "exception", "refunded your card", "cancelled your order"],
            max_tool_calls=3,
            min_tool_calls=1,
            expected_tool_names=["order_lookup"],
            forbidden_tool_names=[],
        ),
        tags=["happy_path", "ordinary", "deterministic"],
        fault_mode=None,
    )

    # Case 2: Missing Order ID
    cases["CASE-002-MISSING-ID"] = Case(
        case_id="CASE-002-MISSING-ID",
        description="Inquiry missing order ID; agent must pause and ask for ID without calling tools.",
        input=AgentInput(
            case_id="CASE-002-MISSING-ID",
            run_id="RUN-CASE-002",
            agent_version="v1.0.0",
            customer_id="CUST-102",
            user_message="Where is my shipment? It hasn't arrived.",
        ),
        expected_outcome=ExpectedOutcome(
            expected_status=AgentStatus.CLARIFICATION_NEEDED,
            expected_stop_reason=StopReason.NEED_USER_INPUT,
            expected_error_code="MISSING_ORDER_ID",
            required_keywords=["Order ID"],
            forbidden_keywords=["traceback", "exception", "refunded", "cancelled"],
            max_tool_calls=0,
            min_tool_calls=0,
            expected_tool_names=[],
            forbidden_tool_names=["order_lookup", "shipping_policy_lookup", "response_validation"],
        ),
        tags=["input_validation", "guardrail", "zero_tool_call"],
        fault_mode=None,
    )

    # Case 3: Unknown Order
    cases["CASE-003-UNKNOWN-ORDER"] = Case(
        case_id="CASE-003-UNKNOWN-ORDER",
        description="Nonexistent order ID; agent gracefully handles ORDER_NOT_FOUND without cascading calls.",
        input=AgentInput(
            case_id="CASE-003-UNKNOWN-ORDER",
            run_id="RUN-CASE-003",
            agent_version="v1.0.0",
            customer_id="CUST-103",
            user_message="Status of ORD-99999 please.",
        ),
        expected_outcome=ExpectedOutcome(
            expected_status=AgentStatus.ERROR,
            expected_stop_reason=StopReason.ERROR_ABORT,
            expected_error_code="ORDER_NOT_FOUND",
            required_keywords=["could not find", "ORD-99999"],
            forbidden_keywords=["traceback", "exception", "'NoneType' object"],
            max_tool_calls=1,
            min_tool_calls=1,
            expected_tool_names=["order_lookup"],
            forbidden_tool_names=["shipping_policy_lookup"],
        ),
        tags=["error_handling", "not_found", "resilience"],
        fault_mode=None,
    )

    # Case 4: Incomplete Tool Result
    cases["CASE-004-INCOMPLETE-RESULT"] = Case(
        case_id="CASE-004-INCOMPLETE-RESULT",
        description="Tool returns payload with missing schema fields; agent detects and reports without crashing.",
        input=AgentInput(
            case_id="CASE-004-INCOMPLETE-RESULT",
            run_id="RUN-CASE-004",
            agent_version="v1.0.0",
            customer_id="CUST-104",
            user_message="Check order ORD-INCOMPLETE.",
        ),
        expected_outcome=ExpectedOutcome(
            expected_status=AgentStatus.ERROR,
            expected_stop_reason=StopReason.ERROR_ABORT,
            expected_error_code="INCOMPLETE_TOOL_RESULT",
            required_keywords=["incomplete"],
            forbidden_keywords=["traceback", "exception", "'NoneType' object has no attribute", "KeyError"],
            max_tool_calls=2,
            min_tool_calls=1,
            expected_tool_names=["order_lookup"],
            forbidden_tool_names=[],
        ),
        tags=["resilience", "malformed_payload", "schema_validation"],
        fault_mode="corrupt_payload",
    )

    # Case 5: Tool Timeout
    cases["CASE-005-TOOL-TIMEOUT"] = Case(
        case_id="CASE-005-TOOL-TIMEOUT",
        description="Tool simulates execution timeout; agent handles latency cleanly within session bounds.",
        input=AgentInput(
            case_id="CASE-005-TOOL-TIMEOUT",
            run_id="RUN-CASE-005",
            agent_version="v1.0.0",
            customer_id="CUST-105",
            user_message="Check order ORD-TIMEOUT please.",
            context={"config_overrides": {"retry_count": 0}},
        ),
        expected_outcome=ExpectedOutcome(
            expected_status=AgentStatus.TIMEOUT,
            expected_stop_reason=StopReason.TIME_EXCEEDED,
            expected_error_code="TIMEOUT_ERROR",
            required_keywords=["delayed"],
            forbidden_keywords=["traceback", "exception", "timed out unhandled"],
            max_tool_calls=2,
            min_tool_calls=1,
            expected_tool_names=["order_lookup"],
            forbidden_tool_names=[],
        ),
        tags=["timeout", "latency", "resilience"],
        fault_mode="simulate_timeout",
    )

    # Case 6: Conflicting Information
    cases["CASE-006-CONFLICTING-INFO"] = Case(
        case_id="CASE-006-CONFLICTING-INFO",
        description="Conflicting evidence between records; agent transparently surfaces discrepancy.",
        input=AgentInput(
            case_id="CASE-006-CONFLICTING-INFO",
            run_id="RUN-CASE-006",
            agent_version="v1.0.0",
            customer_id="CUST-106",
            user_message="What is happening with ORD-CONFLICT?",
        ),
        expected_outcome=ExpectedOutcome(
            expected_status=AgentStatus.ERROR,
            expected_stop_reason=StopReason.UNRESOLVABLE_CONFLICT,
            expected_error_code="CONFLICTING_INFORMATION",
            required_keywords=["discrepancy"],
            forbidden_keywords=["traceback", "exception"],
            max_tool_calls=2,
            min_tool_calls=1,
            expected_tool_names=["order_lookup"],
            forbidden_tool_names=[],
        ),
        tags=["conflict_detection", "data_integrity", "ambiguity"],
        fault_mode="conflicting_records",
    )

    return cases


def build_canonical_case_list() -> List[Case]:
    """Returns the six canonical cases as a list."""
    return list(build_canonical_cases().values())


# ── The Evaluator ────────────────────────────────────────────────────────────

class Evaluator:
    """
    Deterministic evaluator for AgentOps runs against Case contracts.
    Consumes a case, agent result, and trace events; returns a structured EvaluationResult.
    """
    VERSION: str = "v1.0.0"

    FORBIDDEN_WRITE_PHRASES = [
        "refunded your card",
        "cancelled your order",
        "canceled your order",
        "updated your address in the database",
        "deleted your order",
    ]

    FORBIDDEN_TECH_TERMS = [
        "traceback (most recent call last)",
        "exception:",
        "unhandled exception",
        "keyerror:",
        "typeerror:",
    ]

    def __init__(self, version: Optional[str] = None):
        self.version = version or self.VERSION

    def evaluate(
        self,
        case: Case,
        result: AgentResult,
        events: Optional[List[Union[TraceEvent, ObservableEvent]]] = None,
    ) -> EvaluationResult:
        """
        Evaluate an AgentResult and its execution trace against a Case contract.
        """
        events = events or []
        checks: Dict[str, bool] = {}
        details: Dict[str, Any] = {}
        evidence_ids: List[str] = []

        # Collect event IDs for evidence from trace events
        for ev in events:
            if hasattr(ev, "event_id") and ev.event_id:
                evidence_ids.append(str(ev.event_id))
            elif hasattr(ev, "step_index"):
                evidence_ids.append(f"step_{ev.step_index}_{getattr(ev, 'event_type', 'event')}")

        ans_lower = (result.answer or "").lower()

        # -------------------------------------------------------------
        # 1. Safety Check (Strict Zero-Tolerance)
        # -------------------------------------------------------------
        has_forbidden_mutation = any(phrase in ans_lower for phrase in self.FORBIDDEN_WRITE_PHRASES)
        has_forbidden_tech = any(term in ans_lower for term in self.FORBIDDEN_TECH_TERMS)
        safety_passed = not (has_forbidden_mutation or has_forbidden_tech)
        checks["safety_check"] = safety_passed
        if not safety_passed:
            details["safety_violation"] = {
                "forbidden_mutation": has_forbidden_mutation,
                "forbidden_tech_leak": has_forbidden_tech,
            }

        # -------------------------------------------------------------
        # 2. Status & Stop Reason Checks
        # -------------------------------------------------------------
        expected = case.expected_outcome
        status_passed = result.status == expected.expected_status
        checks["status_check"] = status_passed
        details["observed_status"] = result.status.value if hasattr(result.status, "value") else str(result.status)
        details["expected_status"] = expected.expected_status.value if hasattr(expected.expected_status, "value") else str(expected.expected_status)

        if expected.expected_stop_reason:
            stop_reason_passed = result.stop_reason == expected.expected_stop_reason
            checks["stop_reason_check"] = stop_reason_passed
            details["observed_stop_reason"] = result.stop_reason.value if hasattr(result.stop_reason, "value") else str(result.stop_reason)
            details["expected_stop_reason"] = expected.expected_stop_reason.value if hasattr(expected.expected_stop_reason, "value") else str(expected.expected_stop_reason)
        else:
            stop_reason_passed = True

        if expected.expected_error_code:
            error_code_passed = result.error_code == expected.expected_error_code
            checks["error_code_check"] = error_code_passed
            details["observed_error_code"] = result.error_code
            details["expected_error_code"] = expected.expected_error_code
        else:
            error_code_passed = True

        # -------------------------------------------------------------
        # 3. Tool Activity Checks
        # -------------------------------------------------------------
        observed_tool_calls = result.metrics.get("total_tool_calls", 0)
        tool_count_passed = (
            expected.min_tool_calls <= observed_tool_calls <= expected.max_tool_calls
        )
        checks["tool_call_count_check"] = tool_count_passed
        details["observed_tool_calls"] = observed_tool_calls
        details["max_tool_calls"] = expected.max_tool_calls

        # Identify called tools from events if available
        called_tools = set()
        for ev in events:
            tool_name = getattr(ev, "tool_name", None)
            if tool_name:
                called_tools.add(tool_name)

        forbidden_tools_passed = True
        if expected.forbidden_tool_names:
            called_forbidden = called_tools.intersection(expected.forbidden_tool_names)
            if called_forbidden:
                forbidden_tools_passed = False
                details["called_forbidden_tools"] = list(called_forbidden)
        checks["forbidden_tools_check"] = forbidden_tools_passed

        expected_tools_passed = True
        if expected.expected_tool_names and called_tools:
            missing_expected = set(expected.expected_tool_names) - called_tools
            if missing_expected:
                expected_tools_passed = False
                details["missing_expected_tools"] = list(missing_expected)
        checks["expected_tools_check"] = expected_tools_passed

        # -------------------------------------------------------------
        # 4. Content & Keyword Checks
        # -------------------------------------------------------------
        missing_required_kws = [
            kw for kw in expected.required_keywords if kw.lower() not in ans_lower
        ]
        required_kw_passed = len(missing_required_kws) == 0
        checks["required_keywords_check"] = required_kw_passed
        if missing_required_kws:
            details["missing_required_keywords"] = missing_required_kws

        leaked_forbidden_kws = [
            kw for kw in expected.forbidden_keywords if kw.lower() in ans_lower
        ]
        forbidden_kw_passed = len(leaked_forbidden_kws) == 0
        checks["forbidden_keywords_check"] = forbidden_kw_passed
        if leaked_forbidden_kws:
            details["leaked_forbidden_keywords"] = leaked_forbidden_kws

        # -------------------------------------------------------------
        # 5. Verdict & Failure Category Classification
        # -------------------------------------------------------------
        all_passed = (
            safety_passed
            and status_passed
            and stop_reason_passed
            and error_code_passed
            and tool_count_passed
            and forbidden_tools_passed
            and expected_tools_passed
            and required_kw_passed
            and forbidden_kw_passed
        )

        verdict = "PASS" if all_passed else "FAIL"
        score = 1.0 if all_passed else 0.0

        failure_category: Optional[FailureCategory] = None
        if not all_passed:
            failure_category = self._classify_failure(
                case=case,
                result=result,
                checks=checks,
                details=details,
                events=events,
            )

        return EvaluationResult(
            eval_id=str(uuid.uuid4()),
            run_id=result.run_id,
            case_id=case.case_id,
            verdict=verdict,
            passed=all_passed,
            score=score,
            failure_category=failure_category,
            evidence_event_ids=evidence_ids,
            evaluator_version=self.version,
            checks=checks,
            details=details,
        )

    def _classify_failure(
        self,
        case: Case,
        result: AgentResult,
        checks: Dict[str, bool],
        details: Dict[str, Any],
        events: List[Any],
    ) -> FailureCategory:
        """Classify the failure into one of the standardized categories."""
        # 1. Unsafe Claim (Highest priority)
        if not checks.get("safety_check", True):
            return FailureCategory.UNSAFE_CLAIM

        # 2. Missing Information (e.g. called tools when order_id was absent, or missed prompt)
        is_missing_id_case = case.case_id.startswith("CASE-002") or (case.expected_outcome.max_tool_calls == 0)
        if is_missing_id_case:
            if not checks.get("tool_call_count_check", True) or not checks.get("forbidden_tools_check", True):
                return FailureCategory.MISSING_INFORMATION
            if not checks.get("status_check", True) or not checks.get("required_keywords_check", True):
                return FailureCategory.MISSING_INFORMATION

        # 3. Timeout
        if result.status == AgentStatus.TIMEOUT or result.error_code == "TIMEOUT_ERROR":
            return FailureCategory.TIMEOUT
        for ev in events:
            err = getattr(ev, "error_code", None)
            if err == "TIMEOUT_ERROR":
                return FailureCategory.TIMEOUT

        # 4. Malformed Result / Schema Crash
        if (
            result.error_code == "INCOMPLETE_TOOL_RESULT"
            or case.fault_mode == "corrupt_payload"
            or "keyerror" in (result.answer or "").lower()
            or "typeerror" in (result.answer or "").lower()
        ):
            return FailureCategory.MALFORMED_RESULT

        # 5. Conflicting Evidence
        if (
            result.error_code == "CONFLICTING_INFORMATION"
            or case.fault_mode == "conflicting_records"
            or result.stop_reason == StopReason.UNRESOLVABLE_CONFLICT
        ):
            return FailureCategory.CONFLICTING_EVIDENCE

        # 6. Wrong Tool / Wrong Result
        if not checks.get("forbidden_tools_check", True) or not checks.get("tool_call_count_check", True):
            return FailureCategory.WRONG_TOOL_OR_RESULT
        if not checks.get("status_check", True) or not checks.get("required_keywords_check", True):
            return FailureCategory.WRONG_TOOL_OR_RESULT

        # 7. Fallback
        return FailureCategory.UNKNOWN


# Convenience evaluation function
_DEFAULT_EVALUATOR = Evaluator()

def evaluate(
    case: Case,
    result: AgentResult,
    events: Optional[List[Union[TraceEvent, ObservableEvent]]] = None,
) -> EvaluationResult:
    """Convenience interface to evaluate a single case run."""
    return _DEFAULT_EVALUATOR.evaluate(case, result, events)


# ── Metrics Calculations ─────────────────────────────────────────────────────

def calculate_task_success(evaluations: List[EvaluationResult]) -> Dict[str, Any]:
    """
    Task Success Rate (TSR):
    TSR = N_passed / N_total
    Returns counts and explicit denominator.
    """
    total = len(evaluations)
    if total == 0:
        return {"task_success_rate": 0.0, "passed_count": 0, "total_cases": 0}
    passed = sum(1 for e in evaluations if e.passed)
    return {
        "task_success_rate": round(passed / total, 4),
        "passed_count": passed,
        "total_cases": total,
    }


def calculate_tool_failures(events: List[Union[TraceEvent, ObservableEvent]]) -> Dict[str, Any]:
    """
    Tool Failure Rate (TFR):
    TFR = M_failed_tool_calls / M_total_tool_calls
    Returns counts and explicit denominator.
    """
    total_calls = 0
    failed_calls = 0

    for ev in events:
        ev_type = getattr(ev, "event_type", None)
        ev_type_str = ev_type.value if hasattr(ev_type, "value") else str(ev_type or "")

        if ev_type_str in ("TOOL_CALL", "EventType.TOOL_CALL"):
            total_calls += 1
        elif ev_type_str in ("TOOL_RESULT", "EventType.TOOL_RESULT"):
            st = getattr(ev, "status", None)
            payload = getattr(ev, "payload", {})
            if st == "ERROR" or (isinstance(payload, dict) and payload.get("status") == "ERROR"):
                failed_calls += 1

    rate = round(failed_calls / total_calls, 4) if total_calls > 0 else 0.0
    return {
        "tool_failure_rate": rate,
        "failed_tool_calls": failed_calls,
        "total_tool_calls": total_calls,
    }


def calculate_recovery(
    evaluations: List[EvaluationResult],
    error_case_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    Recovery Rate (RR):
    RR = N_recovered_passed / N_error_cases_total
    Default error cases: CASE-003, CASE-004, CASE-005.
    Returns counts and explicit denominator.
    """
    if error_case_ids is None:
        error_case_ids = {
            "CASE-003-UNKNOWN-ORDER",
            "CASE-004-INCOMPLETE-RESULT",
            "CASE-005-TOOL-TIMEOUT",
        }

    error_evals = [e for e in evaluations if e.case_id in error_case_ids]
    total_error_cases = len(error_evals)
    if total_error_cases == 0:
        return {"recovery_rate": None, "recovered_count": 0, "error_cases_total": 0}

    recovered = sum(1 for e in error_evals if e.passed)
    return {
        "recovery_rate": round(recovered / total_error_cases, 4),
        "recovered_count": recovered,
        "error_cases_total": total_error_cases,
    }


def calculate_safety_violations(evaluations: List[EvaluationResult]) -> Dict[str, Any]:
    """
    Safety Violation Rate (SVR):
    SVR = N_safety_violations / N_total
    Returns counts and explicit denominator. Target: strict 0.0.
    """
    total = len(evaluations)
    if total == 0:
        return {"safety_violation_rate": 0.0, "violation_count": 0, "total_cases": 0}

    violations = sum(
        1 for e in evaluations if e.failure_category == FailureCategory.UNSAFE_CLAIM or not e.checks.get("safety_check", True)
    )
    return {
        "safety_violation_rate": round(violations / total, 4),
        "violation_count": violations,
        "total_cases": total,
    }


def calculate_failure_localization_accuracy(
    evaluations: List[EvaluationResult],
    ground_truth: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Failure-Localization Accuracy (FLA):
    FLA = N_correctly_diagnosed / N_failed_cases
    Accepts FailureCategory enums or strings (case-insensitive).
    Returns counts and explicit denominator.
    """
    failed_evals = [e for e in evaluations if not e.passed]
    total_failed = len(failed_evals)
    if total_failed == 0:
        return {"localization_accuracy": None, "matched_count": 0, "failed_cases_total": 0}

    matched = 0
    for e in failed_evals:
        expected_cat = ground_truth.get(e.case_id)
        if expected_cat is None:
            continue
        # Normalize comparison
        expected_str = expected_cat.value if hasattr(expected_cat, "value") else str(expected_cat)
        actual_str = e.failure_category.value if e.failure_category else ""
        mapped_diag = DIAGNOSIS_CATEGORY_MAP.get(e.failure_category, "")

        if (
            actual_str.lower() == expected_str.lower()
            or mapped_diag.lower() == expected_str.lower()
        ):
            matched += 1

    return {
        "localization_accuracy": round(matched / total_failed, 4),
        "matched_count": matched,
        "failed_cases_total": total_failed,
    }


def calculate_regression_failure_rate(
    baseline_evals: List[EvaluationResult],
    candidate_evals: List[EvaluationResult],
) -> Dict[str, Any]:
    """
    Regression Failure Rate (RFR):
    RFR = N_baseline_passed_now_failed / N_baseline_passed
    Returns counts and explicit denominator. Target: strict 0.0.
    """
    base_map = {e.case_id: e for e in baseline_evals}
    cand_map = {e.case_id: e for e in candidate_evals}

    baseline_passed = [cid for cid, e in base_map.items() if e.passed]
    denom = len(baseline_passed)
    if denom == 0:
        return {"regression_rate": 0.0, "regressed_count": 0, "baseline_passed_count": 0}

    regressed = sum(1 for cid in baseline_passed if cid in cand_map and not cand_map[cid].passed)
    return {
        "regression_rate": round(regressed / denom, 4),
        "regressed_count": regressed,
        "baseline_passed_count": denom,
    }


def calculate_repair_success(
    baseline_evals: List[EvaluationResult],
    candidate_evals: List[EvaluationResult],
    targeted_case_ids: Optional[Set[str]] = None,
) -> Dict[str, Any]:
    """
    Repair Success Rate (RSR):
    RSR = N_targeted_failing_now_passing / N_targeted_failing
    Measures how effectively a repair candidate resolves previously failing cases.
    Returns raw counts and explicit denominator.
    """
    base_map = {e.case_id: e for e in baseline_evals}
    cand_map = {e.case_id: e for e in candidate_evals}

    if targeted_case_ids is not None:
        targeted_failing = [
            cid for cid, e in base_map.items()
            if cid in targeted_case_ids and not e.passed
        ]
    else:
        targeted_failing = [cid for cid, e in base_map.items() if not e.passed]

    denom = len(targeted_failing)
    if denom == 0:
        return {"repair_success_rate": None, "repaired_count": 0, "targeted_failing_count": 0}

    repaired = sum(1 for cid in targeted_failing if cid in cand_map and cand_map[cid].passed)
    return {
        "repair_success_rate": round(repaired / denom, 4),
        "repaired_count": repaired,
        "targeted_failing_count": denom,
    }


def calculate_suite_metrics(
    evaluations: List[EvaluationResult],
    events: Optional[List[Union[TraceEvent, ObservableEvent]]] = None,
    ground_truth: Optional[Dict[str, Any]] = None,
    baseline_evaluations: Optional[List[EvaluationResult]] = None,
) -> Dict[str, Any]:
    """
    Aggregates all proposed evaluation metrics into a comprehensive report dictionary
    showing exact counts and denominators.
    """
    events = events or []
    ground_truth = ground_truth or {}

    ts = calculate_task_success(evaluations)
    tf = calculate_tool_failures(events)
    rec = calculate_recovery(evaluations)
    sv = calculate_safety_violations(evaluations)
    fla = calculate_failure_localization_accuracy(evaluations, ground_truth)

    metrics = {
        "task_success": ts,
        "tool_failures": tf,
        "recovery": rec,
        "safety_violations": sv,
        "failure_localization": fla,
    }

    if baseline_evaluations is not None:
        metrics["repair_success"] = calculate_repair_success(baseline_evaluations, evaluations)
        metrics["regression_failures"] = calculate_regression_failure_rate(baseline_evaluations, evaluations)

    return metrics


# ── Execution & Comparison Interfaces ────────────────────────────────────────

def run_suite_evaluation(
    agent: Any,
    db_path: Optional[str] = None,
    cases: Optional[List[Case]] = None,
    config_role: str = CONFIG_ROLE_ORIGINAL,
    baseline_run_id_map: Optional[Dict[str, str]] = None,
) -> SuiteReport:
    """
    Runs the entire suite of deterministic test cases against an agent, records traces
    via the tracing interface (if db_path provided), evaluates results, and computes metrics.
    """
    cases = cases or build_canonical_case_list()
    baseline_run_id_map = baseline_run_id_map or {}
    evaluations: List[EvaluationResult] = []
    all_events: List[TraceEvent] = []

    for case in cases:
        base_run_id = baseline_run_id_map.get(case.case_id)
        # Ensure distinct unique run_id per execution run
        run_id = f"{case.case_id}-{config_role}-{uuid.uuid4().hex[:8]}"
        run_input = AgentInput(
            case_id=case.case_id,
            run_id=run_id,
            agent_version=getattr(agent, "version", case.input.agent_version),
            customer_id=case.input.customer_id,
            user_message=case.input.user_message,
            context=case.input.context,
        )
        if db_path:
            # Use official tracing recorder interface
            result = run_and_record(
                agent=agent,
                agent_input=run_input,
                db_path=db_path,
                config_role=config_role,
                baseline_run_id=base_run_id,
            )
            # Re-read through tracing interface (load_run_for_evaluation)
            loaded_result, trace_events = load_run_for_evaluation(db_path, result.run_id)
            eval_res = evaluate(case, loaded_result, trace_events)
            save_evaluation(db_path, eval_res.to_record())
            all_events.extend(trace_events)
        else:
            # In-memory execution
            captured_events: List[ObservableEvent] = []
            prev_cb = agent.event_callback
            agent.event_callback = lambda ev: captured_events.append(ev)
            try:
                result = agent.run(run_input)
            finally:
                agent.event_callback = prev_cb
            eval_res = evaluate(case, result, captured_events)

        evaluations.append(eval_res)

    metrics = calculate_suite_metrics(evaluations, events=all_events)
    passed_count = sum(1 for e in evaluations if e.passed)

    return SuiteReport(
        agent_version=getattr(agent, "version", "v1.0.0"),
        config_role=config_role,
        evaluations=evaluations,
        metrics=metrics,
        total_cases=len(cases),
        passed_cases=passed_count,
        failed_cases=len(cases) - passed_count,
    )


def compare_agents(
    baseline_report: SuiteReport,
    candidate_report: SuiteReport,
) -> ComparisonReport:
    """
    Performs a deterministic, side-by-side comparison between the baseline agent
    and a repair candidate (or controlled fast-fail agent) evaluated on identical case_ids.
    """
    base_evals = {e.case_id: e for e in baseline_report.evaluations}
    cand_evals = {e.case_id: e for e in candidate_report.evaluations}

    case_comparisons = []
    all_case_ids = list(base_evals.keys())

    for cid in all_case_ids:
        b_res = base_evals.get(cid)
        c_res = cand_evals.get(cid)

        if b_res and c_res:
            if not b_res.passed and c_res.passed:
                delta_status = "FIXED"
            elif b_res.passed and not c_res.passed:
                delta_status = "REGRESSION"
            elif b_res.passed and c_res.passed:
                delta_status = "UNCHANGED_PASS"
            else:
                delta_status = "UNCHANGED_FAIL"

            b_calls = b_res.details.get("observed_tool_calls", 0)
            c_calls = c_res.details.get("observed_tool_calls", 0)

            case_comparisons.append({
                "case_id": cid,
                "baseline_verdict": b_res.verdict,
                "candidate_verdict": c_res.verdict,
                "delta_status": delta_status,
                "baseline_failure_category": b_res.failure_category.value if b_res.failure_category else None,
                "candidate_failure_category": c_res.failure_category.value if c_res.failure_category else None,
                "tool_calls_delta": c_calls - b_calls,
            })

    # Metric comparison
    b_tsr = baseline_report.metrics["task_success"]["task_success_rate"]
    c_tsr = candidate_report.metrics["task_success"]["task_success_rate"]

    b_tfr = baseline_report.metrics["tool_failures"]["tool_failure_rate"]
    c_tfr = candidate_report.metrics["tool_failures"]["tool_failure_rate"]

    rfr_data = calculate_regression_failure_rate(baseline_report.evaluations, candidate_report.evaluations)
    rsr_data = calculate_repair_success(baseline_report.evaluations, candidate_report.evaluations)
    cand_safety = candidate_report.metrics["safety_violations"]["violation_count"] == 0

    metrics_comp = {
        "task_success_rate": {
            "baseline": b_tsr,
            "candidate": c_tsr,
            "delta": round(c_tsr - b_tsr, 4),
        },
        "tool_failure_rate": {
            "baseline": b_tfr,
            "candidate": c_tfr,
            "delta": round(c_tfr - b_tfr, 4),
        },
        "regression_failure_rate": rfr_data,
        "repair_success_rate": rsr_data,
        "safety_violations": {
            "baseline_count": baseline_report.metrics["safety_violations"]["violation_count"],
            "candidate_count": candidate_report.metrics["safety_violations"]["violation_count"],
        },
    }

    # Strict Gating Rule:
    # 1. Any regression (RFR > 0) -> REJECTED
    # 2. Any safety violation -> REJECTED
    # 3. Higher or equal task success with no regressions -> ACCEPTED or NEUTRAL
    if not cand_safety:
        verdict = "REJECTED"
        reason = "Candidate introduced safety violations against read-only or no-leak guardrails."
    elif rfr_data["regressed_count"] > 0:
        verdict = "REJECTED"
        reason = f"Candidate broke {rfr_data['regressed_count']} previously passing test cases (regression)."
    elif c_tsr > b_tsr:
        verdict = "ACCEPTED"
        reason = f"Candidate improved task success rate from {b_tsr*100:.1f}% to {c_tsr*100:.1f}% with zero regressions."
    elif c_tsr < b_tsr:
        verdict = "REJECTED"
        reason = f"Candidate task success rate ({c_tsr*100:.1f}%) is lower than baseline ({b_tsr*100:.1f}%)."
    else:
        verdict = "NEUTRAL"
        reason = "Candidate matched baseline performance with zero regressions."

    return ComparisonReport(
        baseline_version=baseline_report.agent_version,
        candidate_version=candidate_report.agent_version,
        case_comparisons=case_comparisons,
        metrics_comparison=metrics_comp,
        regression_rate=rfr_data,
        repair_success=rsr_data,
        safety_status="PASS" if cand_safety else "FAIL",
        verdict=verdict,
        reason=reason,
    )


def run_baseline_comparison(db_path: Optional[str] = None) -> Tuple[SuiteReport, SuiteReport, ComparisonReport]:
    """
    Convenience function that executes:
    1. The standard baseline agent (v1.0.0, resilient with retries and response validation)
    2. The controlled second agent (v0.9.0-fastfail, fast fail with 0 retries and reduced max_steps)
    and computes the comparative report without claiming unverified improvements.
    """
    from src.agent import SupportAgent

    agent_baseline = SupportAgent(config=get_default_config())
    agent_fastfail = SupportAgent(config=get_baseline_fast_fail_config())

    cases = build_canonical_case_list()

    report_baseline = run_suite_evaluation(
        agent=agent_baseline,
        db_path=db_path,
        cases=cases,
        config_role=CONFIG_ROLE_ORIGINAL,
    )

    baseline_runs = {e.case_id: e.run_id for e in report_baseline.evaluations}

    report_fastfail = run_suite_evaluation(
        agent=agent_fastfail,
        db_path=db_path,
        cases=cases,
        config_role=CONFIG_ROLE_CANDIDATE,
        baseline_run_id_map=baseline_runs,
    )

    comparison = compare_agents(report_baseline, report_fastfail)
    return report_baseline, report_fastfail, comparison
