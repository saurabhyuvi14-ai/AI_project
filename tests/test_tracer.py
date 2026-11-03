"""
Tests for src/tracer.py — typed models and SQLite persistence.
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from src.models import AgentConfig, AgentInput, EventType
from src.agent import SupportAgent
from src.tracer import (
    EvaluationRecord,
    RunRecord,
    TraceEvent,
    Tracer,
    init_db,
    load_trace,
    save_evaluation,
    save_event,
    save_run,
)


def _make_run(run_id: str = "RUN-TEST-001") -> RunRecord:
    return RunRecord(
        run_id=run_id,
        case_id="CASE-001",
        agent_version="v1.0",
        status="SUCCESS",
        start_time=datetime.now(timezone.utc).isoformat(),
    )


def _make_event(run_id: str, seq: int, event_type: str = "PLANNING") -> TraceEvent:
    import uuid
    return TraceEvent(
        event_id=str(uuid.uuid4()),
        run_id=run_id,
        sequence_no=seq,
        timestamp=datetime.now(timezone.utc).isoformat(),
        event_type=event_type,
    )


class TestTracerDB(unittest.TestCase):

    def setUp(self):
        self._tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmpfile.close()
        self.db_path = self._tmpfile.name

    def tearDown(self):
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    # ── 1. init_db creates tables ────────────────────────────────────────────

    def test_init_db_creates_tables(self):
        init_db(self.db_path)
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        self.assertIn("runs", tables)
        self.assertIn("events", tables)
        self.assertIn("evaluations", tables)

    # ── 2. save & load RunRecord ─────────────────────────────────────────────

    def test_save_and_load_run(self):
        init_db(self.db_path)
        run = RunRecord(
            run_id="RUN-TEST-002",
            case_id="CASE-002",
            agent_version="v2.0",
            status="ERROR",
            start_time="2026-01-01T00:00:00+00:00",
            end_time="2026-01-01T00:01:00+00:00",
            final_output="some answer",
            error_code="ORDER_NOT_FOUND",
        )
        save_run(self.db_path, run)
        loaded_run, _ = load_trace(self.db_path, "RUN-TEST-002")
        self.assertIsNotNone(loaded_run)
        self.assertEqual(loaded_run.run_id, run.run_id)
        self.assertEqual(loaded_run.case_id, run.case_id)
        self.assertEqual(loaded_run.agent_version, run.agent_version)
        self.assertEqual(loaded_run.status, run.status)
        self.assertEqual(loaded_run.start_time, run.start_time)
        self.assertEqual(loaded_run.end_time, run.end_time)
        self.assertEqual(loaded_run.final_output, run.final_output)
        self.assertEqual(loaded_run.error_code, run.error_code)

    # ── 3. save & load events in order ──────────────────────────────────────

    def test_save_and_load_events_in_order(self):
        init_db(self.db_path)
        run = _make_run("RUN-TEST-003")
        save_run(self.db_path, run)

        for seq in [1, 2, 3]:
            save_event(self.db_path, _make_event("RUN-TEST-003", seq))

        _, events = load_trace(self.db_path, "RUN-TEST-003")
        self.assertEqual(len(events), 3)
        self.assertEqual([e.sequence_no for e in events], [1, 2, 3])

    # ── 4. ordering not by insertion order ──────────────────────────────────

    def test_event_ordering_not_insertion_order(self):
        init_db(self.db_path)
        run = _make_run("RUN-TEST-004")
        save_run(self.db_path, run)

        # Insert out-of-order: 3, 1, 2
        for seq in [3, 1, 2]:
            save_event(self.db_path, _make_event("RUN-TEST-004", seq))

        _, events = load_trace(self.db_path, "RUN-TEST-004")
        self.assertEqual([e.sequence_no for e in events], [1, 2, 3])

    # ── 5. error event fields preserved ─────────────────────────────────────

    def test_error_event_fields(self):
        init_db(self.db_path)
        run = _make_run("RUN-TEST-005")
        save_run(self.db_path, run)

        import uuid
        ev = TraceEvent(
            event_id=str(uuid.uuid4()),
            run_id="RUN-TEST-005",
            sequence_no=1,
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type="TOOL_RESULT",
            tool_name="order_lookup",
            status="ERROR",
            error_code="ORDER_NOT_FOUND",
            duration_ms=42.5,
        )
        save_event(self.db_path, ev)

        _, events = load_trace(self.db_path, "RUN-TEST-005")
        self.assertEqual(len(events), 1)
        loaded = events[0]
        self.assertEqual(loaded.error_code, "ORDER_NOT_FOUND")
        self.assertEqual(loaded.status, "ERROR")
        self.assertEqual(loaded.tool_name, "order_lookup")
        self.assertAlmostEqual(loaded.duration_ms, 42.5)

    # ── 6. Tracer.record + flush ─────────────────────────────────────────────

    def test_tracer_class_record_and_flush(self):
        run = _make_run("RUN-TEST-006")
        tracer = Tracer(db_path=self.db_path, run=run)

        tracer.record(event_type="PLANNING", stage="step_1",
                      input={"thought": "starting"})
        tracer.record(event_type="TOOL_CALL", stage="step_2",
                      tool_name="order_lookup",
                      input={"order_id": "ORD-10001"})

        # Before flush, DB should have 0 events (run is saved)
        _, events_before = load_trace(self.db_path, "RUN-TEST-006")
        self.assertEqual(len(events_before), 0)

        tracer.flush()

        _, events_after = load_trace(self.db_path, "RUN-TEST-006")
        self.assertEqual(len(events_after), 2)
        self.assertEqual(events_after[0].event_type, "PLANNING")
        self.assertEqual(events_after[1].event_type, "TOOL_CALL")
        self.assertEqual(events_after[1].tool_name, "order_lookup")

    # ── 7. Tracer.complete_run ───────────────────────────────────────────────

    def test_tracer_complete_run(self):
        run = _make_run("RUN-TEST-007")
        tracer = Tracer(db_path=self.db_path, run=run)

        tracer.record(event_type="PLANNING", stage="step_1")

        end_time = datetime.now(timezone.utc).isoformat()
        tracer.complete_run(
            end_time=end_time,
            final_output="Order ORD-10001 is SHIPPED.",
            error_code=None,
        )

        loaded_run, events = load_trace(self.db_path, "RUN-TEST-007")
        self.assertIsNotNone(loaded_run)
        self.assertEqual(loaded_run.end_time, end_time)
        self.assertEqual(loaded_run.final_output, "Order ORD-10001 is SHIPPED.")
        self.assertIsNone(loaded_run.error_code)
        self.assertEqual(len(events), 1)

    # ── 8. save_evaluation ──────────────────────────────────────────────────

    def test_save_evaluation(self):
        init_db(self.db_path)
        run = _make_run("RUN-TEST-008")
        save_run(self.db_path, run)

        import uuid, sqlite3
        eval_rec = EvaluationRecord(
            eval_id=str(uuid.uuid4()),
            run_id="RUN-TEST-008",
            case_id="CASE-001",
            verdict="PASS",
            score=0.95,
            checks=json.dumps({"has_order_status": True, "has_carrier": True}),
            failure_category=None,
            evidence_event_ids=json.dumps(["evt-001", "evt-002"]),
        )
        save_evaluation(self.db_path, eval_rec)

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM evaluations WHERE eval_id = ?", (eval_rec.eval_id,)
        ).fetchone()
        conn.close()

        self.assertIsNotNone(row)
        self.assertEqual(row["verdict"], "PASS")
        self.assertAlmostEqual(row["score"], 0.95)
        self.assertEqual(json.loads(row["checks"]), {"has_order_status": True, "has_carrier": True})
        self.assertEqual(json.loads(row["evidence_event_ids"]), ["evt-001", "evt-002"])

    # ── 9. trace_with_agent ──────────────────────────────────────────────────

    def test_trace_with_agent(self):
        run = RunRecord(
            run_id="RUN-TEST-009",
            case_id="CASE-001",
            agent_version="v1.0",
            status="RUNNING",
            start_time=datetime.now(timezone.utc).isoformat(),
        )
        tracer = Tracer(db_path=self.db_path, run=run)

        def event_callback(observable_event):
            tracer.record(
                event_type=observable_event.event_type.value
                if hasattr(observable_event.event_type, "value")
                else observable_event.event_type,
                stage=f"step_{observable_event.step_index}",
                tool_name=observable_event.tool_name,
                input=observable_event.payload,
            )

        agent = SupportAgent(
            config=AgentConfig(),
            event_callback=event_callback,
        )

        agent_input = AgentInput(
            case_id="CASE-001",
            run_id="RUN-TEST-009",
            agent_version="v1.0",
            user_message="What is the status of my order ORD-10001?",
            customer_id="CUST-101",
        )

        result = agent.run(agent_input)

        tracer.flush()

        _, events = load_trace(self.db_path, "RUN-TEST-009")
        self.assertGreaterEqual(len(events), 3)

        seq_numbers = [e.sequence_no for e in events]
        self.assertEqual(seq_numbers, sorted(seq_numbers))

        event_types = [e.event_type for e in events]
        self.assertIn("PLANNING", event_types)


if __name__ == "__main__":
    unittest.main()
