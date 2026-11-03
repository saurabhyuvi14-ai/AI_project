# Day 2 Interface Contracts: Agent, Runner, and Mock Tools

**Project:** AI Agent Evaluation, Observability, and Safe Self-Repair Platform  
**Document:** Interface Contracts & Tracing Specification  
**Status:** Approved for Implementation (Day 2)  
**Target File:** `docs/day2_agent_contracts.md`  

---

## 1. Input Contract: Runner to Support Agent

When the evaluation or production runner invokes the Support Agent, it passes a structured input context. This contract guarantees reproducibility and traceability across runs and candidate evaluation copies.

### Schema: `AgentInput`
```typescript
interface AgentInput {
  case_id: string;               // Evaluation test case ID (e.g., "CASE-001-ORD-NORMAL")
  run_id: string;                // Unique run identifier (e.g., "RUN-20261103-001")
  agent_version: string;         // Version/hash of agent configuration (e.g., "v1.0.0" or "v1.0.1-candidate")
  customer_id?: string;          // Optional authenticated customer ID (e.g., "CUST-901")
  user_message: string;          // Customer query (e.g., "Where is my order ORD-12345?")
  context?: {
    timestamp: string;           // ISO-8601 simulated invocation time
    session_history?: Array<{   // Prior messages in the conversation (if multi-turn)
      role: "user" | "assistant" | "system";
      content: string;
    }>;
    config_overrides?: Record<string, any>; // Optional configuration parameters for autofix experiments
  };
}
```

---

## 2. Output Contract: Agent Final Result

At the conclusion of an execution (or on failure/timeout), the agent must return a deterministic `AgentResult`.

### Schema: `AgentResult`
```typescript
interface AgentResult {
  case_id: string;               // Propagated from AgentInput
  run_id: string;                // Propagated from AgentInput
  agent_version: string;         // Active agent version used for this run
  status: "SUCCESS" | "CLARIFICATION_NEEDED" | "ERROR" | "TIMEOUT" | "LIMIT_EXCEEDED";
  answer: string;                // The user-facing response text (must be polite, concise, and non-technical)
  stop_reason: "GOAL_REACHED" | "NEED_USER_INPUT" | "UNRESOLVABLE_CONFLICT" | "ERROR_ABORT" | "MAX_STEPS" | "MAX_TOOL_CALLS" | "TIME_EXCEEDED";
  metrics: {
    total_steps: number;         // Total reasoning/dispatch steps taken
    total_tool_calls: number;    // Count of tool executions executed
    latency_ms: number;          // Total wall-clock execution time in milliseconds
  };
  error?: {
    error_code: string;          // Standardized machine error code (e.g., "ORDER_NOT_FOUND", "TOOL_TIMEOUT")
    message: string;             // Internal debug message (not shown to customer)
    details?: Record<string, any>; // Contextual metadata (e.g., failed tool name, input params)
  };
}
```

---

## 3. Common Mock Tool Interface

All mock tools implement a uniform execution contract. Tools are strictly read-only, deterministic against synthetic fixtures, and can simulate latency or inject configured error states.

### Standard Tool Request & Response Wrappers

```typescript
interface ToolCallRequest {
  tool_name: "lookup_order_details" | "get_order_items" | "track_shipment";
  parameters: Record<string, any>;
  call_id: string;               // Unique ID per tool invocation (e.g., "call_001")
}

interface ToolCallResult {
  tool_name: string;             // Mirror of requested tool
  call_id: string;               // Matched call ID
  status: "SUCCESS" | "ERROR";
  data?: Record<string, any>;    // Populated on SUCCESS
  error?: {
    error_code: string;          // Machine code (e.g. "ORDER_NOT_FOUND", "TIMEOUT_ERROR")
    message: string;             // Human-readable explanation
  };
  latency_ms: number;            // Tool execution latency
}
```

### Specific Tool Definitions

| Tool Name | Input Fields | Successful Output (`data`) | Error Output (`error_code`) |
| :--- | :--- | :--- | :--- |
| `lookup_order_details` | `order_id` (string, required) | `order_id` (str), `status` (str: `PENDING`\|`PROCESSING`\|`SHIPPED`\|`DELIVERED`\|`CANCELLED`), `customer_id` (str), `created_at` (str), `total_amount` (float), `currency` (str) | `INVALID_INPUT`, `ORDER_NOT_FOUND`, `TIMEOUT_ERROR`, `INTERNAL_ERROR` |
| `get_order_items` | `order_id` (string, required) | `order_id` (str), `items` (array of `{ sku, name, quantity, unit_price }`) | `INVALID_INPUT`, `ORDER_NOT_FOUND`, `PAYLOAD_CORRUPT`, `TIMEOUT_ERROR` |
| `track_shipment` | `order_id` (str, opt), `tracking_number` (str, opt) *(At least one required)* | `tracking_number` (str), `carrier` (str), `shipping_status` (str: `LABEL_CREATED`\|`PICKED_UP`\|`IN_TRANSIT`\|`OUT_FOR_DELIVERY`\|`DELIVERED`), `estimated_delivery` (str), `last_checkpoint` (str) | `INVALID_INPUT`, `TRACKING_NOT_FOUND`, `CARRIER_UNAVAILABLE`, `TIMEOUT_ERROR` |

