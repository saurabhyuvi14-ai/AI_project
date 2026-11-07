"""
Tests for src/trace_diagnosis.py and the read-only trace store functions used by
the evaluator, UI, and diagnosis. Synthetic data only.
"""
import hashlib
import os
import tempfile
import unittest

from agentops.diagnosis import DiagnosisRequest, FailureCategory as DiagnosisCategory, diagnose
from src.agent import SupportAgent
from src.evaluation import build_canonical_case_list, build_canonical_cases, evaluate
from src.models import AgentInput, get_baseline_fast_fail_config, get_default_config
from src.trace_diagnosis import (
    TraceStoreDiagnosisTools,
    case_expectations_from_cases,
    failure_summary,
    inspect_run,
    tool_config_version,
)
from src.trace_recorder import load_run_for_evaluation, run_and_record
from src.tracer import (
    CONFIG_ROLE_CANDIDATE,
    CONFIG_ROLE_ORIGINAL,
    get_run_trace,
    list_evaluations,
    list_runs,
    load_trace,
    save_evaluation,
)


def _case_input(case_id: str, run_id: str) -> AgentInput:
    base = build_canonical_cases()[case_id].input
    return AgentInput(
        case_id=case_id,
        run_id=run_id,
        agent_version="",
        user_message=base.user_message,
        customer_id=base.customer_id,
        context=base.context,
    )


