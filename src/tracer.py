"""
Tracer module: typed models and SQLite persistence for AgentOps run traces.
Public interface for agent and runner modules to record events without
coupling to SQLite internals.
"""
import json
import os
import sqlite3
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

# ── Constants ────────────────────────────────────────────────────────────────

# Distinguishes the original (baseline) agent configuration from a repair candidate.
CONFIG_ROLE_ORIGINAL = "original"
CONFIG_ROLE_CANDIDATE = "candidate"

# Run status while the agent is still executing (terminal values are AgentStatus).
RUN_STATUS_RUNNING = "RUNNING"

# ── Typed Records ────────────────────────────────────────────────────────────

@dataclass
class RunRecord:
    run_id: str
    case_id: str
    agent_version: str
    status: str            # AgentStatus value or RUN_STATUS_RUNNING
    start_time: str        # ISO-8601 UTC
    end_time: Optional[str] = None
    final_output: Optional[str] = None
    error_code: Optional[str] = None
    error_details: Optional[str] = None     # JSON string
    stop_reason: Optional[str] = None       # StopReason value
    metrics: Optional[str] = None           # JSON string
    config_role: Optional[str] = None       # CONFIG_ROLE_ORIGINAL | CONFIG_ROLE_CANDIDATE
    config_snapshot: Optional[str] = None   # JSON string of the effective AgentConfig
    baseline_run_id: Optional[str] = None   # candidate run -> original run it is compared to

@dataclass
class TraceEvent:
    event_id: str
    run_id: str
    sequence_no: int
    timestamp: str         # ISO-8601 UTC
    event_type: str        # EventType value
    stage: Optional[str] = None       # lifecycle label e.g. "tool_start", "run_completed"
    tool_name: Optional[str] = None
    input: Optional[str] = None       # JSON string
    output: Optional[str] = None      # JSON string
    status: Optional[str] = None      # "SUCCESS" | "ERROR" | None
    duration_ms: Optional[float] = None
    error_code: Optional[str] = None
    step_index: Optional[int] = None  # agent step the event belongs to

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

@dataclass
class RunTrace:
    """A stored run together with its events in sequence order."""
    run: RunRecord
    events: List[TraceEvent]

# ── DB Helpers ───────────────────────────────────────────────────────────────

_RUN_COLUMNS = (
    "run_id", "case_id", "agent_version", "status", "start_time", "end_time",
    "final_output", "error_code", "error_details", "stop_reason", "metrics",
    "config_role", "config_snapshot", "baseline_run_id",
)

_EVENT_COLUMNS = (
    "event_id", "run_id", "sequence_no", "timestamp", "event_type", "stage",
    "tool_name", "input", "output", "status", "duration_ms", "error_code",
    "step_index",
)

# Columns added after the Day 2 schema; applied to existing databases by init_db.
_MIGRATIONS = {
    "runs": (
        ("error_details", "TEXT"),
        ("stop_reason", "TEXT"),
        ("metrics", "TEXT"),
        ("config_role", "TEXT"),
        ("config_snapshot", "TEXT"),
        ("baseline_run_id", "TEXT"),
    ),
    "events": (
        ("step_index", "INTEGER"),
    ),
}


def _connect_readonly(db_path: str) -> sqlite3.Connection:
    """Open an existing store read-only; read functions can never modify trace data."""
    conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _row_value(row: sqlite3.Row, key: str):
    return row[key] if key in row.keys() else None


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(**{col: _row_value(row, col) for col in _RUN_COLUMNS})


def _event_from_row(row: sqlite3.Row) -> TraceEvent:
    return TraceEvent(**{col: _row_value(row, col) for col in _EVENT_COLUMNS})


def _insert_run(conn: sqlite3.Connection, run: RunRecord) -> None:
    conn.execute(
        f"INSERT OR REPLACE INTO runs ({', '.join(_RUN_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _RUN_COLUMNS)})",
        tuple(getattr(run, col) for col in _RUN_COLUMNS),
    )


def _insert_event(conn: sqlite3.Connection, event: TraceEvent) -> None:
    conn.execute(
        f"INSERT INTO events ({', '.join(_EVENT_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _EVENT_COLUMNS)})",
        tuple(getattr(event, col) for col in _EVENT_COLUMNS),
    )