---

## 4. Observability & Tracing Contract

To support runtime evaluation and offline diagnosis by the self-repair platform, the agent emits structured trace events. Every event includes common correlation IDs.

### Common Trace Event Envelope: `AgentTraceEvent`
```typescript
interface AgentTraceEvent {
  case_id: string;
  run_id: string;
  agent_version: string;
  timestamp: string;             // ISO-8601
  step_index: number;            // 1-based sequential reasoning turn
  event_type: "STEP_START" | "PLANNING" | "TOOL_CALL" | "OBSERVATION" | "SAFE_STOP" | "STEP_END";
  payload: Record<string, any>;
}
```

### Event Types & Payloads
1. **`PLANNING`**:
   - `payload.thought`: Summary of agent rationale (e.g., `"Customer did not provide order ID. Must halt tool execution and request ID."`).
   - `payload.action_intent`: Planned next action (`CALL_TOOL`, `ASK_USER`, `FINALIZE`).
2. **`TOOL_CALL`**:
   - `tool_name`: Name of tool invoked.
   - `payload.tool_name`, `payload.call_id`, `payload.parameters`.
3. **`OBSERVATION`**:
   - `tool_name`: Associated tool.
   - `payload.status`: `"SUCCESS"` or `"ERROR"`.
   - `payload.data` or `payload.error` (contains `error_code` and `message`).
4. **`SAFE_STOP`**:
   - `payload.stop_reason`: e.g., `"NEED_USER_INPUT"`, `"UNRESOLVABLE_CONFLICT"`, `"MAX_STEPS"`.
   - `payload.policy_rule_triggered`: Code or description of the safety guardrail engaged.

---

## 5. Execution Limits (Recommendations for Team Consensus)

To guarantee that benchmark runs execute deterministically without runaway costs or hanging threads, the following limits are established:

| Boundary Parameter | Proposed Default | Recommended Range | Enforcement Behavior |
| :--- | :--- | :--- | :--- |
| **Max Steps (`max_steps`)** | **`4`** | `3 - 6` | Reaching limit triggers `status: "LIMIT_EXCEEDED"`, `stop_reason: "MAX_STEPS"`, and a safe fallback message to user. |
| **Max Tool Calls (`max_tool_calls`)** | **`3`** | `3 - 5` | Exceeding limit aborts further tool dispatches; agent synthesizes response from existing observations or escalates. |
| **Per-Tool Timeout (`tool_timeout_ms`)** | **`2000 ms`** | `1000 - 3000 ms` | Tool cancels synchronously; tool runner returns `error_code: "TIMEOUT_ERROR"`. |
| **Overall Run Timeout (`run_timeout_ms`)** | **`10000 ms`** | `8000 - 15000 ms` | Runner terminates agent execution; returns `status: "TIMEOUT"`, `stop_reason: "TIME_EXCEEDED"`. |

---

## 6. Concrete JSON Examples

### Example A: Agent Result (`AgentResult`)
*Scenario: Successful Ordinary Order Inquiry*
```json
{
  "case_id": "CASE-001-ORD-NORMAL",
  "run_id": "RUN-20261103-104200-01",
  "agent_version": "v1.0.0",
  "status": "SUCCESS",
  "answer": "Your order ORD-10293 is currently SHIPPED. It contains 1x Wireless Keyboard ($49.99). The shipment is in transit with MockPost (Tracking: MP-772910) and is estimated to arrive by 2026-11-05.",
  "stop_reason": "GOAL_REACHED",
  "metrics": {
    "total_steps": 3,
    "total_tool_calls": 3,
    "latency_ms": 482
  }
}
```

### Example B: Tool Result (`ToolCallResult`)
*Scenario: Tool Error (Order Not Found)*
```json
{
  "tool_name": "lookup_order_details",
  "call_id": "call_lookup_001",
  "status": "ERROR",
  "error": {
    "error_code": "ORDER_NOT_FOUND",
    "message": "No order found matching order_id 'ORD-99999'."
  },
  "latency_ms": 25
}
```

---

## 7. Python Signatures for Runner & Tracing

