"""
Trace recorder: connects SupportAgent's event callback to the SQLite trace store.

Agent, evaluator, and UI code use the functions here and in src/tracer.py and never
touch SQLite directly:

    recorder = TraceRecorder(db_path, agent_input, config, config_role=CONFIG_ROLE_ORIGINAL)
    agent = SupportAgent(config=config, event_callback=recorder)
    recorder.finish(agent.run(agent_input))

or, in one call:

    result = run_and_record(agent, agent_input, db_path, config_role=CONFIG_ROLE_CANDIDATE,
                            baseline_run_id=original_run_id)
"""
import json
import threading
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from src.models import (
    AgentConfig,
    AgentInput,
    AgentResult,
    AgentStatus,
    EventType,
    ObservableEvent,
    StopReason,
)
from src.tracer import (
    CONFIG_ROLE_CANDIDATE,
    CONFIG_ROLE_ORIGINAL,
    RUN_STATUS_RUNNING,
    RunRecord,
    TraceEvent,
    get_run_trace,
    init_db,
    save_event,
    save_run,
)

# ── Lifecycle stage labels stored in TraceEvent.stage ─────────────────────────

STAGE_PLANNING = "planning"
STAGE_TOOL_START = "tool_start"
STAGE_TOOL_SUCCESS = "tool_success"
STAGE_TOOL_FAILURE = "tool_failure"
STAGE_OBSERVATION = "observation"
STAGE_VALIDATION = "validation"
STAGE_ERROR = "error"
STAGE_SAFE_STOP = "safe_stop"
STAGE_RUN_COMPLETED = "run_completed"
STAGE_RUN_FAILED = "run_failed"
STAGE_RUN_TIMEOUT = "run_timeout"
STAGE_LIMIT_STOP = "limit_stop"

_TERMINAL_STAGE_BY_STATUS = {
    AgentStatus.SUCCESS.value: STAGE_RUN_COMPLETED,
    AgentStatus.CLARIFICATION_NEEDED.value: STAGE_RUN_COMPLETED,
    AgentStatus.ERROR.value: STAGE_RUN_FAILED,
    AgentStatus.TIMEOUT.value: STAGE_RUN_TIMEOUT,
    AgentStatus.LIMIT_EXCEEDED.value: STAGE_LIMIT_STOP,
}

_VALID_CONFIG_ROLES = (CONFIG_ROLE_ORIGINAL, CONFIG_ROLE_CANDIDATE)

# ── Sanitisation: never persist secrets or hidden reasoning ──────────────────

_SECRET_KEY_MARKERS = ("password", "secret", "api_key", "apikey", "token", "authorization", "credential")
_HIDDEN_REASONING_KEYS = {"reasoning", "hidden_reasoning", "chain_of_thought", "scratchpad"}
REDACTED = "[REDACTED]"


def sanitize_payload(value: Any) -> Any:
    """Return a JSON-safe copy with secret-like values redacted and hidden reasoning dropped."""
    if isinstance(value, dict):
        clean = {}
        for key, item in value.items():
            key_str = str(key)
            lowered = key_str.lower()
            if lowered in _HIDDEN_REASONING_KEYS:
                continue
            if any(marker in lowered for marker in _SECRET_KEY_MARKERS):
                clean[key_str] = REDACTED
            else:
                clean[key_str] = sanitize_payload(item)
        return clean
    if isinstance(value, (list, tuple, set)):
        return [sanitize_payload(item) for item in value]
    if hasattr(value, "value") and isinstance(getattr(value, "value"), (str, int, float)):
        return value.value  # Enum
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _to_json(value: Any) -> Optional[str]:
    if value is None:
        return None
    return json.dumps(sanitize_payload(value), sort_keys=True)


def _enum_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    return value.value if hasattr(value, "value") else str(value)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def effective_config(config: AgentConfig, agent_input: AgentInput) -> AgentConfig:
    """The configuration the agent will actually run with (base config plus input overrides)."""
    overrides = (agent_input.context or {}).get("config_overrides") or {}
    merged = asdict(config)
    merged.update({key: value for key, value in overrides.items() if key in merged})
    return AgentConfig(**merged)


# ── Recorder ─────────────────────────────────────────────────────────────────