def init_db(db_path: str) -> None:
    """Create tables and indexes if they don't exist, and add any missing columns."""
    dirname = os.path.dirname(db_path)
    if dirname:
        os.makedirs(dirname, exist_ok=True)

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id          TEXT PRIMARY KEY,
                case_id         TEXT NOT NULL,
                agent_version   TEXT NOT NULL,
                status          TEXT NOT NULL,
                start_time      TEXT NOT NULL,
                end_time        TEXT,
                final_output    TEXT,
                error_code      TEXT,
                error_details   TEXT,
                stop_reason     TEXT,
                metrics         TEXT,
                config_role     TEXT,
                config_snapshot TEXT,
                baseline_run_id TEXT
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
                step_index  INTEGER,
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
        for table, columns in _MIGRATIONS.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, col_type in columns:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {col_type}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_case ON runs (case_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_version ON runs (agent_version)")
    conn.close()


def save_run(db_path: str, run: RunRecord) -> None:
    """Insert or replace a RunRecord."""
    conn = sqlite3.connect(db_path)
    with conn:
        _insert_run(conn, run)
    conn.close()


def save_event(db_path: str, event: TraceEvent) -> None:
    """Insert a TraceEvent; raises IntegrityError on duplicate (run_id, sequence_no)."""
    conn = sqlite3.connect(db_path)
    with conn:
        _insert_event(conn, event)
    conn.close()


def save_run_trace(db_path: str, run: RunRecord, events: List[TraceEvent]) -> None:
    """Atomically store a run and replace its events. Either everything is saved or nothing."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            _insert_run(conn, run)
            conn.execute("DELETE FROM events WHERE run_id = ?", (run.run_id,))
            for event in events:
                if event.run_id != run.run_id:
                    raise ValueError(
                        f"event {event.event_id!r} belongs to run {event.run_id!r}, not {run.run_id!r}"
                    )
                _insert_event(conn, event)
    finally:
        conn.close()


def load_trace(db_path: str, run_id: str) -> tuple:
    """Return (RunRecord, list[TraceEvent]) ordered by sequence_no ASC."""
    if not os.path.exists(db_path):
        return (None, [])
    conn = _connect_readonly(db_path)
    try:
        run_row = conn.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run_row is None:
            return (None, [])
        event_rows = conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY sequence_no ASC", (run_id,)
        ).fetchall()
        return (_run_from_row(run_row), [_event_from_row(row) for row in event_rows])
    finally:
        conn.close()


def get_run_trace(db_path: str, run_id: str) -> Optional[RunTrace]:
    """Return the run and its events in sequence order, or None if the run is unknown."""
    if not os.path.exists(db_path):
        return None
    run, events = load_trace(db_path, run_id)
    if run is None:
        return None
    return RunTrace(run=run, events=events)


def list_runs(
    db_path: str,
    case_id: Optional[str] = None,
    agent_version: Optional[str] = None,
    config_role: Optional[str] = None,
    baseline_run_id: Optional[str] = None,
) -> List[RunRecord]:
    """
    List stored runs (oldest first), optionally filtered by case, version, config role,
    or the original run a candidate was compared against.
    """
    if not os.path.exists(db_path):
        return []
    clauses, params = [], []
    for column, value in (
        ("case_id", case_id), ("agent_version", agent_version), ("config_role", config_role),
        ("baseline_run_id", baseline_run_id),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    conn = _connect_readonly(db_path)
    try:
        rows = conn.execute(
            f"SELECT * FROM runs{where} ORDER BY start_time ASC, run_id ASC", params
        ).fetchall()
        return [_run_from_row(row) for row in rows]
    finally:
        conn.close()


def list_evaluations(db_path: str, run_id: Optional[str] = None) -> List[EvaluationRecord]:
    """List stored evaluations (oldest first), optionally for one run."""
    if not os.path.exists(db_path):
        return []
    where, params = ("WHERE run_id = ?", (run_id,)) if run_id is not None else ("", ())
    conn = _connect_readonly(db_path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "evaluations" not in tables:
            return []
        rows = conn.execute(
            f"SELECT * FROM evaluations {where} ORDER BY created_at ASC, eval_id ASC", params
        ).fetchall()
        return [EvaluationRecord(**{key: row[key] for key in row.keys()}) for row in rows]
    finally:
        conn.close()


def _parse_json(value: Optional[str]):
    return json.loads(value) if value is not None else None


def run_trace_to_dict(trace: RunTrace) -> dict:
    """Plain, JSON-ready view of a stored run for the UI (JSON columns decoded)."""
    run = asdict(trace.run)
    for key in ("error_details", "metrics", "config_snapshot"):
        run[key] = _parse_json(run[key])
    events = []
    for event in trace.events:
        item = asdict(event)
        item["input"] = _parse_json(item["input"])
        item["output"] = _parse_json(item["output"])
        events.append(item)
    return {"run": run, "events": events}


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
