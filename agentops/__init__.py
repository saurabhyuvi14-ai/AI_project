"""Diagnosis and safe repair-candidate primitives for AgentOps."""

from .diagnosis import (
    CaseExpectation,
    DiagnosisEvidence,
    DiagnosisLimitExceeded,
    DiagnosisRequest,
    DiagnosisResult,
    FailureCategory,
    FailureSummary,
    RunInspection,
    TraceEvent,
    diagnose,
    get_case_expectation,
    get_failure_summary,
    get_run_trace,
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
    "DiagnosisEvidence",
    "DiagnosisLimitExceeded",
    "DiagnosisRequest",
    "DiagnosisResult",
    "FailureCategory",
    "FailureSummary",
    "RepairCandidate",
    "RunInspection",
    "TraceEvent",
    "apply_candidate_to_copy",
    "diagnose",
    "get_case_expectation",
    "get_failure_summary",
    "get_run_trace",
    "validate_candidate",
]
