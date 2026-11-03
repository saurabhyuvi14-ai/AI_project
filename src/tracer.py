"""
Tracer module: typed models and SQLite persistence for AgentOps run traces.
Public interface for agent and runner modules to record events without
coupling to SQLite internals.
"""
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

# ── Typed Records ────────────────────────────────────────────────────────────

@dataclass
class RunRecord:
    run_id: str
    case_id: str
    agent_version: str
    status: str            # AgentStatus value
    start_time: str        # ISO-8601 UTC
    end_time: Optional[str] = None
    final_output: Optional[str] = None
    error_code: Optional[str] = None

@dataclass
class TraceEvent:
    event_id: str
    run_id: str
    sequence_no: int
    timestamp: str         # ISO-8601 UTC
    event_type: str        # EventType value
    stage: Optional[str] = None       # step/stage label e.g. "step_1"
    tool_name: Optional[str] = None
    input: Optional[str] = None       # JSON string
    output: Optional[str] = None      # JSON string
    status: Optional[str] = None      # "SUCCESS" | "ERROR" | None
    duration_ms: Optional[float] = None
    error_code: Optional[str] = None

@dataclass
class EvaluationRecord:
    eval_id: str
    run_id: str
    case_id: str
    verdict: str           # "PASS" | "FAIL"
    score: float           # 0.0 – 1.0
    checks: str            # JSON string of {check_name: bool}
    failure_category: Optional[str] = None
    evidence_event_ids: Optional[str] = None   # JSON list of event_ids
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

# ── DB Helpers ───────────────────────────────────────────────────────────────

def init_db(db_path: str) -> None:
    """Create tables if they don't exist."""
    dirname = os.path.dirname(db_path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id        TEXT PRIMARY KEY,
                case_id       TEXT NOT NULL,
                agent_version TEXT NOT NULL,
                status        TEXT NOT NULL,
                start_time    TEXT NOT NULL,
                end_time      TEXT,
                final_output  TEXT,
                error_code    TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                event_id    TEXT PRIMARY KEY,
                run_id      TEXT NOT NULL,
                sequence_no INTEGER NOT NULL,
                timestamp   TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                stage       TEXT,
                tool_name   TEXT,
                input       TEXT,
                output      TEXT,
                status      TEXT,
                duration_ms REAL,
                error_code  TEXT,
                UNIQUE (run_id, sequence_no)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS evaluations (
                eval_id            TEXT PRIMARY KEY,
                run_id             TEXT NOT NULL,
                case_id            TEXT NOT NULL,
                verdict            TEXT NOT NULL,
                score              REAL NOT NULL,
                checks             TEXT NOT NULL,
                failure_category   TEXT,
                evidence_event_ids TEXT,
                created_at         TEXT NOT NULL
            )
        """)
    conn.close()


def save_run(db_path: str, run: RunRecord) -> None:
    """Insert or replace a RunRecord."""
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("""
            INSERT OR REPLACE INTO runs
                (run_id, case_id, agent_version, status, start_time, end_time, final_output, error_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            run.run_id,
            run.case_id,
            run.agent_version,
            run.status,
            run.start_time,
            run.end_time,
            run.final_output,
            run.error_code,
        ))
    conn.close()


