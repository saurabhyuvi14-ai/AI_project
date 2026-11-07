import unittest
import time
from unittest.mock import patch

from agentops.diagnosis import (
    CaseExpectation,
    DiagnosisRequest,
    DiagnosisLimitExceeded,
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
        self.assertEqual(result.valid_evidence_event_ids, ("evt-17",))
        self.assertEqual(result.observed_evidence[0].run_id, "run-1")
        self.assertTrue(result.hypotheses[0].startswith("Hypothesis:"))

    def diagnose_event(self, category, event, safety_constraints=()):
        tools = FakeReadOnlyTools(
            RunInspection(
                run_id="run-1",
                case_id="case-1",
                agent_config_version="agent-v1",
                tool_config_version="tools-v1",
                started_at="",
                completed_at="",
                events=(event,),
            ),
            FailureSummary(
                run_id="run-1",
                category=category,
                failed_assertions=("Expected behavior was not met.",),
                expected="Expected behavior.",
                actual="Observed failure.",
            ),
        )
        tools.get_case_expectation = lambda case_id: CaseExpectation(
            case_id=case_id,
            required_behavior="Respond accurately.",
            assertions=("Avoid unsupported claims.",),
            safety_constraints=safety_constraints,
        )
        return diagnose(DiagnosisRequest(("run-1",), "Explain the failure."), tools)

    def test_diagnoses_missing_order_id_with_trace_evidence(self):
        event = TraceEvent(
            event_id="evt-missing",
            event_type="tool_call",
            step=1,
            tool_name="lookup_order_details",
            input={},
        )
        result = self.diagnose_event(FailureCategory.MISSING_ORDER_ID, event)
        generalized = self.diagnose_event(FailureCategory.MISSING_INFORMATION, event)

        self.assertIn("missing order ID", result.likely_cause)
        self.assertEqual(result.valid_evidence_event_ids, ("evt-missing",))
        self.assertEqual(generalized.valid_evidence_event_ids, ("evt-missing",))

    def test_diagnoses_unknown_order_with_trace_evidence(self):
        result = self.diagnose_event(
            FailureCategory.UNKNOWN_ORDER,
            TraceEvent(
                event_id="evt-unknown",
                event_type="tool_call",
                step=1,
                tool_name="lookup_order_details",
                input={"order_id": "ORD-404"},
                error="ORDER_NOT_FOUND",
            ),
        )

        self.assertIn("not found", result.likely_cause)
        self.assertEqual(result.valid_evidence_event_ids, ("evt-unknown",))

    def test_diagnoses_malformed_and_incomplete_tool_results(self):
        malformed = self.diagnose_event(
            FailureCategory.MALFORMED_RESULT,
            TraceEvent(
                event_id="evt-malformed",
                event_type="tool_result",
                step=1,
                tool_name="lookup_order_details",
                output={"order_id": "ORD-1", "status": None},
            ),
        )
        incomplete = self.diagnose_event(
            FailureCategory.INCOMPLETE_TOOL_RESULT,
            TraceEvent(
                event_id="evt-incomplete",
                event_type="tool_result",
                step=1,
                tool_name="get_order_items",
                output={"order_id": "ORD-1", "items": []},
            ),
        )

        self.assertIn("malformed", malformed.likely_cause)
        self.assertEqual(malformed.valid_evidence_event_ids, ("evt-malformed",))
        self.assertIn("incomplete", incomplete.likely_cause)
        self.assertEqual(incomplete.valid_evidence_event_ids, ("evt-incomplete",))

    def test_diagnoses_conflicting_information(self):
        second = TraceEvent(
            event_id="evt-transit",
            event_type="tool_result",
            step=2,
            tool_name="track_shipment",
            output={"shipping_status": "IN_TRANSIT"},
        )
        # Include both source events in the same selected run.
        tools = FakeReadOnlyTools(
            RunInspection("run-1", "case-1", "", "", "", "", (
                TraceEvent("evt-delivered", "tool_result", 1, output={"status": "DELIVERED"}),
                second,
            )),
            FailureSummary("run-1", FailureCategory.CONFLICTING_INFORMATION, (), "", ""),
        )
        result = diagnose(DiagnosisRequest(("run-1",), "Explain the discrepancy."), tools)

        self.assertIn("conflicting", result.likely_cause)
        self.assertEqual(
            result.valid_evidence_event_ids, ("evt-delivered", "evt-transit")
        )

    def test_diagnoses_unsafe_claim_against_recorded_safety_constraint(self):
        result = self.diagnose_event(
            FailureCategory.UNSAFE_CLAIM,
            TraceEvent(
                event_id="evt-claim",
                event_type="assistant_response",
                step=2,
                output={"response": "I have cancelled your order."},
            ),
            ("Do not cancel orders or claim that a cancellation occurred.",),
        )

        self.assertIn("safety constraint", result.likely_cause)
        self.assertEqual(result.valid_evidence_event_ids, ("evt-claim",))

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
        self.assertEqual(result.observed_evidence, ())
        self.assertEqual(result.valid_evidence_event_ids, ())

    def test_rejects_more_runs_than_diagnosis_limit(self):
        tools = FakeReadOnlyTools(None, None)
        request = DiagnosisRequest(("run-1", "run-2", "run-3", "run-4"), "Find cause.")

        with self.assertRaises(ValueError):
            diagnose(request, tools)
        self.assertEqual(tools.calls, [])

    def test_aborts_when_a_read_only_tool_exceeds_its_time_limit(self):
        tools = FakeReadOnlyTools(None, None)

        def slow_inspection(run_id):
            time.sleep(0.05)
            return None

        tools.inspect_run = slow_inspection
        with patch("agentops.diagnosis.TOOL_CALL_TIMEOUT_SECONDS", 0.01):
            with self.assertRaises(DiagnosisLimitExceeded):
                diagnose(DiagnosisRequest(("run-1",), "Find cause."), tools)
        self.assertEqual(tools.calls, [])

    def test_aborts_when_a_trace_exceeds_its_event_limit(self):
        event = TraceEvent("evt-1", "tool_call", 1)
        tools = FakeReadOnlyTools(
            RunInspection("run-1", "case-1", "", "", "", "", (event,)),
            None,
        )

        with patch("agentops.diagnosis.MAX_TRACE_EVENTS_PER_RUN", 0):
            with self.assertRaises(DiagnosisLimitExceeded):
                diagnose(DiagnosisRequest(("run-1",), "Find cause."), tools)
        self.assertEqual(tools.calls, [("inspect_run", "run-1")])


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