def _file_digest(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class TraceDiagnosisTests(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "agentops.db")
        self.cases = build_canonical_cases()
        self.tools = TraceStoreDiagnosisTools(self.db_path, case_expectations_from_cases(self.cases.values()))

    def tearDown(self):
        self._tmpdir.cleanup()

    def _run_and_evaluate(self, case_id, run_id, config=None, role=CONFIG_ROLE_ORIGINAL, baseline=None):
        run_and_record(SupportAgent(config=config or get_default_config()),
                       _case_input(case_id, run_id), self.db_path,
                       config_role=role, baseline_run_id=baseline)
        result, events = load_run_for_evaluation(self.db_path, run_id)
        evaluation = evaluate(self.cases[case_id], result, events)
        save_evaluation(self.db_path, evaluation.to_record())
        return evaluation

    def test_inspect_run_matches_stored_trace(self):
        self._run_and_evaluate("CASE-001-ORDINARY", "RUN-D-OK")
        stored = get_run_trace(self.db_path, "RUN-D-OK")
        inspection = self.tools.inspect_run("RUN-D-OK")

        self.assertEqual(inspection.case_id, "CASE-001-ORDINARY")
        self.assertEqual(inspection.agent_config_version, "v1.0.0")
        self.assertEqual(inspection.started_at, stored.run.start_time)
        self.assertEqual(inspection.completed_at, stored.run.end_time)
        self.assertEqual([e.event_id for e in inspection.events], [e.event_id for e in stored.events])

        tool_start = next(e for e in inspection.events if e.event_type == "TOOL_CALL")
        self.assertEqual(tool_start.input, {"order_id": "ORD-10001"})
        tool_result = next(e for e in inspection.events if e.event_type == "TOOL_RESULT")
        self.assertEqual(tool_result.output["order_id"], "ORD-10001")
        self.assertIsInstance(tool_result.duration_ms, int)

    def test_timeout_diagnosis_cites_stored_event_ids(self):
        self._run_and_evaluate("CASE-005-TOOL-TIMEOUT", "RUN-D-TIMEOUT", config=get_baseline_fast_fail_config())
        summary = self.tools.get_failure_summary("RUN-D-TIMEOUT")
        self.assertEqual(summary.category, DiagnosisCategory.TOOL_TIMEOUT)

        result = diagnose(DiagnosisRequest(run_ids=("RUN-D-TIMEOUT",), goal="Explain the timeout."), self.tools)
        stored_ids = {e.event_id for e in get_run_trace(self.db_path, "RUN-D-TIMEOUT").events}
        self.assertIn("timeout", result.likely_cause.lower())
        self.assertTrue(result.cited_evidence_event_ids)
        self.assertTrue(set(result.cited_evidence_event_ids) <= stored_ids)

    def test_unknown_order_diagnosis(self):
        self._run_and_evaluate("CASE-003-UNKNOWN-ORDER", "RUN-D-UNKNOWN")
        self.assertEqual(failure_summary(self.db_path, "RUN-D-UNKNOWN").category, DiagnosisCategory.UNKNOWN_ORDER)
        result = diagnose(DiagnosisRequest(run_ids=("RUN-D-UNKNOWN",), goal="Why no order?"), self.tools)
        self.assertIn("ORDER_NOT_FOUND", result.likely_cause)

    def test_failure_summary_uses_latest_evaluation_checks(self):
        evaluation = self._run_and_evaluate("CASE-005-TOOL-TIMEOUT", "RUN-D-FAILCHK",
                                            config=get_baseline_fast_fail_config())
        summary = failure_summary(self.db_path, "RUN-D-FAILCHK")
        expected_failed = tuple(name for name, ok in evaluation.checks.items() if not ok)
        self.assertEqual(summary.failed_assertions, expected_failed)
        self.assertIn(evaluation.verdict, summary.actual)
        self.assertIn("TIMEOUT", summary.actual)

    def test_failure_summary_without_evaluation(self):
        run_and_record(SupportAgent(), _case_input("CASE-001-ORDINARY", "RUN-D-NOEVAL"), self.db_path)
        summary = failure_summary(self.db_path, "RUN-D-NOEVAL")
        self.assertEqual(summary.category, DiagnosisCategory.OTHER)
        self.assertEqual(summary.failed_assertions, ())

    def test_diagnosis_does_not_modify_store(self):
        self._run_and_evaluate("CASE-005-TOOL-TIMEOUT", "RUN-D-RO", config=get_baseline_fast_fail_config())
        before = _file_digest(self.db_path)
        diagnose(DiagnosisRequest(run_ids=("RUN-D-RO",), goal="Explain."), self.tools)
        list_runs(self.db_path)
        list_evaluations(self.db_path)
        self.assertEqual(_file_digest(self.db_path), before)

    def test_read_functions_do_not_create_missing_store(self):
        missing = os.path.join(self._tmpdir.name, "absent.db")
        self.assertEqual(load_trace(missing, "X"), (None, []))
        self.assertEqual(list_runs(missing), [])
        self.assertEqual(list_evaluations(missing), [])
        with self.assertRaises(KeyError):
            inspect_run(missing, "X")
        self.assertFalse(os.path.exists(missing))

    def test_unknown_run_and_case_raise(self):
        self._run_and_evaluate("CASE-001-ORDINARY", "RUN-D-K")
        with self.assertRaises(KeyError):
            self.tools.inspect_run("MISSING")
        with self.assertRaises(KeyError):
            self.tools.get_failure_summary("MISSING")
        with self.assertRaises(KeyError):
            TraceStoreDiagnosisTools(self.db_path).get_case_expectation("CASE-001-ORDINARY")

    def test_original_and_candidate_links_and_tool_config_versions(self):
        self._run_and_evaluate("CASE-005-TOOL-TIMEOUT", "RUN-D-ORIG", config=get_baseline_fast_fail_config())
        self._run_and_evaluate("CASE-005-TOOL-TIMEOUT", "RUN-D-CAND", config=get_default_config(),
                               role=CONFIG_ROLE_CANDIDATE, baseline="RUN-D-ORIG")

        candidates = list_runs(self.db_path, baseline_run_id="RUN-D-ORIG")
        self.assertEqual([r.run_id for r in candidates], ["RUN-D-CAND"])
        self.assertEqual(candidates[0].config_role, CONFIG_ROLE_CANDIDATE)

        orig = self.tools.inspect_run("RUN-D-ORIG")
        cand = self.tools.inspect_run("RUN-D-CAND")
        self.assertNotEqual(orig.agent_config_version, cand.agent_config_version)
        self.assertNotEqual(orig.tool_config_version, cand.tool_config_version)
        self.assertEqual(tool_config_version(get_run_trace(self.db_path, "RUN-D-ORIG").run),
                         orig.tool_config_version)

        self.assertEqual([e.run_id for e in list_evaluations(self.db_path, "RUN-D-CAND")], ["RUN-D-CAND"])
        self.assertEqual(len(list_evaluations(self.db_path)), 2)

        result = diagnose(DiagnosisRequest(run_ids=("RUN-D-ORIG", "RUN-D-CAND"), goal="Compare."), self.tools)
        self.assertEqual(result.run_ids, ("RUN-D-ORIG", "RUN-D-CAND"))

    def test_case_expectations_cover_canonical_cases(self):
        expectations = case_expectations_from_cases(build_canonical_case_list())
        self.assertEqual(set(expectations), set(self.cases))
        unknown = expectations["CASE-003-UNKNOWN-ORDER"]
        self.assertIn("error_code == ORDER_NOT_FOUND", unknown.assertions)


if __name__ == "__main__":
    unittest.main()