def save_event(db_path: str, event: TraceEvent) -> None:
    """Insert a TraceEvent."""
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("""
            INSERT INTO events
                (event_id, run_id, sequence_no, timestamp, event_type, stage,
                 tool_name, input, output, status, duration_ms, error_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            event.event_id,
            event.run_id,
            event.sequence_no,
            event.timestamp,
            event.event_type,
            event.stage,
            event.tool_name,
            event.input,
            event.output,
            event.status,
            event.duration_ms,
            event.error_code,
        ))
    conn.close()


def load_trace(db_path: str, run_id: str) -> tuple:
    """Return (RunRecord, list[TraceEvent]) ordered by sequence_no ASC."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    run_row = conn.execute(
        "SELECT * FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()

    if run_row is None:
        conn.close()
        return (None, [])

    run = RunRecord(
        run_id=run_row["run_id"],
        case_id=run_row["case_id"],
        agent_version=run_row["agent_version"],
        status=run_row["status"],
        start_time=run_row["start_time"],
        end_time=run_row["end_time"],
        final_output=run_row["final_output"],
        error_code=run_row["error_code"],
    )

    event_rows = conn.execute(
        "SELECT * FROM events WHERE run_id = ? ORDER BY sequence_no ASC", (run_id,)
    ).fetchall()

    events = [
        TraceEvent(
            event_id=row["event_id"],
            run_id=row["run_id"],
            sequence_no=row["sequence_no"],
            timestamp=row["timestamp"],
            event_type=row["event_type"],
            stage=row["stage"],
            tool_name=row["tool_name"],
            input=row["input"],
            output=row["output"],
            status=row["status"],
            duration_ms=row["duration_ms"],
            error_code=row["error_code"],
        )
        for row in event_rows
    ]

    conn.close()
    return (run, events)


def save_evaluation(db_path: str, evaluation: EvaluationRecord) -> None:
    """Insert an EvaluationRecord."""
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("""
            INSERT INTO evaluations
                (eval_id, run_id, case_id, verdict, score, checks,
                 failure_category, evidence_event_ids, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            evaluation.eval_id,
            evaluation.run_id,
            evaluation.case_id,
            evaluation.verdict,
            evaluation.score,
            evaluation.checks,
            evaluation.failure_category,
            evaluation.evidence_event_ids,
            evaluation.created_at,
        ))
    conn.close()


# ── High-Level Tracer Interface (agent calls this) ────────────────────────────

class Tracer:
    """
    Simple stateful tracer the agent calls to record events.
    Stores events in-memory and flushes to SQLite on flush() or context exit.
    Usage:
        tracer = Tracer(db_path="data/agentops.db", run=run_record)
        tracer.record(event_type="TOOL_CALL", stage="step_2", tool_name="order_lookup",
                      input={"order_id": "ORD-10001"})
        tracer.flush()
    """
    def __init__(self, db_path: str, run: RunRecord):
        self.db_path = db_path
        self.run = run
        self._events: List[TraceEvent] = []
        self._seq = 0
        init_db(db_path)
        save_run(db_path, run)

    def record(
        self,
        event_type: str,
        stage: Optional[str] = None,
        tool_name: Optional[str] = None,
        input: Optional[dict] = None,
        output: Optional[dict] = None,
        status: Optional[str] = None,
        duration_ms: Optional[float] = None,
        error_code: Optional[str] = None,
    ) -> TraceEvent:
        """Create and buffer a TraceEvent. Returns the event."""
        self._seq += 1
        event = TraceEvent(
            event_id=str(uuid.uuid4()),
            run_id=self.run.run_id,
            sequence_no=self._seq,
            timestamp=datetime.now(timezone.utc).isoformat(),
            event_type=event_type,
            stage=stage,
            tool_name=tool_name,
            input=json.dumps(input) if input is not None else None,
            output=json.dumps(output) if output is not None else None,
            status=status,
            duration_ms=duration_ms,
            error_code=error_code,
        )
        self._events.append(event)
        return event

    def flush(self) -> None:
        """Persist all buffered events to SQLite."""
        for ev in self._events:
            save_event(self.db_path, ev)
        self._events.clear()

    def complete_run(self, end_time: str, final_output: Optional[str], error_code: Optional[str]) -> None:
        """Update the run record with end state and flush."""
        self.run.end_time = end_time
        self.run.final_output = final_output
        self.run.error_code = error_code
        save_run(self.db_path, self.run)
        self.flush()

    @property
    def events(self) -> List[TraceEvent]:
        return list(self._events)
