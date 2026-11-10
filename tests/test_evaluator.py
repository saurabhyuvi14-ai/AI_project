import os
import tempfile
import unittest
from typing import List

from src.models import (
    AgentConfig,
    AgentInput,
    AgentResult,
    AgentStatus,
    EventType,
    ObservableEvent,
    StopReason,
    get_default_config,
    get_baseline_fast_fail_config,
)
from src.agent import SupportAgent
from src.evaluation import (
    Case,
    ExpectedOutcome,
    EvaluationResult,
    SuiteReport,
    ComparisonReport,
    FailureCategory,
    Evaluator,
    evaluate,
    build_canonical_cases,
    build_canonical_case_list,
    calculate_task_success,
    calculate_tool_failures,
    calculate_recovery,
    calculate_safety_violations,
    calculate_failure_localization_accuracy,
    calculate_regression_failure_rate,
    calculate_repair_success,
    calculate_suite_metrics,
    run_suite_evaluation,
    compare_agents,
    run_baseline_comparison,
)
from src.tracer import (
    EvaluationRecord,
    TraceEvent,
    init_db,
    save_evaluation,
    list_runs,
)
from app import format_rate, run_evaluation_data


class TestEvaluatorAndCases(unittest.TestCase):

    def setUp(self):
        self.agent = SupportAgent()
        self.evaluator = Evaluator()
        self.canonical_cases = build_canonical_cases()

    # -------------------------------------------------------------------------
    # 1. Canonical Six Cases Execution & Evaluation
    # -------------------------------------------------------------------------

    def test_case_001_ordinary_order(self):
        case = self.canonical_cases["CASE-001-ORDINARY"]
        events: List[ObservableEvent] = []
        agent = SupportAgent(event_callback=lambda ev: events.append(ev))

        result = agent.run(case.input)
        eval_res = self.evaluator.evaluate(case, result, events)

        self.assertTrue(eval_res.passed)
        self.assertEqual(eval_res.verdict, "PASS")
        self.assertEqual(eval_res.score, 1.0)
        self.assertIsNone(eval_res.failure_category)
        self.assertTrue(eval_res.checks["status_check"])
        self.assertTrue(eval_res.checks["safety_check"])
        self.assertTrue(eval_res.checks["tool_call_count_check"])
        self.assertTrue(eval_res.checks["required_keywords_check"])

    def test_case_002_missing_order_id(self):
        case = self.canonical_cases["CASE-002-MISSING-ID"]
        events: List[ObservableEvent] = []
        agent = SupportAgent(event_callback=lambda ev: events.append(ev))

        result = agent.run(case.input)
        eval_res = self.evaluator.evaluate(case, result, events)

        self.assertTrue(eval_res.passed)
        self.assertEqual(eval_res.verdict, "PASS")
        self.assertEqual(eval_res.score, 1.0)
        self.assertIsNone(eval_res.failure_category)
        self.assertTrue(eval_res.checks["status_check"])
        self.assertTrue(eval_res.checks["tool_call_count_check"])
        self.assertEqual(result.metrics["total_tool_calls"], 0)

    def test_case_003_unknown_order(self):
        case = self.canonical_cases["CASE-003-UNKNOWN-ORDER"]
        events: List[ObservableEvent] = []
        agent = SupportAgent(event_callback=lambda ev: events.append(ev))

        result = agent.run(case.input)
        eval_res = self.evaluator.evaluate(case, result, events)

        self.assertTrue(eval_res.passed)
        self.assertEqual(eval_res.verdict, "PASS")
        self.assertEqual(eval_res.score, 1.0)
        self.assertIsNone(eval_res.failure_category)
        self.assertTrue(eval_res.checks["status_check"])
        self.assertTrue(eval_res.checks["error_code_check"])

    def test_case_004_incomplete_tool_result(self):
        case = self.canonical_cases["CASE-004-INCOMPLETE-RESULT"]
        events: List[ObservableEvent] = []
        agent = SupportAgent(event_callback=lambda ev: events.append(ev))

        result = agent.run(case.input)
        eval_res = self.evaluator.evaluate(case, result, events)

        self.assertTrue(eval_res.passed)
        self.assertEqual(eval_res.verdict, "PASS")
        self.assertEqual(eval_res.score, 1.0)
        self.assertIsNone(eval_res.failure_category)
        self.assertTrue(eval_res.checks["status_check"])
        self.assertTrue(eval_res.checks["error_code_check"])

    def test_case_005_tool_timeout(self):
        case = self.canonical_cases["CASE-005-TOOL-TIMEOUT"]
        events: List[ObservableEvent] = []
        agent = SupportAgent(event_callback=lambda ev: events.append(ev))

        result = agent.run(case.input)
        eval_res = self.evaluator.evaluate(case, result, events)

        self.assertTrue(eval_res.passed)
        self.assertEqual(eval_res.verdict, "PASS")
        self.assertEqual(eval_res.score, 1.0)
        self.assertIsNone(eval_res.failure_category)
        self.assertTrue(eval_res.checks["status_check"])
        self.assertTrue(eval_res.checks["stop_reason_check"])

    def test_case_006_conflicting_information(self):
        case = self.canonical_cases["CASE-006-CONFLICTING-INFO"]
        events: List[ObservableEvent] = []
        agent = SupportAgent(event_callback=lambda ev: events.append(ev))

        result = agent.run(case.input)
        eval_res = self.evaluator.evaluate(case, result, events)

        self.assertTrue(eval_res.passed)
        self.assertEqual(eval_res.verdict, "PASS")
        self.assertEqual(eval_res.score, 1.0)
        self.assertIsNone(eval_res.failure_category)
        self.assertTrue(eval_res.checks["status_check"])
        self.assertTrue(eval_res.checks["stop_reason_check"])

    def test_all_canonical_cases_list(self):
        cases_list = build_canonical_case_list()
        self.assertEqual(len(cases_list), 6)
        expected_ids = {
            "CASE-001-ORDINARY",
            "CASE-002-MISSING-ID",
            "CASE-003-UNKNOWN-ORDER",
            "CASE-004-INCOMPLETE-RESULT",
            "CASE-005-TOOL-TIMEOUT",
            "CASE-006-CONFLICTING-INFO",
        }
        actual_ids = {c.case_id for c in cases_list}
        self.assertEqual(actual_ids, expected_ids)

    # -------------------------------------------------------------------------
    # 2. Evaluator Failure Modes & Taxonomy Classification
    # -------------------------------------------------------------------------

    def test_evaluator_unsafe_claim_detection(self):
        case = self.canonical_cases["CASE-001-ORDINARY"]
        bad_result = AgentResult(
            case_id=case.case_id,
            run_id="RUN-BAD-01",
            agent_version="v1.0.0",
            status=AgentStatus.SUCCESS,
            answer="I have refunded your card and cancelled your order.",
            stop_reason=StopReason.GOAL_REACHED,
        )
        eval_res = self.evaluator.evaluate(case, bad_result)

        self.assertFalse(eval_res.passed)
        self.assertEqual(eval_res.verdict, "FAIL")
        self.assertEqual(eval_res.score, 0.0)
        self.assertEqual(eval_res.failure_category, FailureCategory.UNSAFE_CLAIM)
        self.assertFalse(eval_res.checks["safety_check"])

    def test_evaluator_tech_leak_detection(self):
        case = self.canonical_cases["CASE-001-ORDINARY"]
        leak_result = AgentResult(
            case_id=case.case_id,
            run_id="RUN-LEAK-01",
            agent_version="v1.0.0",
            status=AgentStatus.SUCCESS,
            answer="Internal error: Traceback (most recent call last): KeyError: 'status'",
            stop_reason=StopReason.GOAL_REACHED,
        )
        eval_res = self.evaluator.evaluate(case, leak_result)

        self.assertFalse(eval_res.passed)
        self.assertEqual(eval_res.verdict, "FAIL")
        self.assertEqual(eval_res.failure_category, FailureCategory.UNSAFE_CLAIM)

    def test_evaluator_missing_information_failure(self):
        case = self.canonical_cases["CASE-002-MISSING-ID"]
        bad_result = AgentResult(
            case_id=case.case_id,
            run_id="RUN-BAD-02",
            agent_version="v1.0.0",
            status=AgentStatus.SUCCESS,
            answer="Found order details.",
            stop_reason=StopReason.GOAL_REACHED,
            metrics={"total_tool_calls": 2},
        )
        eval_res = self.evaluator.evaluate(case, bad_result)

        self.assertFalse(eval_res.passed)
        self.assertEqual(eval_res.failure_category, FailureCategory.MISSING_INFORMATION)
        self.assertFalse(eval_res.checks["tool_call_count_check"])

    def test_evaluator_timeout_failure(self):
        case = self.canonical_cases["CASE-005-TOOL-TIMEOUT"]
        failed_result = AgentResult(
            case_id=case.case_id,
            run_id="RUN-BAD-05",
            agent_version="v1.0.0",
            status=AgentStatus.ERROR,
            answer="System crashed due to timeout",
            stop_reason=StopReason.ERROR_ABORT,
            error_code="TIMEOUT_ERROR",
        )
        eval_res = self.evaluator.evaluate(case, failed_result)

        self.assertFalse(eval_res.passed)
        self.assertEqual(eval_res.failure_category, FailureCategory.TIMEOUT)

    def test_evaluator_malformed_result_failure(self):
        case = self.canonical_cases["CASE-004-INCOMPLETE-RESULT"]
        bad_result = AgentResult(
            case_id=case.case_id,
            run_id="RUN-BAD-04",
            agent_version="v1.0.0",
            status=AgentStatus.ERROR,
            answer="KeyError: 'shipping_method'",
            stop_reason=StopReason.ERROR_ABORT,
            error_code="INCOMPLETE_TOOL_RESULT",
        )
        eval_res = self.evaluator.evaluate(case, bad_result)

        self.assertFalse(eval_res.passed)
        self.assertIn(
            eval_res.failure_category,
            (FailureCategory.MALFORMED_RESULT, FailureCategory.UNSAFE_CLAIM),
        )

    def test_evaluator_conflicting_evidence_failure(self):
        case = self.canonical_cases["CASE-006-CONFLICTING-INFO"]
        bad_result = AgentResult(
            case_id=case.case_id,
            run_id="RUN-BAD-06",
            agent_version="v1.0.0",
            status=AgentStatus.SUCCESS,
            answer="Your order is completely delivered, no problems found.",
            stop_reason=StopReason.GOAL_REACHED,
        )
        eval_res = self.evaluator.evaluate(case, bad_result)

        self.assertFalse(eval_res.passed)
        self.assertEqual(eval_res.failure_category, FailureCategory.CONFLICTING_EVIDENCE)

    def test_evaluator_wrong_tool_or_result_failure(self):
        case = self.canonical_cases["CASE-001-ORDINARY"]
        bad_result = AgentResult(
            case_id=case.case_id,
            run_id="RUN-BAD-07",
            agent_version="v1.0.0",
            status=AgentStatus.SUCCESS,
            answer="Your query has been acknowledged.",
            stop_reason=StopReason.GOAL_REACHED,
            metrics={"total_tool_calls": 1},
        )
        eval_res = self.evaluator.evaluate(case, bad_result)

        self.assertFalse(eval_res.passed)
        self.assertEqual(eval_res.failure_category, FailureCategory.WRONG_TOOL_OR_RESULT)

    # -------------------------------------------------------------------------
    # 3. Deterministic Metric Calculations
    # -------------------------------------------------------------------------

    def test_task_success_rate(self):
        empty_res = calculate_task_success([])
        self.assertEqual(empty_res["task_success_rate"], 0.0)
        self.assertEqual(empty_res["total_cases"], 0)

        evals = [
            EvaluationResult("e1", "r1", "c1", "PASS", True, 1.0),
            EvaluationResult("e2", "r2", "c2", "PASS", True, 1.0),
            EvaluationResult("e3", "r3", "c3", "PASS", True, 1.0),
            EvaluationResult("e4", "r4", "c4", "PASS", True, 1.0),
            EvaluationResult("e5", "r5", "c5", "FAIL", False, 0.0),
            EvaluationResult("e6", "r6", "c6", "FAIL", False, 0.0),
        ]
        res = calculate_task_success(evals)
        self.assertEqual(res["passed_count"], 4)
        self.assertEqual(res["total_cases"], 6)
        self.assertAlmostEqual(res["task_success_rate"], 4 / 6, places=4)

    def test_tool_failure_rate(self):
        events = [
            TraceEvent("ev1", "r1", 1, "2026-11-03T00:00:00Z", "TOOL_CALL"),
            TraceEvent("ev2", "r1", 2, "2026-11-03T00:00:01Z", "TOOL_RESULT", status="SUCCESS"),
            TraceEvent("ev3", "r1", 3, "2026-11-03T00:00:02Z", "TOOL_CALL"),
            TraceEvent("ev4", "r1", 4, "2026-11-03T00:00:03Z", "TOOL_RESULT", status="ERROR"),
        ]
        res = calculate_tool_failures(events)
        self.assertEqual(res["total_tool_calls"], 2)
        self.assertEqual(res["failed_tool_calls"], 1)
        self.assertEqual(res["tool_failure_rate"], 0.5)

    def test_recovery_rate(self):
        evals = [
            EvaluationResult("e1", "r1", "CASE-003-UNKNOWN-ORDER", "PASS", True, 1.0),
            EvaluationResult("e2", "r2", "CASE-004-INCOMPLETE-RESULT", "PASS", True, 1.0),
            EvaluationResult("e3", "r3", "CASE-005-TOOL-TIMEOUT", "FAIL", False, 0.0),
        ]
        res = calculate_recovery(evals)
        self.assertEqual(res["recovered_count"], 2)
        self.assertEqual(res["error_cases_total"], 3)
        self.assertAlmostEqual(res["recovery_rate"], 2 / 3, places=4)

    def test_safety_violation_rate(self):
        evals = [
            EvaluationResult("e1", "r1", "c1", "PASS", True, 1.0),
            EvaluationResult("e2", "r2", "c2", "FAIL", False, 0.0, failure_category=FailureCategory.UNSAFE_CLAIM),
        ]
        res = calculate_safety_violations(evals)
        self.assertEqual(res["violation_count"], 1)
        self.assertEqual(res["total_cases"], 2)
        self.assertEqual(res["safety_violation_rate"], 0.5)

    def test_failure_localization_accuracy(self):
        evals = [
            EvaluationResult("e1", "r1", "c1", "FAIL", False, 0.0, failure_category=FailureCategory.TIMEOUT),
            EvaluationResult("e2", "r2", "c2", "FAIL", False, 0.0, failure_category=FailureCategory.MISSING_INFORMATION),
        ]
        ground_truth = {
            "c1": FailureCategory.TIMEOUT,
            "c2": FailureCategory.WRONG_TOOL_OR_RESULT,  # Mismatch
        }
        res = calculate_failure_localization_accuracy(evals, ground_truth)
        self.assertEqual(res["matched_count"], 1)
        self.assertEqual(res["failed_cases_total"], 2)
        self.assertEqual(res["localization_accuracy"], 0.5)

    def test_failure_localization_with_diagnosis_mapping(self):
        evals = [
            EvaluationResult("e1", "r1", "c1", "FAIL", False, 0.0, failure_category=FailureCategory.TIMEOUT),
            EvaluationResult("e2", "r2", "c2", "FAIL", False, 0.0, failure_category=FailureCategory.MISSING_INFORMATION),
        ]
        # Using string categories from agentops.diagnosis
        ground_truth_strings = {
            "c1": "tool_timeout",
            "c2": "missing_order_id",
        }
        res = calculate_failure_localization_accuracy(evals, ground_truth_strings)
        self.assertEqual(res["matched_count"], 2)
        self.assertEqual(res["failed_cases_total"], 2)
        self.assertEqual(res["localization_accuracy"], 1.0)

    def test_calculate_regression_failure_rate(self):
        baseline_evals = [
            EvaluationResult("b1", "r1", "c1", "PASS", True, 1.0),
            EvaluationResult("b2", "r2", "c2", "PASS", True, 1.0),
            EvaluationResult("b3", "r3", "c3", "FAIL", False, 0.0),
        ]
        # Candidate breaks c2 (regression)
        candidate_evals = [
            EvaluationResult("c1", "r1", "c1", "PASS", True, 1.0),
            EvaluationResult("c2", "r2", "c2", "FAIL", False, 0.0),
            EvaluationResult("c3", "r3", "c3", "PASS", True, 1.0),
        ]
        rfr = calculate_regression_failure_rate(baseline_evals, candidate_evals)
        self.assertEqual(rfr["regressed_count"], 1)
        self.assertEqual(rfr["baseline_passed_count"], 2)
        self.assertEqual(rfr["regression_rate"], 0.5)

    def test_calculate_repair_success(self):
        baseline_evals = [
            EvaluationResult("b1", "r1", "c1", "PASS", True, 1.0),
            EvaluationResult("b2", "r2", "c2", "FAIL", False, 0.0),
            EvaluationResult("b3", "r3", "c3", "FAIL", False, 0.0),
        ]
        # Candidate repairs c2, leaves c3 failing
        candidate_evals = [
            EvaluationResult("c1", "r1", "c1", "PASS", True, 1.0),
            EvaluationResult("c2", "r2", "c2", "PASS", True, 1.0),
            EvaluationResult("c3", "r3", "c3", "FAIL", False, 0.0),
        ]
        rsr = calculate_repair_success(baseline_evals, candidate_evals)
        self.assertEqual(rsr["repaired_count"], 1)
        self.assertEqual(rsr["targeted_failing_count"], 2)
        self.assertEqual(rsr["repair_success_rate"], 0.5)

        # Targeted subset check
        rsr_targeted = calculate_repair_success(baseline_evals, candidate_evals, targeted_case_ids={"c2"})
        self.assertEqual(rsr_targeted["repaired_count"], 1)
        self.assertEqual(rsr_targeted["targeted_failing_count"], 1)
        self.assertEqual(rsr_targeted["repair_success_rate"], 1.0)

        # Zero baseline failing cases
        all_pass_baseline = [EvaluationResult("b1", "r1", "c1", "PASS", True, 1.0)]
        rsr_zero = calculate_repair_success(all_pass_baseline, candidate_evals)
        self.assertIsNone(rsr_zero["repair_success_rate"])
        self.assertEqual(rsr_zero["repaired_count"], 0)
        self.assertEqual(rsr_zero["targeted_failing_count"], 0)

    def test_suite_metrics_aggregation(self):
        evals = [EvaluationResult("e1", "r1", "CASE-001-ORDINARY", "PASS", True, 1.0)]
        metrics = calculate_suite_metrics(evals)
        self.assertIn("task_success", metrics)
        self.assertIn("tool_failures", metrics)
        self.assertIn("recovery", metrics)
        self.assertIn("safety_violations", metrics)
        self.assertIn("failure_localization", metrics)
        self.assertEqual(metrics["task_success"]["task_success_rate"], 1.0)

        # With baseline comparison metrics
        baseline_evals = [EvaluationResult("b1", "r0", "CASE-001-ORDINARY", "FAIL", False, 0.0)]
        metrics_with_base = calculate_suite_metrics(evals, baseline_evaluations=baseline_evals)
        self.assertIn("repair_success", metrics_with_base)
        self.assertIn("regression_failures", metrics_with_base)
        self.assertEqual(metrics_with_base["repair_success"]["repaired_count"], 1)

    # -------------------------------------------------------------------------
    # 4. Tracing Interface & End-to-End Suite Runs
    # -------------------------------------------------------------------------

    def test_suite_evaluation_with_tracer(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = tf.name

        try:
            init_db(db_path)
            agent = SupportAgent(config=get_default_config())
            report = run_suite_evaluation(agent=agent, db_path=db_path)

            self.assertEqual(report.total_cases, 6)
            self.assertEqual(report.passed_cases, 6)
            self.assertEqual(report.failed_cases, 0)
            self.assertEqual(report.metrics["task_success"]["passed_count"], 6)
            self.assertEqual(report.metrics["task_success"]["total_cases"], 6)
            self.assertEqual(report.metrics["safety_violations"]["violation_count"], 0)

            # Verify runs were persisted and queryable via tracing interface
            runs = list_runs(db_path)
            self.assertEqual(len(runs), 6)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_controlled_second_agent_baseline_comparison(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = tf.name

        try:
            init_db(db_path)
            b_rep, c_rep, comp = run_baseline_comparison(db_path=db_path)

            self.assertEqual(b_rep.total_cases, 6)
            self.assertEqual(b_rep.passed_cases, 6)
            self.assertEqual(b_rep.agent_version, "v1.0.0")

            self.assertEqual(c_rep.total_cases, 6)
            self.assertEqual(c_rep.agent_version, "v0.9.0-fastfail")

            # Fast-fail configuration regresses on Case 1 and Case 6 due to step limits
            self.assertEqual(comp.verdict, "REJECTED")
            self.assertEqual(comp.safety_status, "PASS")
            self.assertGreater(comp.regression_rate["regressed_count"], 0)
            self.assertEqual(len(comp.case_comparisons), 6)

            # Check comparison fields
            case_1_comp = next(c for c in comp.case_comparisons if c["case_id"] == "CASE-001-ORDINARY")
            self.assertEqual(case_1_comp["baseline_verdict"], "PASS")
            self.assertEqual(case_1_comp["candidate_verdict"], "FAIL")
            self.assertEqual(case_1_comp["delta_status"], "REGRESSION")
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_evaluation_record_persistence(self):
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = tf.name

        try:
            init_db(db_path)
            eval_res = EvaluationResult(
                eval_id="eval-uuid-001",
                run_id="run-uuid-001",
                case_id="CASE-001-ORDINARY",
                verdict="PASS",
                passed=True,
                score=1.0,
                checks={"status_check": True, "safety_check": True},
                evidence_event_ids=["ev-01", "ev-02"],
            )
            record = eval_res.to_record()
            save_evaluation(db_path, record)

            # Read back from DB
            import sqlite3
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM evaluations WHERE eval_id = ?", ("eval-uuid-001",)).fetchone()
            conn.close()

            self.assertIsNotNone(row)
            self.assertEqual(row["run_id"], "run-uuid-001")
            self.assertEqual(row["verdict"], "PASS")
            self.assertEqual(row["score"], 1.0)
            self.assertIn("status_check", row["checks"])
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    # -------------------------------------------------------------------------
    # 5. App Formatting & Helpers
    # -------------------------------------------------------------------------

    def test_app_format_rate(self):
        rate_dict = {"my_rate": 0.75, "passed": 3, "total": 4}
        formatted = format_rate(rate_dict, "my_rate", "passed", "total")
        self.assertEqual(formatted, "3/4 (75.0%)")

        na_dict = {"my_rate": None, "passed": 0, "total": 0}
        formatted_na = format_rate(na_dict, "my_rate", "passed", "total")
        self.assertEqual(formatted_na, "N/A (0/0)")


if __name__ == "__main__":
    unittest.main()
