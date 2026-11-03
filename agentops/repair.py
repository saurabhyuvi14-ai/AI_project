"""Validation and isolated application of allowlisted repair candidates."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


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
    available_evidence_event_ids: set[str] | frozenset[str],
) -> CandidateValidationResult:
    """Check provenance, lifecycle, current value, and the narrow setting allowlist."""
    reasons: list[str] = []
    if not candidate.repair_id.strip():
        reasons.append("repair_id must not be empty")
    if not candidate.source_run_ids:
        reasons.append("at least one source run ID is required")
    elif len(set(candidate.source_run_ids)) != len(candidate.source_run_ids):
        reasons.append("source_run_ids cannot contain duplicates")
    elif any(run_id not in available_source_run_ids for run_id in candidate.source_run_ids):
        reasons.append("source_run_ids must refer to selected available runs")
    if not candidate.evidence_event_ids:
        reasons.append("trace evidence is required")
    elif len(set(candidate.evidence_event_ids)) != len(candidate.evidence_event_ids):
        reasons.append("evidence_event_ids cannot contain duplicates")
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
        if not isinstance(candidate.old_value, str):
            reasons.append("clarification instruction old_value must be a string")
        if candidate.new_value != _CLARIFICATION_VALUE:
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
    available_evidence_event_ids: set[str] | frozenset[str],
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
