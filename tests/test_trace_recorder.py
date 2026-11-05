"""
Tests for src/trace_recorder.py — agent event callback wired to the SQLite trace store.
All data is synthetic (fixtures from src/tools.py).
"""
import importlib
import json
import os
import sqlite3
import tempfile
import unittest

from src.agent import SupportAgent
from src.evaluation import build_canonical_cases, evaluate
from src.models import (
    AgentInput,
    AgentStatus,
    ObservableEvent,
    get_baseline_fast_fail_config,
    get_default_config,
)
import src.tracer as tracer_module
from src.trace_recorder import (
    REDACTED,
    TraceRecorder,
    load_run_for_evaluation,
    run_and_record,
    sanitize_payload,
)
from src.tracer import (
    CONFIG_ROLE_CANDIDATE,
    CONFIG_ROLE_ORIGINAL,
    RUN_STATUS_RUNNING,
    get_run_trace,
    init_db,
    list_runs,
    run_trace_to_dict,
    save_run_trace,
)


def _input(run_id: str, message: str, case_id: str = "CASE-SYN-001", version: str = "") -> AgentInput:
    return AgentInput(
        case_id=case_id,
        run_id=run_id,
        agent_version=version,
        user_message=message,
        customer_id="CUST-101",
    )


class TraceRecorderTestBase(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmpdir.name, "traces", "agentops.db")

    def tearDown(self):
        self._tmpdir.cleanup()

    def _stages(self, run_id):
        return [e.stage for e in get_run_trace(self.db_path, run_id).events]


class TestNormalRun(TraceRecorderTestBase):

    def test_successful_run_persists_run_and_ordered_events(self):
        agent = SupportAgent(config=get_default_config())
        result = run_and_record(agent, _input("RUN-OK-1", "Status of ORD-10001?"), self.db_path)
        self.assertEqual(result.status, AgentStatus.SUCCESS)

        trace = get_run_trace(self.db_path, "RUN-OK-1")
        run = trace.run
        self.assertEqual(run.case_id, "CASE-SYN-001")
        self.assertEqual(run.agent_version, "v1.0.0")
        self.assertEqual(run.status, "SUCCESS")
        self.assertEqual(run.stop_reason, "GOAL_REACHED")
        self.assertEqual(run.final_output, result.answer)
        self.assertIsNone(run.error_code)
        self.assertIsNotNone(run.start_time)
        self.assertIsNotNone(run.end_time)
        self.assertLessEqual(run.start_time, run.end_time)
        self.assertEqual(json.loads(run.metrics)["total_tool_calls"], result.metrics["total_tool_calls"])

        seqs = [e.sequence_no for e in trace.events]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)))
        self.assertEqual(len({e.event_id for e in trace.events}), len(trace.events))
        self.assertTrue(all(e.run_id == "RUN-OK-1" for e in trace.events))
        timestamps = [e.timestamp for e in trace.events]
        self.assertEqual(timestamps, sorted(timestamps))

        stages = [e.stage for e in trace.events]
        for expected in ("planning", "tool_start", "tool_success", "observation", "validation",
                         "safe_stop", "run_completed"):
            self.assertIn(expected, stages)
        self.assertEqual(stages[-1], "run_completed")

    def test_tool_events_carry_input_output_status_and_duration(self):
        agent = SupportAgent(config=get_default_config())
        run_and_record(agent, _input("RUN-OK-2", "Where is ORD-10001"), self.db_path)
        events = get_run_trace(self.db_path, "RUN-OK-2").events

        start = next(e for e in events if e.stage == "tool_start" and e.tool_name == "order_lookup")
        self.assertEqual(json.loads(start.input)["parameters"], {"order_id": "ORD-10001"})

        success = next(e for e in events if e.stage == "tool_success" and e.tool_name == "order_lookup")
        self.assertGreater(success.sequence_no, start.sequence_no)
        self.assertEqual(success.status, "SUCCESS")
        self.assertEqual(json.loads(success.output)["data"]["order_id"], "ORD-10001")
        self.assertIsNotNone(success.duration_ms)
        self.assertGreaterEqual(success.duration_ms, 0.0)
        self.assertEqual(success.step_index, start.step_index)

        validation = next(e for e in events if e.stage == "validation")
        self.assertEqual(validation.event_type, "VALIDATION")
        self.assertEqual(validation.status, "SUCCESS")
        self.assertTrue(json.loads(validation.output)["is_valid"])

    def test_existing_callback_still_receives_events(self):
        received = []
        agent = SupportAgent(config=get_default_config(), event_callback=received.append)
        run_and_record(agent, _input("RUN-OK-3", "ORD-10001 please"), self.db_path)
        self.assertEqual(len(received), len(get_run_trace(self.db_path, "RUN-OK-3").events))
        self.assertEqual(agent.event_callback, received.append)


