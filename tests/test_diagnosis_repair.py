import unittest

from agentops.diagnosis import (
    CaseExpectation,
    DiagnosisRequest,
    FailureCategory,
    FailureSummary,
    RunInspection,
    TraceEvent,
    diagnose,
)
from agentops.repair import (
    ApprovalStatus,
    CandidateTestStatus,
    CandidateValidationError,
    RepairCandidate,
    apply_candidate_to_copy,
    validate_candidate,
)


class FakeReadOnlyTools:
    def __init__(self, inspection, summary):
        self.inspection = inspection
        self.summary = summary
        self.calls = []

    def inspect_run(self, run_id):
        self.calls.append(("inspect_run", run_id))
        return self.inspection

    def get_case_expectation(self, case_id):
        self.calls.append(("get_case_expectation", case_id))
        return CaseExpectation(
            case_id=case_id,
            required_behavior="Report an order lookup failure accurately.",
            assertions=("Do not claim a successful lookup.",),
            safety_constraints=("Do not mutate order data.",),
        )

    def get_failure_summary(self, run_id):
        self.calls.append(("get_failure_summary", run_id))
        return self.summary


class DiagnosisTests(unittest.TestCase):
    def test_timeout_diagnosis_cites_only_real_trace_event_ids(self):
        event = TraceEvent(
            event_id="evt-17",
            event_type="tool_call",
            step=1,
            tool_name="lookup_order_details",
            input={"order_id": "ORD-12345"},
            error="TIMEOUT_ERROR",
        )
        tools = FakeReadOnlyTools(
            RunInspection(
                run_id="run-1",
                case_id="case-1",
                agent_config_version="agent-v1",
                tool_config_version="tools-v1",
                started_at="2026-01-01T00:00:00Z",
                completed_at="2026-01-01T00:00:01Z",
                events=(event,),
            ),
            FailureSummary(
                run_id="run-1",
                category=FailureCategory.TOOL_TIMEOUT,
                failed_assertions=("The request did not complete.",),
                expected="A completed lookup.",
                actual="Timeout.",
            ),
        )

        result = diagnose(
            DiagnosisRequest(run_ids=("run-1",), goal="Explain the timeout."),
            tools,
        )

        self.assertIn("timeout", result.likely_cause.lower())
        self.assertEqual(result.cited_evidence_event_ids, ("evt-17",))
        self.assertTrue(any("evt-17" in fact for fact in result.observed_facts))
        self.assertEqual(len(tools.calls), 3)

    def test_abstains_when_failure_category_has_no_trace_support(self):
        tools = FakeReadOnlyTools(
            RunInspection(
                run_id="run-1",
                case_id="case-1",
                agent_config_version="agent-v1",
                tool_config_version="tools-v1",
                started_at="",
                completed_at="",
                events=(),
            ),
            FailureSummary(
                run_id="run-1",
                category=FailureCategory.TOOL_TIMEOUT,
                failed_assertions=("Timed out.",),
                expected="Success.",
                actual="Failure.",
            ),
        )

        result = diagnose(DiagnosisRequest(("run-1",), "Find the cause."), tools)

        self.assertIn("Insufficient trace evidence", result.likely_cause)
        self.assertEqual(result.cited_evidence_event_ids, ())

    def test_rejects_more_runs_than_diagnosis_limit(self):
        tools = FakeReadOnlyTools(None, None)
        request = DiagnosisRequest(("run-1", "run-2", "run-3", "run-4"), "Find cause.")

        with self.assertRaises(ValueError):
            diagnose(request, tools)
        self.assertEqual(tools.calls, [])


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "tools": {
                "lookup_order_details": {"retry_count": 0},
            },
            "agent": {
                "clarification_instruction": "Ask a question when details are missing."
            },
        }
        self.available_runs = {"run-1"}
        self.available_events = {"evt-17"}

    def candidate(self, **overrides):
        values = {
            "repair_id": "repair-1",
            "source_run_ids": ("run-1",),
            "setting_path": "tools.lookup_order_details.retry_count",
            "old_value": 0,
            "new_value": 1,
            "rationale": "The read-only lookup timed out once.",
            "evidence_event_ids": ("evt-17",),
        }
        values.update(overrides)
        return RepairCandidate(**values)

    def test_valid_retry_candidate_applies_to_a_copy_only(self):
        candidate = self.candidate()

        result = validate_candidate(
            candidate, self.settings, self.available_runs, self.available_events
        )
        copied_settings = apply_candidate_to_copy(
            candidate, self.settings, self.available_runs, self.available_events
        )

        self.assertTrue(result.valid)
        self.assertEqual(copied_settings["tools"]["lookup_order_details"]["retry_count"], 1)
        self.assertEqual(self.settings["tools"]["lookup_order_details"]["retry_count"], 0)
        self.assertIsNot(copied_settings, self.settings)
        self.assertIsNot(copied_settings["tools"], self.settings["tools"])

    def test_rejects_retry_outside_bounded_transitions(self):
        candidate = self.candidate(new_value=3)

        result = validate_candidate(
            candidate, self.settings, self.available_runs, self.available_events
        )

        self.assertFalse(result.valid)
        self.assertTrue(any("only increase" in reason for reason in result.rejection_reasons))
        with self.assertRaises(CandidateValidationError):
            apply_candidate_to_copy(
                candidate, self.settings, self.available_runs, self.available_events
            )

    def test_accepts_second_bounded_retry_transition(self):
        self.settings["tools"]["lookup_order_details"]["retry_count"] = 1
        candidate = self.candidate(old_value=1, new_value=2)

        result = validate_candidate(
            candidate, self.settings, self.available_runs, self.available_events
        )

        self.assertTrue(result.valid)

    def test_rejects_retry_for_unallowlisted_tool(self):
        candidate = self.candidate(
            setting_path="tools.cancel_order.retry_count",
            old_value=0,
            new_value=1,
        )

        result = validate_candidate(
            candidate, self.settings, self.available_runs, self.available_events
        )

        self.assertFalse(result.valid)
        self.assertTrue(any("unsupported setting path" in reason for reason in result.rejection_reasons))

    def test_rejects_boolean_retry_setting(self):
        self.settings["tools"]["lookup_order_details"]["retry_count"] = False
        candidate = self.candidate(old_value=0)

        result = validate_candidate(
            candidate, self.settings, self.available_runs, self.available_events
        )

        self.assertFalse(result.valid)
        self.assertTrue(any("must be an integer" in reason for reason in result.rejection_reasons))

    def test_rejects_unsupported_setting_path(self):
        candidate = self.candidate(
            setting_path="agent.system_prompt",
            old_value="old",
            new_value="new",
        )

        result = validate_candidate(
            candidate, self.settings, self.available_runs, self.available_events
        )

        self.assertFalse(result.valid)
        self.assertTrue(any("unsupported setting path" in reason for reason in result.rejection_reasons))

    def test_accepts_only_the_allowlisted_clarification_value(self):
        candidate = self.candidate(
            setting_path="agent.clarification_instruction",
            old_value="Ask a question when details are missing.",
            new_value=(
                "Ask the user for the order ID when it is missing, "
                "before calling order lookup tools."
            ),
        )

        result = validate_candidate(
            candidate, self.settings, self.available_runs, self.available_events
        )
        copied = apply_candidate_to_copy(
            candidate, self.settings, self.available_runs, self.available_events
        )

        self.assertTrue(result.valid)
        self.assertEqual(
            copied["agent"]["clarification_instruction"], candidate.new_value
        )

    def test_rejects_missing_or_fabricated_trace_evidence(self):
        candidate = self.candidate(evidence_event_ids=("evt-fabricated",))

        result = validate_candidate(
            candidate, self.settings, self.available_runs, self.available_events
        )

        self.assertFalse(result.valid)
        self.assertTrue(any("evidence_event_ids" in reason for reason in result.rejection_reasons))

    def test_rejects_non_pending_or_already_tested_candidate(self):
        candidate = self.candidate(
            test_status=CandidateTestStatus.SAFETY_REGRESSION,
            approval_status=ApprovalStatus.APPROVED,
        )

        result = validate_candidate(
            candidate, self.settings, self.available_runs, self.available_events
        )

        self.assertFalse(result.valid)
        self.assertTrue(any("untested" in reason for reason in result.rejection_reasons))
        self.assertTrue(any("pending human approval" in reason for reason in result.rejection_reasons))


if __name__ == "__main__":
    unittest.main()