class TraceRecorder:
    """
    Callable EventCallback that writes every agent event to SQLite as it happens,
    assigning a gap-free sequence number per run.
    """

    def __init__(
        self,
        db_path: str,
        agent_input: AgentInput,
        config: AgentConfig,
        config_role: str = CONFIG_ROLE_ORIGINAL,
        baseline_run_id: Optional[str] = None,
    ):
        if config_role not in _VALID_CONFIG_ROLES:
            raise ValueError(f"config_role must be one of {_VALID_CONFIG_ROLES}, got {config_role!r}")
        if config_role == CONFIG_ROLE_ORIGINAL and baseline_run_id is not None:
            raise ValueError("baseline_run_id only applies to candidate runs")

        self.db_path = db_path
        self.run_id = agent_input.run_id
        cfg = effective_config(config, agent_input)
        overrides = (agent_input.context or {}).get("config_overrides") or {}
        version = overrides.get("version") or agent_input.agent_version or cfg.version

        self.run = RunRecord(
            run_id=agent_input.run_id,
            case_id=agent_input.case_id,
            agent_version=version,
            status=RUN_STATUS_RUNNING,
            start_time=_now(),
            config_role=config_role,
            config_snapshot=_to_json(asdict(cfg)),
            baseline_run_id=baseline_run_id,
        )
        self._seq = 0
        self._lock = threading.Lock()
        self._tool_started: Dict[Tuple[Optional[str], Optional[str]], float] = {}
        self._finished = False

        init_db(db_path)
        if get_run_trace(db_path, self.run_id) is not None:
            raise ValueError(f"run_id {self.run_id!r} already exists in the trace store")
        save_run(db_path, self.run)

    # EventCallback interface
    def __call__(self, event: ObservableEvent) -> None:
        self.record(event)

    def record(self, event: ObservableEvent) -> TraceEvent:
        """Convert one ObservableEvent into a TraceEvent and persist it immediately."""
        if event.run_id != self.run_id:
            raise ValueError(f"event for run {event.run_id!r} sent to recorder for {self.run_id!r}")
        with self._lock:
            if self._finished:
                raise RuntimeError(f"run {self.run_id!r} is already finished")
            self._seq += 1
            trace_event = self._convert(event, self._seq)
            save_event(self.db_path, trace_event)
            return trace_event

    def _convert(self, event: ObservableEvent, sequence_no: int) -> TraceEvent:
        event_type = _enum_value(event.event_type)
        payload = event.payload or {}
        stage = STAGE_PLANNING
        input_data: Any = None
        output_data: Any = None
        status: Optional[str] = None
        duration_ms: Optional[float] = None
        error_code: Optional[str] = None

        if event_type == EventType.TOOL_CALL.value:
            stage = STAGE_TOOL_START
            input_data = payload
            self._tool_started[(event.tool_name, payload.get("call_id"))] = time.perf_counter()
        elif event_type == EventType.TOOL_RESULT.value:
            status = payload.get("status")
            stage = STAGE_TOOL_SUCCESS if status == "SUCCESS" else STAGE_TOOL_FAILURE
            output_data = payload
            error = payload.get("error") or {}
            error_code = error.get("error_code") if isinstance(error, dict) else None
            started = self._tool_started.pop((event.tool_name, payload.get("call_id")), None)
            if started is not None:
                duration_ms = round((time.perf_counter() - started) * 1000.0, 3)
        elif event_type == EventType.OBSERVATION.value:
            stage = STAGE_OBSERVATION
            output_data = payload
            status = payload.get("status")
        elif event_type == EventType.VALIDATION.value:
            stage = STAGE_VALIDATION
            output_data = payload
            if payload.get("status") != "SUCCESS":
                status, error_code = "ERROR", "VALIDATION_UNAVAILABLE"
            elif payload.get("is_valid"):
                status = "SUCCESS"
            else:
                status, error_code = "ERROR", "RESPONSE_INVALID"
        elif event_type == EventType.ERROR.value:
            stage = STAGE_ERROR
            output_data = payload
            status = "ERROR"
            error_code = payload.get("error_code")
        elif event_type == EventType.SAFE_STOP.value:
            run_status = _enum_value(payload.get("status"))
            stage = STAGE_LIMIT_STOP if run_status == AgentStatus.LIMIT_EXCEEDED.value else STAGE_SAFE_STOP
            output_data = payload
            status = run_status
            error_code = payload.get("error_code")
        elif event_type == EventType.FINAL_STATUS.value:
            run_status = _enum_value(payload.get("status"))
            stage = _TERMINAL_STAGE_BY_STATUS.get(run_status, STAGE_RUN_FAILED)
            output_data = payload
            status = run_status
            metrics = payload.get("metrics") or {}
            duration_ms = metrics.get("latency_ms")
        else:  # PLANNING and any future event types
            input_data = payload

        return TraceEvent(
            event_id=str(uuid.uuid4()),
            run_id=self.run_id,
            sequence_no=sequence_no,
            timestamp=event.timestamp or _now(),
            event_type=event_type,
            stage=stage,
            tool_name=event.tool_name,
            input=_to_json(input_data),
            output=_to_json(output_data),
            status=status,
            duration_ms=duration_ms,
            error_code=error_code,
            step_index=event.step_index,
        )

    def finish(self, result: AgentResult) -> RunRecord:
        """Store the run's terminal state from the agent's result."""
        if result.run_id != self.run_id:
            raise ValueError(f"result for run {result.run_id!r} sent to recorder for {self.run_id!r}")
        details = result.error.get("details") if result.error else None
        self.run.agent_version = result.agent_version or self.run.agent_version
        self.run.status = _enum_value(result.status)
        self.run.end_time = _now()
        self.run.final_output = result.answer
        self.run.error_code = result.error_code
        self.run.error_details = _to_json(details)
        self.run.stop_reason = _enum_value(result.stop_reason)
        self.run.metrics = _to_json(result.metrics)
        return self._close()

    def fail(self, exc: BaseException) -> RunRecord:
        """Mark the run as failed when the agent raised instead of returning a result."""
        self._record_internal_error(exc)
        self.run.status = AgentStatus.ERROR.value
        self.run.end_time = _now()
        self.run.error_code = "AGENT_EXCEPTION"
        self.run.error_details = _to_json({"exception_type": type(exc).__name__, "message": str(exc)})
        self.run.stop_reason = StopReason.ERROR_ABORT.value
        return self._close()

    def _record_internal_error(self, exc: BaseException) -> None:
        with self._lock:
            self._seq += 1
            save_event(self.db_path, TraceEvent(
                event_id=str(uuid.uuid4()),
                run_id=self.run_id,
                sequence_no=self._seq,
                timestamp=_now(),
                event_type=EventType.FINAL_STATUS.value,
                stage=STAGE_RUN_FAILED,
                output=_to_json({"exception_type": type(exc).__name__}),
                status=AgentStatus.ERROR.value,
                error_code="AGENT_EXCEPTION",
            ))

    def _close(self) -> RunRecord:
        with self._lock:
            self._finished = True
            save_run(self.db_path, self.run)
        return self.run


