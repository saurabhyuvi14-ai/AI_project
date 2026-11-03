"""Diagnosis and safe repair-candidate primitives for AgentOps."""

from .diagnosis import (
    CaseExpectation,
    DiagnosisRequest,
    DiagnosisResult,
    FailureCategory,
    FailureSummary,
    RunInspection,
    TraceEvent,
    diagnose,
)
from .repair import (
    ApprovalStatus,
    CandidateTestStatus,
    RepairCandidate,
    apply_candidate_to_copy,
    validate_candidate,
)

__all__ = [
    "ApprovalStatus",
    "CandidateTestStatus",
    "CaseExpectation",
    "DiagnosisRequest",
    "DiagnosisResult",
    "FailureCategory",
    "FailureSummary",
    "RepairCandidate",
    "RunInspection",
    "TraceEvent",
    "apply_candidate_to_copy",
    "diagnose",
    "validate_candidate",
]