class TestFailedRuns(TraceRecorderTestBase):

    def test_unknown_order_records_tool_failure_and_run_failed(self):
        agent = SupportAgent(config=get_default_config())
        result = run_and_record(agent, _input("RUN-FAIL-1", "Check ORD-99999"), self.db_path)
        self.assertEqual(result.status, AgentStatus.ERROR)

        trace = get_run_trace(self.db_path, "RUN-FAIL-1")
        self.assertEqual(trace.run.status, "ERROR")
        self.assertEqual(trace.run.error_code, "ORDER_NOT_FOUND")
        failure = next(e for e in trace.events if e.stage == "tool_failure")
        self.assertEqual(failure.status, "ERROR")
        self.assertEqual(failure.error_code, "ORDER_NOT_FOUND")
        self.assertEqual(trace.events[-1].stage, "run_failed")

    def test_timeout_run(self):
        agent = SupportAgent(config=get_default_config())
        run_and_record(agent, _input("RUN-TIMEOUT-1", "Status ORD-TIMEOUT"), self.db_path)
        trace = get_run_trace(self.db_path, "RUN-TIMEOUT-1")
        self.assertEqual(trace.run.status, "TIMEOUT")
        self.assertEqual(trace.run.error_code, "TIMEOUT_ERROR")
        self.assertEqual(trace.run.stop_reason, "TIME_EXCEEDED")
        self.assertEqual(trace.events[-1].stage, "run_timeout")
        # default config retries once: two tool_start/tool_failure pairs
        self.assertEqual(self._stages("RUN-TIMEOUT-1").count("tool_failure"), 2)

    def test_limit_stop_run(self):
        agent = SupportAgent(config=get_default_config())
        agent_input = _input("RUN-LIMIT-1", "Status ORD-10001")
        agent_input.context = {"config_overrides": {"max_steps": 1}}
        run_and_record(agent, agent_input, self.db_path)
        trace = get_run_trace(self.db_path, "RUN-LIMIT-1")
        self.assertEqual(trace.run.status, "LIMIT_EXCEEDED")
        self.assertEqual(trace.run.error_code, "MAX_STEPS_EXCEEDED")
        self.assertIn("limit_stop", [e.stage for e in trace.events])
        self.assertEqual(trace.events[-1].stage, "limit_stop")
        self.assertEqual(json.loads(trace.run.config_snapshot)["max_steps"], 1)

    def test_error_details_persisted(self):
        agent = SupportAgent(config=get_default_config())
        run_and_record(agent, _input("RUN-CONFLICT-1", "Status ORD-CONFLICT"), self.db_path)
        run = get_run_trace(self.db_path, "RUN-CONFLICT-1").run
        self.assertEqual(run.error_code, "CONFLICTING_INFORMATION")
        self.assertEqual(run.stop_reason, "UNRESOLVABLE_CONFLICT")
        details = json.loads(run.error_details)
        self.assertIn("order_status", details)
        self.assertIn("tracking_status", details)

    def test_agent_exception_marks_run_failed_and_reraises(self):
        class ExplodingTool:
            def execute(self, parameters, call_id, simulated_timeout=False):
                raise RuntimeError("synthetic tool crash")

        agent = SupportAgent(config=get_default_config(), tools={"order_lookup": ExplodingTool()})
        with self.assertRaises(RuntimeError):
            run_and_record(agent, _input("RUN-EXC-1", "ORD-10001"), self.db_path)

        trace = get_run_trace(self.db_path, "RUN-EXC-1")
        self.assertEqual(trace.run.status, "ERROR")
        self.assertEqual(trace.run.error_code, "AGENT_EXCEPTION")
        self.assertEqual(json.loads(trace.run.error_details)["exception_type"], "RuntimeError")
        self.assertIsNotNone(trace.run.end_time)
        self.assertEqual(trace.events[-1].stage, "run_failed")
        self.assertEqual([e.sequence_no for e in trace.events], list(range(1, len(trace.events) + 1)))
        self.assertIsNone(agent.event_callback)