def run_and_record(
    agent: Any,
    agent_input: AgentInput,
    db_path: str,
    config_role: str = CONFIG_ROLE_ORIGINAL,
    baseline_run_id: Optional[str] = None,
) -> AgentResult:
    """
    Run a SupportAgent with tracing attached and persist the run and all its events.
    The agent's own event_callback (if any) still receives every event.
    """
    recorder = TraceRecorder(db_path, agent_input, agent.config, config_role, baseline_run_id)
    previous_callback = agent.event_callback

    def callback(event: ObservableEvent) -> None:
        recorder(event)
        if previous_callback:
            previous_callback(event)

    agent.event_callback = callback
    try:
        result = agent.run(agent_input)
    except Exception as exc:
        recorder.fail(exc)
        raise
    finally:
        agent.event_callback = previous_callback
    recorder.finish(result)
    return result


def load_run_for_evaluation(db_path: str, run_id: str) -> Tuple[AgentResult, List[TraceEvent]]:
    """
    Rebuild the (AgentResult, events) pair the evaluator takes from a stored, finished run:
        evaluate(case, *load_run_for_evaluation(db_path, run_id))
    """
    trace = get_run_trace(db_path, run_id)
    if trace is None:
        raise KeyError(f"unknown run_id {run_id!r}")
    run = trace.run
    if run.status == RUN_STATUS_RUNNING:
        raise ValueError(f"run {run_id!r} has not finished")
    details = json.loads(run.error_details) if run.error_details else None
    result = AgentResult(
        case_id=run.case_id,
        run_id=run.run_id,
        agent_version=run.agent_version,
        status=AgentStatus(run.status),
        answer=run.final_output or "",
        stop_reason=StopReason(run.stop_reason) if run.stop_reason else StopReason.ERROR_ABORT,
        metrics=json.loads(run.metrics) if run.metrics else {},
        error_code=run.error_code,
        error={"error_code": run.error_code, "details": details} if run.error_code else None,
    )
    return result, trace.events
