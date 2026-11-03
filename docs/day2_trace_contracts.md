# Day 2 — Trace Contracts & Storage Schema

**Project:** AgentOps  
**Status:** Draft  
**Depends on:** `day1_trace_and_database.md`, `day2_agent_contracts.md`, `src/models.py`

---

## Overview

This document specifies the canonical field contracts for persisting agent runs, structured events, and evaluation records. All field names are authoritative and must be used verbatim in any tracer module, database schema, or serialisation layer.

---

## 1. Run Record

Represents a single end-to-end agent execution.

| Field | Type | Description |
|---|---|---|
| `run_id` | `str` (UUID) | Globally unique run identifier |
| `case_id` | `str` | Test/production case identifier (maps to `AgentInput.case_id`) |
| `agent_version` | `str` | Semver or commit SHA of the agent under test |
| `status` | `AgentStatus` | Terminal status: `SUCCESS`, `FAILED`, `SAFE_STOPPED` |
| `start_time` | `datetime` (ISO 8601 UTC) | Wall-clock time when run was initiated |
| `end_time` | `datetime` (ISO 8601 UTC) | Wall-clock time when run completed or aborted |
| `final_output` | `str \| None` | Final agent response text, if produced |
| `error_code` | `str \| None` | Machine-readable error tag, e.g. `TOOL_TIMEOUT` |

---

## 2. Trace Event Record

One entry per observable agent step, ordered by `sequence_no`.

| Field | Type | Description |
|---|---|---|
| `event_id` | `str` (UUID) | Globally unique event identifier |
| `run_id` | `str` (UUID) | Parent run (FK → `runs.run_id`) |
| `sequence_no` | `int` | Monotonically increasing step counter within a run |
| `timestamp` | `datetime` (ISO 8601 UTC) | Wall-clock time when event was emitted |
| `event_type` | `EventType` | One of the enum values below |
| `stage` | `str \| None` | Human label for the current agent stage, e.g. `"tool_dispatch"` |
| `tool_name` | `str \| None` | Name of the tool invoked (populated for `TOOL_CALL` / `TOOL_RESULT`) |
| `input` | `dict \| None` | JSON-serialisable tool or planner input payload |
| `output` | `dict \| None` | JSON-serialisable tool or planner output payload |
| `status` | `str \| None` | Step-level status: `"ok"`, `"error"`, `"skipped"` |
| `duration_ms` | `int \| None` | Elapsed milliseconds for this step |
| `error_code` | `str \| None` | Step-level machine-readable error tag |

---

## 3. Event Types

Aligned with the `EventType` enum in `src/models.py`.

| `EventType` | Meaning |
|---|---|
| `PLANNING` | Agent produces its reasoning / action plan |
| `TOOL_CALL` | Agent dispatches a tool with specific arguments |
| `TOOL_RESULT` | Raw response returned by the tool |
| `OBSERVATION` | Agent's interpretation / synthesis of the tool result |
| `SAFE_STOP` | Agent halts gracefully (policy limit, max turns, etc.) |
| `FINAL_STATUS` | Terminal bookkeeping event; closes the run |

> **Note — VALIDATION (future):** A `VALIDATION` event type for safety/schema checks is not yet in `EventType`. When added, it should sit between `TOOL_RESULT` and `OBSERVATION` in sequence. Track via open question #1 below.

---

## 4. Evaluation Record

Records a pass/fail verdict for a completed run against a reference case.

| Field | Type | Description |
|---|---|---|
| `eval_id` | `str` (UUID) | Globally unique evaluation identifier |
| `run_id` | `str` (UUID) | Evaluated run (FK → `runs.run_id`) |
| `case_id` | `str` | Case under evaluation (denormalised for fast querying) |
| `verdict` | `str` | `"PASS"` or `"FAIL"` |
| `score` | `float` | Continuous quality score in `[0.0, 1.0]` |
| `checks` | `dict` | JSON map of individual check names → bool/value |
| `failure_category` | `str \| None` | Taxonomy tag, e.g. `"wrong_order"`, `"policy_violation"` |
| `evidence_event_ids` | `list[str]` | `event_id` values that support the verdict |
| `created_at` | `datetime` (ISO 8601 UTC) | Evaluation creation timestamp |

---

## 5. Proposed SQLite Schema