The runner, tracer, agent, and mock tools will rely on the following Python class and type signatures:

```python
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol
from enum import Enum

class AgentStatus(str, Enum):
    SUCCESS = "SUCCESS"
    CLARIFICATION_NEEDED = "CLARIFICATION_NEEDED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"

class StopReason(str, Enum):
    GOAL_REACHED = "GOAL_REACHED"
    NEED_USER_INPUT = "NEED_USER_INPUT"
    UNRESOLVABLE_CONFLICT = "UNRESOLVABLE_CONFLICT"
    ERROR_ABORT = "ERROR_ABORT"
    MAX_STEPS = "MAX_STEPS"
    MAX_TOOL_CALLS = "MAX_TOOL_CALLS"
    TIME_EXCEEDED = "TIME_EXCEEDED"

class EventType(str, Enum):
    STEP_START = "STEP_START"
    PLANNING = "PLANNING"
    TOOL_CALL = "TOOL_CALL"
    OBSERVATION = "OBSERVATION"
    SAFE_STOP = "SAFE_STOP"
    STEP_END = "STEP_END"

@dataclass
class AgentInput:
    case_id: str
    run_id: str
    agent_version: str
    user_message: str
    customer_id: Optional[str] = None
    context: Optional[Dict[str, Any]] = None

@dataclass
class ToolCallResult:
    tool_name: str
    call_id: str
    status: str  # "SUCCESS" | "ERROR"
    data: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, str]] = None  # {"error_code": ..., "message": ...}
    latency_ms: float = 0.0

@dataclass
class AgentResult:
    case_id: str
    run_id: str
    agent_version: str
    status: AgentStatus
    answer: str
    stop_reason: StopReason
    metrics: Dict[str, Any] = field(default_factory=dict)
    error: Optional[Dict[str, Any]] = None

class Tracer(Protocol):
    def emit_event(
        self,
        case_id: str,
        run_id: str,
        agent_version: str,
        step_index: number,
        event_type: EventType,
        payload: Dict[str, Any],
        tool_name: Optional[str] = None,
    ) -> None:
        ...

class MockTool(Protocol):
    name: str
    def execute(self, parameters: Dict[str, Any], call_id: str) -> ToolCallResult:
        ...

class SupportAgent(Protocol):
    version: str
    def run(self, input_data: AgentInput, tools: Dict[str, MockTool], tracer: Tracer) -> AgentResult:
        ...

class AgentRunner(Protocol):
    def run_case(self, case: AgentInput, agent: SupportAgent, config_overrides: Optional[Dict[str, Any]] = None) -> AgentResult:
        ...
```

---

## 8. Alignment, Conflicts with Day 1, and Team Decisions

### Alignment & Refinements to Day 1 Design
1. **Error Codes Formalized:** Day 1 introduced general errors; Day 2 formalizes exact uppercase strings across all tools (`ORDER_NOT_FOUND`, `INVALID_INPUT`, `TIMEOUT_ERROR`, `INTERNAL_ERROR`, `PAYLOAD_CORRUPT`, `TRACKING_NOT_FOUND`, `CARRIER_UNAVAILABLE`).
2. **Standardized Event Types:** Day 1 mentioned observing thought steps and tool calls; Day 2 maps them to concrete `event_type` enum values (`PLANNING`, `TOOL_CALL`, `OBSERVATION`, `SAFE_STOP`).
3. **Status Taxonomy Refinement:** Added `CLARIFICATION_NEEDED` to `AgentStatus` so the evaluation harness can distinguish between a successful final answer and an intentional pause for missing user input.

### Open Questions for Team Resolution
1. **Multi-turn Context vs Single-turn Stateless:** Does the Day 3 runner test each scenario as a single-turn question-answer (evaluating whether the agent appropriately outputs `CLARIFICATION_NEEDED`), or will the runner simulate a second turn where the customer provides the missing ID?  
   *Recommendation:* Start with single-turn verification for Day 3 benchmarks.
2. **Tracer Output Destination:** Should the `Tracer` emit NDJSON (newline-delimited JSON) directly to `logs/{run_id}.jsonl` on disk, or pass events in-memory to an accumulator list inside `AgentRunner`?  
   *Recommendation:* Stream to both an in-memory accumulator (for runner evaluation assertions) and an append-only JSONL file for observability dashboarding.
3. **Mock Data Injection Mechanism:** Should error states (like timeouts and payload corruption for scenarios 4 & 5) be triggered by specific reserved order IDs (e.g., `ORD-TIMEOUT-001`, `ORD-CORRUPT-002`) or via test runner configuration flags?  
   *Recommendation:* Use reserved fixture IDs in the synthetic JSON database to keep tools stateless and deterministic.