class TestPersistenceAcrossReload(TraceRecorderTestBase):

    def test_trace_survives_module_reload_and_new_connection(self):
        agent = SupportAgent(config=get_default_config())
        run_and_record(agent, _input("RUN-RELOAD-1", "ORD-10001"), self.db_path)
        before = run_trace_to_dict(get_run_trace(self.db_path, "RUN-RELOAD-1"))
        del agent

        reloaded = importlib.reload(tracer_module)
        trace = reloaded.get_run_trace(self.db_path, "RUN-RELOAD-1")
        self.assertEqual(reloaded.run_trace_to_dict(trace), before)

    def test_init_db_migrates_day2_schema_without_losing_rows(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute("""CREATE TABLE runs (run_id TEXT PRIMARY KEY, case_id TEXT NOT NULL,
                agent_version TEXT NOT NULL, status TEXT NOT NULL, start_time TEXT NOT NULL,
                end_time TEXT, final_output TEXT, error_code TEXT)""")
            conn.execute("""CREATE TABLE events (event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL,
                sequence_no INTEGER NOT NULL, timestamp TEXT NOT NULL, event_type TEXT NOT NULL,
                stage TEXT, tool_name TEXT, input TEXT, output TEXT, status TEXT,
                duration_ms REAL, error_code TEXT, UNIQUE (run_id, sequence_no))""")
            conn.execute("INSERT INTO runs VALUES ('OLD-1','CASE-OLD','v0.1','SUCCESS',"
                         "'2026-01-01T00:00:00+00:00',NULL,'ok',NULL)")
        conn.close()

        init_db(self.db_path)
        trace = get_run_trace(self.db_path, "OLD-1")
        self.assertEqual(trace.run.final_output, "ok")
        self.assertIsNone(trace.run.config_role)

    def test_save_run_trace_is_atomic_and_ordered(self):
        recorder_db = os.path.join(self._tmpdir.name, "source.db")
        run_and_record(SupportAgent(), _input("RUN-COPY-1", "ORD-10001"), recorder_db)
        source = get_run_trace(recorder_db, "RUN-COPY-1")

        save_run_trace(self.db_path, source.run, list(reversed(source.events)))
        copied = get_run_trace(self.db_path, "RUN-COPY-1")
        self.assertEqual([e.event_id for e in copied.events], [e.event_id for e in source.events])

        bad = list(source.events)
        bad[0] = tracer_module.TraceEvent(**{**bad[0].__dict__, "run_id": "OTHER"})
        changed_run = tracer_module.RunRecord(**{**source.run.__dict__, "final_output": "changed"})
        with self.assertRaises(ValueError):
            save_run_trace(self.db_path, changed_run, bad)
        unchanged = get_run_trace(self.db_path, "RUN-COPY-1")
        self.assertEqual(unchanged.run.final_output, source.run.final_output)
        self.assertEqual(len(unchanged.events), len(source.events))

    def test_unknown_run_returns_none(self):
        self.assertIsNone(get_run_trace(self.db_path, "MISSING"))
        init_db(self.db_path)
        self.assertIsNone(get_run_trace(self.db_path, "MISSING"))
        self.assertEqual(list_runs(self.db_path), [])


class TestRunVersionLinkage(TraceRecorderTestBase):

    def test_original_and_candidate_runs_are_distinguishable_and_linked(self):
        original = run_and_record(
            SupportAgent(config=get_baseline_fast_fail_config()),
            _input("RUN-ORIG-1", "Status ORD-TIMEOUT-TRANSIENT", case_id="CASE-SYN-005"),
            self.db_path,
            config_role=CONFIG_ROLE_ORIGINAL,
        )
        candidate = run_and_record(
            SupportAgent(config=get_default_config()),
            _input("RUN-CAND-1", "Status ORD-TIMEOUT-TRANSIENT", case_id="CASE-SYN-005"),
            self.db_path,
            config_role=CONFIG_ROLE_CANDIDATE,
            baseline_run_id="RUN-ORIG-1",
        )
        self.assertEqual(original.status, AgentStatus.TIMEOUT)
        self.assertEqual(candidate.status, AgentStatus.SUCCESS)

        runs = list_runs(self.db_path, case_id="CASE-SYN-005")
        self.assertEqual([r.run_id for r in runs], ["RUN-ORIG-1", "RUN-CAND-1"])

        orig_run, cand_run = runs
        self.assertEqual(orig_run.config_role, "original")
        self.assertEqual(orig_run.agent_version, "v0.9.0-fastfail")
        self.assertIsNone(orig_run.baseline_run_id)
        self.assertEqual(json.loads(orig_run.config_snapshot)["retry_count"], 0)

        self.assertEqual(cand_run.config_role, "candidate")
        self.assertEqual(cand_run.agent_version, "v1.0.0")
        self.assertEqual(cand_run.baseline_run_id, "RUN-ORIG-1")
        self.assertEqual(json.loads(cand_run.config_snapshot)["retry_count"], 1)

        self.assertEqual([r.run_id for r in list_runs(self.db_path, config_role="candidate")], ["RUN-CAND-1"])
        self.assertEqual([r.run_id for r in list_runs(self.db_path, agent_version="v0.9.0-fastfail")],
                         ["RUN-ORIG-1"])

        orig_events = {e.event_id for e in get_run_trace(self.db_path, "RUN-ORIG-1").events}
        cand_events = {e.event_id for e in get_run_trace(self.db_path, "RUN-CAND-1").events}
        self.assertFalse(orig_events & cand_events)

    def test_version_override_in_input_is_recorded(self):
        agent_input = _input("RUN-OVR-1", "ORD-10001")
        agent_input.context = {"config_overrides": {"version": "v1.1.0-candidate", "retry_count": 2}}
        run_and_record(SupportAgent(), agent_input, self.db_path, config_role=CONFIG_ROLE_CANDIDATE)
        run = get_run_trace(self.db_path, "RUN-OVR-1").run
        self.assertEqual(run.agent_version, "v1.1.0-candidate")
        self.assertEqual(json.loads(run.config_snapshot)["retry_count"], 2)

    def test_duplicate_run_id_and_bad_role_rejected(self):
        run_and_record(SupportAgent(), _input("RUN-DUP-1", "ORD-10001"), self.db_path)
        with self.assertRaises(ValueError):
            run_and_record(SupportAgent(), _input("RUN-DUP-1", "ORD-10001"), self.db_path)
        with self.assertRaises(ValueError):
            TraceRecorder(self.db_path, _input("RUN-ROLE-1", "x"), get_default_config(), config_role="other")
        with self.assertRaises(ValueError):
            TraceRecorder(self.db_path, _input("RUN-ROLE-2", "x"), get_default_config(),
                          config_role=CONFIG_ROLE_ORIGINAL, baseline_run_id="RUN-DUP-1")

    def test_recorder_rejects_events_for_other_runs_and_after_finish(self):
        agent_input = _input("RUN-GUARD-1", "ORD-10001")
        recorder = TraceRecorder(self.db_path, agent_input, get_default_config())
        self.assertEqual(get_run_trace(self.db_path, "RUN-GUARD-1").run.status, RUN_STATUS_RUNNING)
        stray = ObservableEvent(case_id="C", run_id="OTHER", agent_version="v", event_type="PLANNING",
                                step_index=1, payload={})
        with self.assertRaises(ValueError):
            recorder(stray)
        result = SupportAgent(event_callback=recorder).run(agent_input)
        recorder.finish(result)
        with self.assertRaises(RuntimeError):
            recorder(ObservableEvent(case_id="C", run_id="RUN-GUARD-1", agent_version="v",
                                     event_type="PLANNING", step_index=1, payload={}))


class TestEvaluatorAndUiAccess(TraceRecorderTestBase):

    def test_evaluator_runs_on_stored_trace(self):
        case = build_canonical_cases()["CASE-001-ORDINARY"]
        live_events = []
        live_result = run_and_record(
            SupportAgent(event_callback=live_events.append), case.input, self.db_path
        )
        stored_result, stored_events = load_run_for_evaluation(self.db_path, case.input.run_id)

        self.assertEqual(stored_result.status, live_result.status)
        self.assertEqual(stored_result.answer, live_result.answer)
        self.assertEqual(stored_result.metrics, live_result.metrics)

        live_eval = evaluate(case, live_result, live_events)
        stored_eval = evaluate(case, stored_result, stored_events)
        self.assertEqual(stored_eval.verdict, live_eval.verdict)
        self.assertEqual(stored_eval.checks, live_eval.checks)
        self.assertEqual(stored_eval.evidence_event_ids, [e.event_id for e in stored_events])

    def test_evaluator_access_for_failed_run(self):
        case = build_canonical_cases()["CASE-003-UNKNOWN-ORDER"]
        run_and_record(SupportAgent(), case.input, self.db_path)
        result, events = load_run_for_evaluation(self.db_path, case.input.run_id)
        self.assertEqual(result.status, AgentStatus.ERROR)
        self.assertEqual(result.error_code, "ORDER_NOT_FOUND")
        evaluation = evaluate(case, result, events)
        self.assertEqual(evaluation.run_id, case.input.run_id)
        self.assertEqual(evaluation.verdict, "PASS")

    def test_load_for_evaluation_rejects_unknown_and_unfinished(self):
        with self.assertRaises(KeyError):
            load_run_for_evaluation(self.db_path, "MISSING")
        TraceRecorder(self.db_path, _input("RUN-OPEN-1", "ORD-10001"), get_default_config())
        with self.assertRaises(ValueError):
            load_run_for_evaluation(self.db_path, "RUN-OPEN-1")

    def test_ui_dict_is_json_ready(self):
        run_and_record(SupportAgent(), _input("RUN-UI-1", "ORD-10001"), self.db_path)
        view = run_trace_to_dict(get_run_trace(self.db_path, "RUN-UI-1"))
        json.dumps(view)
        self.assertEqual(view["run"]["config_snapshot"]["version"], "v1.0.0")
        self.assertIsInstance(view["events"][0]["input"], dict)
        self.assertEqual([e["sequence_no"] for e in view["events"]],
                         list(range(1, len(view["events"]) + 1)))


class TestSanitization(unittest.TestCase):

    def test_secrets_redacted_and_hidden_reasoning_dropped(self):
        clean = sanitize_payload({
            "order_id": "ORD-10001",
            "api_key": "synthetic-not-a-real-key",
            "nested": {"Authorization": "Bearer synthetic", "items": [{"password": "x"}]},
            "chain_of_thought": "internal",
            "reasoning": "internal",
            "status": AgentStatus.SUCCESS,
        })
        self.assertEqual(clean["order_id"], "ORD-10001")
        self.assertEqual(clean["api_key"], REDACTED)
        self.assertEqual(clean["nested"]["Authorization"], REDACTED)
        self.assertEqual(clean["nested"]["items"][0]["password"], REDACTED)
        self.assertNotIn("chain_of_thought", clean)
        self.assertNotIn("reasoning", clean)
        self.assertEqual(clean["status"], "SUCCESS")


if __name__ == "__main__":
    unittest.main()