```sql
-- Runs
CREATE TABLE IF NOT EXISTS runs (
    run_id        TEXT PRIMARY KEY,
    case_id       TEXT NOT NULL,
    agent_version TEXT NOT NULL,
    status        TEXT NOT NULL,
    start_time    TEXT NOT NULL,
    end_time      TEXT,
    final_output  TEXT,
    error_code    TEXT
);

-- Events
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    sequence_no INTEGER NOT NULL,
    timestamp   TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    stage       TEXT,
    tool_name   TEXT,
    input       TEXT,   -- JSON
    output      TEXT,   -- JSON
    status      TEXT,
    duration_ms INTEGER,
    error_code  TEXT,
    UNIQUE (run_id, sequence_no)
);

-- Evaluations
CREATE TABLE IF NOT EXISTS evaluations (
    eval_id            TEXT PRIMARY KEY,
    run_id             TEXT NOT NULL REFERENCES runs(run_id),
    case_id            TEXT NOT NULL,
    verdict            TEXT NOT NULL CHECK (verdict IN ('PASS', 'FAIL')),
    score              REAL NOT NULL CHECK (score BETWEEN 0.0 AND 1.0),
    checks             TEXT,   -- JSON
    failure_category   TEXT,
    evidence_event_ids TEXT,   -- JSON array of event_id strings
    created_at         TEXT NOT NULL
);

-- Repair Attempts
CREATE TABLE IF NOT EXISTS repair_attempts (
    repair_id        TEXT PRIMARY KEY,
    baseline_run_id  TEXT NOT NULL REFERENCES runs(run_id),
    candidate_config TEXT,   -- JSON serialised AgentConfig delta
    candidate_run_id TEXT REFERENCES runs(run_id),
    diagnosis        TEXT,
    proposed_diff    TEXT,
    repair_status    TEXT NOT NULL,  -- e.g. "pending", "applied", "rejected"
    created_at       TEXT NOT NULL
);
```

---

## 6. Tracer Module — Python Signatures

The tracer module (`src/tracer.py`) must expose the following interface.  
`RunRecord`, `TraceEvent`, and `EvaluationRecord` are typed dataclasses/Pydantic models mirroring the field contracts in §1–4.

```python
def init_db(db_path: str) -> None:
    """Create all tables and indexes if they do not exist."""
    ...

def save_run(db_path: str, run: RunRecord) -> None:
    """Upsert a RunRecord into the runs table."""
    ...

def save_event(db_path: str, event: TraceEvent) -> None:
    """Insert a TraceEvent; raises IntegrityError on duplicate (run_id, sequence_no)."""
    ...

def load_trace(db_path: str, run_id: str) -> tuple[RunRecord, list[TraceEvent]]:
    """Return the RunRecord and all TraceEvents ordered by sequence_no."""
    ...

def save_evaluation(db_path: str, evaluation: EvaluationRecord) -> None:
    """Upsert an EvaluationRecord into the evaluations table."""
    ...
```

---

## 7. Compact JSON Example

One run with two events — a `TOOL_CALL` followed by its `TOOL_RESULT`.

```json
{
  "run": {
    "run_id": "a1b2c3d4-0001-0000-0000-000000000001",
    "case_id": "CASE-001",
    "agent_version": "0.2.0",
    "status": "SUCCESS",
    "start_time": "2026-11-03T10:00:00Z",
    "end_time": "2026-11-03T10:00:02Z",
    "final_output": "Your order ORD-10001 is currently In Transit and will arrive by 2026-11-05.",
    "error_code": null
  },
  "events": [
    {
      "event_id": "e1000000-0001-0000-0000-000000000001",
      "run_id": "a1b2c3d4-0001-0000-0000-000000000001",
      "sequence_no": 1,
      "timestamp": "2026-11-03T10:00:00.500Z",
      "event_type": "TOOL_CALL",
      "stage": "tool_dispatch",
      "tool_name": "order_lookup",
      "input": {"order_id": "ORD-10001", "customer_id": "CUST-101"},
      "output": null,
      "status": "ok",
      "duration_ms": null,
      "error_code": null
    },
    {
      "event_id": "e1000000-0001-0000-0000-000000000002",
      "run_id": "a1b2c3d4-0001-0000-0000-000000000001",
      "sequence_no": 2,
      "timestamp": "2026-11-03T10:00:01.200Z",
      "event_type": "TOOL_RESULT",
      "stage": "tool_dispatch",
      "tool_name": "order_lookup",
      "input": null,
      "output": {
        "order_id": "ORD-10001",
        "customer_id": "CUST-101",
        "status": "In Transit",
        "estimated_delivery": "2026-11-05"
      },
      "status": "ok",
      "duration_ms": 700,
      "error_code": null
    }
  ]
}
```

---

## 8. Conflicts with Day 1 & Open Questions

### Field Renames (Day 1 → Day 2)

| Day 1 name | Day 2 name | Reason |
|---|---|---|
| `scenario_id` | `case_id` | Aligns with `AgentInput.case_id` in `src/models.py` |
| `sequence_number` | `sequence_no` | Shorter; consistent with SQL convention |
| `termination_status` | `status` (on Run) | Brevity; mirrors `AgentStatus` enum name |
| `step_number` | `stage` (on Event) | Semantic shift — stage is a label, not a counter |

### Open Questions

1. **`VALIDATION` event type** — Should `VALIDATION` be added to the `EventType` enum in `src/models.py`? It is referenced in evaluator logic but absent from the current enum. Decision needed before the tracer module is implemented.

2. **`repair_attempts` module placement** — Should `repair_attempts` live in `src/tracer.py` alongside run/event storage, or in a dedicated `src/repair.py` module? Separating it avoids bloating the tracer but adds an import boundary.

3. **`checks` schema** — The `checks` JSON in `EvaluationRecord` is currently untyped. Should a canonical schema (e.g., `{"check_name": {"passed": bool, "detail": str}}`) be enforced at write time?

4. **`evidence_event_ids` integrity** — Foreign-key enforcement of `evidence_event_ids` entries against `events.event_id` is not possible via native SQLite FK on a JSON array. Consider a separate `evaluation_evidence` join table if referential integrity is required.
