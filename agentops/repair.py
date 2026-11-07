"""Validation and isolated application of allowlisted repair candidates."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from enum import StrEnum
from queue import Queue
from time import monotonic
from threading import Thread
from typing import Callable, Mapping, Protocol, TypeVar, cast


MAX_RETEST_CASES = 100
MAX_RETEST_CASE_SECONDS = 10.0
MAX_RETEST_TOTAL_SECONDS = 120.0


class CandidateTestStatus(StrEnum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    SAFETY_REGRESSION = "safety_regression"
    INCOMPLETE = "incomplete"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class RepairReviewStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class RepairCandidate:
    repair_id: str
    source_run_ids: tuple[str, ...]
    setting_path: str
    old_value: int | str
    new_value: int | str
    rationale: str
    test_status: CandidateTestStatus = CandidateTestStatus.NOT_RUN
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    evidence_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateValidationResult:
    valid: bool
    rejection_reasons: tuple[str, ...]


class CandidateValidationError(ValueError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass(frozen=True)
class EvaluationResult:
    case_id: str
    passed: bool
    safety_passed: bool
    details: tuple[str, ...] = ()


class CandidateEvaluator(Protocol):
    """Runs one case against the provided disposable configuration only."""

    def evaluate_case(
        self, settings: Mapping[str, object], case_id: str
    ) -> EvaluationResult: ...


@dataclass(frozen=True)
class RepairReview:
    candidate: RepairCandidate
    status: RepairReviewStatus
    failed_case_before: EvaluationResult
    failed_case_after: EvaluationResult | None
    regression_before: tuple[EvaluationResult, ...]
    regression_after: tuple[EvaluationResult, ...]
    rejection_reasons: tuple[str, ...] = ()


class RepairEvaluationError(RuntimeError):
    """Raised when an isolated evaluator fails to produce a valid case result."""


_RETRYABLE_READ_ONLY_TOOLS = frozenset(
    {"lookup_order_details", "get_order_items", "track_shipment"}
)
_CLARIFICATION_SETTING = "agent.clarification_instruction"
_CLARIFICATION_VALUE = (
    "Ask the user for the order ID when it is missing, before calling order lookup tools."
)


def validate_candidate(
    candidate: RepairCandidate,
    settings: Mapping[str, object],
    available_source_run_ids: set[str] | frozenset[str],
    available_evidence_event_ids: (
        set[str] | frozenset[str] | Mapping[str, str]
    ),
) -> CandidateValidationResult:
    """Check provenance, lifecycle, current value, and the narrow setting allowlist."""
    reasons: list[str] = []
    if not candidate.repair_id.strip():
        reasons.append("repair_id must not be empty")
    if not candidate.source_run_ids:
        reasons.append("at least one source run ID is required")
    elif any(not run_id.strip() for run_id in candidate.source_run_ids):
        reasons.append("source_run_ids cannot contain empty values")
    elif len(set(candidate.source_run_ids)) != len(candidate.source_run_ids):
        reasons.append("source_run_ids cannot contain duplicates")
    elif any(run_id not in available_source_run_ids for run_id in candidate.source_run_ids):
        reasons.append("source_run_ids must refer to selected available runs")
    if not candidate.evidence_event_ids:
        reasons.append("trace evidence is required")
    elif any(not event_id.strip() for event_id in candidate.evidence_event_ids):
        reasons.append("evidence_event_ids cannot contain empty values")
    elif len(set(candidate.evidence_event_ids)) != len(candidate.evidence_event_ids):
        reasons.append("evidence_event_ids cannot contain duplicates")
    elif isinstance(available_evidence_event_ids, Mapping):
        if any(
            event_id not in available_evidence_event_ids
            or available_evidence_event_ids[event_id] not in candidate.source_run_ids
            for event_id in candidate.evidence_event_ids
        ):
            reasons.append(
                "evidence_event_ids must refer to trace events in a source run"
            )
    elif any(event_id not in available_evidence_event_ids for event_id in candidate.evidence_event_ids):
        reasons.append("evidence_event_ids must refer to available trace events")
    if not candidate.rationale.strip():
        reasons.append("rationale must not be empty")
    if candidate.test_status is not CandidateTestStatus.NOT_RUN:
        reasons.append("candidate must be untested before it is applied to a copy")
    if candidate.approval_status is not ApprovalStatus.PENDING:
        reasons.append("candidate must remain pending human approval")

    setting_path = candidate.setting_path
    allowed_retry_tool = _retry_tool_for_path(setting_path)
    if setting_path == _CLARIFICATION_SETTING:
        if not isinstance(candidate.old_value, str) or not isinstance(candidate.new_value, str):
            reasons.append("clarification instruction values must be strings")
        elif candidate.new_value != _CLARIFICATION_VALUE:
            reasons.append("unsupported clarification instruction change")
    elif allowed_retry_tool is not None:
        if (
            isinstance(candidate.old_value, bool)
            or not isinstance(candidate.old_value, int)
            or isinstance(candidate.new_value, bool)
            or not isinstance(candidate.new_value, int)
        ):
            reasons.append("retry_count values must be integers")
        elif (candidate.old_value, candidate.new_value) not in {(0, 1), (1, 2)}:
            reasons.append("retry_count may only increase from 0 to 1 or from 1 to 2")
    else:
        reasons.append(f"unsupported setting path: {setting_path!r}")

    try:
        current_value = _get_path(settings, setting_path)
    except (KeyError, TypeError):
        reasons.append("setting_path does not exist in the supplied configuration")
    else:
        if setting_path == _CLARIFICATION_SETTING and not isinstance(current_value, str):
            reasons.append("configured clarification instruction must be a string")
        if allowed_retry_tool is not None and (
            isinstance(current_value, bool) or not isinstance(current_value, int)
        ):
            reasons.append("configured retry_count must be an integer")
        if current_value != candidate.old_value:
            reasons.append("old_value does not match the supplied configuration")

    return CandidateValidationResult(valid=not reasons, rejection_reasons=tuple(reasons))


def apply_candidate_to_copy(
    candidate: RepairCandidate,
    original_settings: Mapping[str, object],
    available_source_run_ids: set[str] | frozenset[str],
    available_evidence_event_ids: (
        set[str] | frozenset[str] | Mapping[str, str]
    ),
) -> dict[str, object]:
    """Apply a valid candidate to a deep copy; the supplied settings are never mutated."""
    validation = validate_candidate(
        candidate,
        original_settings,
        available_source_run_ids,
        available_evidence_event_ids,
    )
    if not validation.valid:
        raise CandidateValidationError(validation.rejection_reasons)

    copied_settings = deepcopy(dict(original_settings))
    _set_path(copied_settings, candidate.setting_path, candidate.new_value)
    return copied_settings


_T = TypeVar("_T")


def _bounded_evaluation(
    operation: Callable[[], _T], timeout: float
) -> _T:
    result: Queue[tuple[bool, object]] = Queue(maxsize=1)

    def run() -> None:
        try:
            result.put((True, operation()))
        except Exception as error:
            result.put(
                (
                    False,
                    RepairEvaluationError(
                        f"Isolated evaluation raised {type(error).__name__}: {error}"
                    ),
                )
            )

    worker = Thread(target=run, name="agentops-retest", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise TimeoutError(f"Retest exceeded its {timeout:g}-second limit.")
    succeeded, value = result.get_nowait()
    if not succeeded:
        raise cast(RepairEvaluationError, value)
    return cast(_T, value)


def review_candidate(
    candidate: RepairCandidate,
    original_settings: Mapping[str, object],
    available_source_run_ids: set[str] | frozenset[str],
    available_evidence_event_ids: Mapping[str, str],
    failed_case_before: EvaluationResult,
    regression_before: tuple[EvaluationResult, ...],
    evaluator: CandidateEvaluator,
) -> RepairReview:
    """Retest once per case on an isolated copy and return a human review decision."""
    validation = validate_candidate(
        candidate,
        original_settings,
        available_source_run_ids,
        available_evidence_event_ids,
    )
    if not validation.valid:
        rejected = replace(candidate, approval_status=ApprovalStatus.REJECTED)
        return RepairReview(
            candidate=rejected,
            status=RepairReviewStatus.REJECTED,
            failed_case_before=failed_case_before,
            failed_case_after=None,
            regression_before=regression_before,
            regression_after=(),
            rejection_reasons=validation.rejection_reasons,
        )

    if not _valid_evaluation_result(failed_case_before):
        return _incomplete_review(
            candidate,
            failed_case_before,
            regression_before,
            "The failed-case baseline result is invalid.",
        )
    before_by_case: dict[str, EvaluationResult] = {
        failed_case_before.case_id: failed_case_before
    }
    for result in regression_before:
        if not _valid_evaluation_result(result):
            return _incomplete_review(
                candidate,
                failed_case_before,
                regression_before,
                "Regression baseline contains an invalid evaluation result.",
            )
        if result.case_id in before_by_case:
            if (
                result.case_id == failed_case_before.case_id
                and result == failed_case_before
            ):
                continue
            return _incomplete_review(
                candidate,
                failed_case_before,
                regression_before,
                f"Duplicate baseline evaluation for case {result.case_id!r}.",
            )
        before_by_case[result.case_id] = result
    if failed_case_before.passed:
        return _incomplete_review(
            candidate,
            failed_case_before,
            regression_before,
            "The selected source case must be a failed baseline case.",
        )
    if not regression_before:
        return _incomplete_review(
            candidate,
            failed_case_before,
            regression_before,
            "A baseline regression suite is required before review.",
        )
    if len(before_by_case) > MAX_RETEST_CASES:
        return _incomplete_review(
            candidate,
            failed_case_before,
            regression_before,
            f"Retest suite exceeds the {MAX_RETEST_CASES}-case limit.",
        )

    try:
        copied_settings = apply_candidate_to_copy(
            candidate,
            original_settings,
            available_source_run_ids,
            available_evidence_event_ids,
        )
    except CandidateValidationError as error:
        rejected = replace(candidate, approval_status=ApprovalStatus.REJECTED)
        return RepairReview(
            candidate=rejected,
            status=RepairReviewStatus.REJECTED,
            failed_case_before=failed_case_before,
            failed_case_after=None,
            regression_before=regression_before,
            regression_after=(),
            rejection_reasons=error.reasons,
        )

    deadline = monotonic() + MAX_RETEST_TOTAL_SECONDS
    after_by_case: dict[str, EvaluationResult] = {}
    try:
        for case_id in before_by_case:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Retest exceeded its total wall-clock limit.")
            result = _bounded_evaluation(
                lambda case_id=case_id: evaluator.evaluate_case(
                    deepcopy(copied_settings), case_id
                ),
                min(MAX_RETEST_CASE_SECONDS, remaining),
            )
            if not _valid_evaluation_result(result) or result.case_id != case_id:
                raise RepairEvaluationError(
                    f"Evaluator returned an invalid result for case {case_id!r}."
                )
            after_by_case[case_id] = result
    except (TimeoutError, RepairEvaluationError) as error:
        return _incomplete_review(
            candidate,
            failed_case_before,
            regression_before,
            str(error),
            tuple(
                after_by_case[result.case_id]
                for result in regression_before
                if result.case_id in after_by_case
            ),
            failed_case_after=after_by_case.get(failed_case_before.case_id),
        )

    failed_case_after = after_by_case[failed_case_before.case_id]
    regression_after = tuple(
        after_by_case[result.case_id] for result in regression_before
    )
    rejection_reasons: list[str] = []
    safety_failed = not failed_case_after.safety_passed or any(
        not result.safety_passed for result in regression_after
    )
    case_failed = not failed_case_after.passed
    if safety_failed:
        rejection_reasons.append("Candidate evaluation failed a safety assertion.")
    if case_failed:
        rejection_reasons.append("The previously failed case still fails.")
    regression_failed = False
    for before in regression_before:
        after = after_by_case[before.case_id]
        if before.passed and not after.passed:
            regression_failed = True
            rejection_reasons.append(
                f"Previously passing regression case {before.case_id!r} now fails."
            )
    test_status = (
        CandidateTestStatus.SAFETY_REGRESSION
        if safety_failed
        else CandidateTestStatus.FAILED
        if case_failed or regression_failed
        else CandidateTestStatus.PASSED
    )

    if rejection_reasons:
        reviewed_candidate = replace(
            candidate,
            test_status=test_status,
            approval_status=ApprovalStatus.REJECTED,
        )
        status = RepairReviewStatus.REJECTED
    else:
        reviewed_candidate = replace(
            candidate,
            test_status=CandidateTestStatus.PASSED,
            approval_status=ApprovalStatus.PENDING,
        )
        status = RepairReviewStatus.PENDING_APPROVAL
    return RepairReview(
        candidate=reviewed_candidate,
        status=status,
        failed_case_before=failed_case_before,
        failed_case_after=failed_case_after,
        regression_before=regression_before,
        regression_after=regression_after,
        rejection_reasons=tuple(rejection_reasons),
    )


def decide_repair_review(
    review: RepairReview, *, approved: bool
) -> RepairReview:
    """Record a human decision; approval never applies settings to the original."""
    if (
        review.status is not RepairReviewStatus.PENDING_APPROVAL
        or review.candidate.test_status is not CandidateTestStatus.PASSED
        or review.candidate.approval_status is not ApprovalStatus.PENDING
    ):
        raise ValueError("Only a passing candidate pending approval can be decided.")
    approval_status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    review_status = (
        RepairReviewStatus.APPROVED if approved else RepairReviewStatus.REJECTED
    )
    reasons = review.rejection_reasons
    if not approved:
        reasons = ("A human reviewer rejected the candidate.",)
    return replace(
        review,
        candidate=replace(review.candidate, approval_status=approval_status),
        status=review_status,
        rejection_reasons=reasons,
    )


def _incomplete_review(
    candidate: RepairCandidate,
    failed_case_before: EvaluationResult,
    regression_before: tuple[EvaluationResult, ...],
    reason: str,
    regression_after: tuple[EvaluationResult, ...] = (),
    failed_case_after: EvaluationResult | None = None,
) -> RepairReview:
    return RepairReview(
        candidate=replace(
            candidate,
            test_status=CandidateTestStatus.INCOMPLETE,
            approval_status=ApprovalStatus.REJECTED,
        ),
        status=RepairReviewStatus.REJECTED,
        failed_case_before=failed_case_before,
        failed_case_after=failed_case_after,
        regression_before=regression_before,
        regression_after=regression_after,
        rejection_reasons=(reason,),
    )


def _valid_evaluation_result(result: EvaluationResult) -> bool:
    return (
        isinstance(result, EvaluationResult)
        and isinstance(result.case_id, str)
        and bool(result.case_id.strip())
        and isinstance(result.passed, bool)
        and isinstance(result.safety_passed, bool)
    )


def _retry_tool_for_path(setting_path: str) -> str | None:
    parts = setting_path.split(".")
    if (
        len(parts) == 3
        and parts[0] == "tools"
        and parts[2] == "retry_count"
        and parts[1] in _RETRYABLE_READ_ONLY_TOOLS
    ):
        return parts[1]
    return None


def _get_path(settings: Mapping[str, object], setting_path: str) -> object:
    value: object = settings
    for part in setting_path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            raise KeyError(setting_path)
        value = value[part]
    return value


def _set_path(settings: dict[str, object], setting_path: str, new_value: int | str) -> None:
    parts = setting_path.split(".")
    value = settings
    for part in parts[:-1]:
        child = value.get(part)
        if not isinstance(child, dict):
            raise CandidateValidationError(
                (f"setting_path cannot be applied to a copied configuration: {setting_path!r}",)
            )
        value = child
    value[parts[-1]] = new_value
