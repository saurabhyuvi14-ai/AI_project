import re
import time
from typing import Any, Dict, List, Optional

from src.models import (
    AgentConfig,
    AgentInput,
    AgentResult,
    AgentStatus,
    EventCallback,
    EventType,
    ObservableEvent,
    StopReason,
    ToolCallResult,
)
from src.tools import OrderLookupTool, ResponseValidationTool, ShippingPolicyLookupTool

# Regex to safely extract Order ID like ORD-10001, ORD-ABCDE, etc.
ORDER_ID_REGEX = re.compile(r"\b(ORD-[A-Za-z0-9_-]+)\b", re.IGNORECASE)

# Keywords indicating unsupported write actions
WRITE_INTENT_KEYWORDS = [
    "cancel my order",
    "cancel order",
    "refund me",
    "process refund",
    "change address",
    "update shipping address",
    "modify items",
    "delete order"
]


class SupportAgent:
    """
    Deterministic shop-support agent with configurable limits,
    observable tracing callbacks, and safety guardrails.
    """

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        tools: Optional[Dict[str, Any]] = None,
        event_callback: Optional[EventCallback] = None,
    ):
        self.config = config or AgentConfig()
        self.tools = tools or {
            "order_lookup": OrderLookupTool(),
            "shipping_policy_lookup": ShippingPolicyLookupTool(),
            "response_validation": ResponseValidationTool(),
        }
        self.event_callback = event_callback

    def _emit(
        self,
        input_data: AgentInput,
        event_type: EventType,
        step_index: int,
        payload: Dict[str, Any],
        tool_name: Optional[str] = None,
    ) -> None:
        if self.event_callback:
            event = ObservableEvent(
                case_id=input_data.case_id,
                run_id=input_data.run_id,
                agent_version=input_data.agent_version,
                event_type=event_type,
                step_index=step_index,
                payload=payload,
                tool_name=tool_name,
            )
            self.event_callback(event)

    def _call_tool(
        self,
        tool_name: str,
        parameters: Dict[str, Any],
        call_id: str,
        step_index: int,
        input_data: AgentInput,
        simulated_timeout: bool = False,
    ) -> ToolCallResult:
        self._emit(
            input_data,
            EventType.TOOL_CALL,
            step_index,
            {"call_id": call_id, "parameters": parameters},
            tool_name=tool_name,
        )

        tool = self.tools.get(tool_name)
        if not tool:
            res = ToolCallResult(
                tool_name=tool_name,
                call_id=call_id,
                status="ERROR",
                error={"error_code": "TOOL_NOT_FOUND", "message": f"Tool '{tool_name}' not available."},
            )
        else:
            # Respect mock tool execution
            res = tool.execute(parameters, call_id=call_id, simulated_timeout=simulated_timeout)

        # Emit tool result & observation
        self._emit(
            input_data,
            EventType.TOOL_RESULT,
            step_index,
            {"call_id": call_id, "status": res.status, "data": res.data, "error": res.error},
            tool_name=tool_name,
        )
        self._emit(
            input_data,
            EventType.OBSERVATION,
            step_index,
            {"tool_name": tool_name, "status": res.status, "has_data": bool(res.data)},
            tool_name=tool_name,
        )
        return res

    def run(self, input_data: AgentInput) -> AgentResult:
        start_time = time.time()
        step_index = 0
        total_tool_calls = 0

        # Merge config overrides from input context if present
        cfg = self.config
        if input_data.context and "config_overrides" in input_data.context:
            overrides = input_data.context["config_overrides"]
            cfg = AgentConfig(
                max_steps=overrides.get("max_steps", cfg.max_steps),
                max_tool_calls=overrides.get("max_tool_calls", cfg.max_tool_calls),
                tool_timeout_ms=overrides.get("tool_timeout_ms", cfg.tool_timeout_ms),
                run_timeout_ms=overrides.get("run_timeout_ms", cfg.run_timeout_ms),
                retry_count=overrides.get("retry_count", cfg.retry_count),
                clarification_prompt=overrides.get("clarification_prompt", cfg.clarification_prompt),
            )

        def elapsed_ms() -> float:
            return (time.time() - start_time) * 1000.0

        def check_time_limit() -> bool:
            return elapsed_ms() >= cfg.run_timeout_ms

        def finalize(
            status: AgentStatus,
            answer: str,
            stop_reason: StopReason,
            error_code: Optional[str] = None,
            error_details: Optional[Dict[str, Any]] = None,
        ) -> AgentResult:
            latency = elapsed_ms()
            result = AgentResult(
                case_id=input_data.case_id,
                run_id=input_data.run_id,
                agent_version=input_data.agent_version,
                status=status,
                answer=answer,
                stop_reason=stop_reason,
                metrics={
                    "total_steps": step_index,
                    "total_tool_calls": total_tool_calls,
                    "latency_ms": round(latency, 2),
                },
                error_code=error_code,
                error={"error_code": error_code, "details": error_details} if error_code else None,
            )
            self._emit(
                input_data,
                EventType.SAFE_STOP,
                step_index,
                {"stop_reason": stop_reason, "status": status, "error_code": error_code},
            )
            self._emit(
                input_data,
                EventType.FINAL_STATUS,
                step_index,
                {"status": status, "answer": answer, "metrics": result.metrics},
            )
            return result

        # Check total run timeout immediately
        if check_time_limit():
            return finalize(
                AgentStatus.TIMEOUT,
                "Request exceeded the maximum allotted processing time.",
                StopReason.TIME_EXCEEDED,
                error_code="TIMEOUT_ERROR",
            )

        # ----------------------------------------------------
        # Step 1: Input Analysis & Guardrails (Plan & Intent)
        # ----------------------------------------------------
        step_index += 1
        msg_lower = input_data.user_message.lower()

        # Check unsupported write requests (Safety guardrail)
        if any(keyword in msg_lower for keyword in WRITE_INTENT_KEYWORDS):
            self._emit(
                input_data,
                EventType.PLANNING,
                step_index,
                {"thought": "Detected unsupported write/mutation intent. Halting automated execution safely."},
            )
            return finalize(
                AgentStatus.SUCCESS,
                "I am a read-only support assistant and cannot cancel orders, issue refunds, or change addresses directly. "
                "Please contact our human support team at support@example.com for order modifications.",
                StopReason.UNSUPPORTED_REQUEST,
            )

        # Detect Order ID
        match = ORDER_ID_REGEX.search(input_data.user_message)
        extracted_order_id = match.group(1).upper() if match else None

        if not extracted_order_id:
            # Situation: Missing Order ID
            self._emit(
                input_data,
                EventType.PLANNING,
                step_index,
                {"thought": "No order ID found in user inquiry. Prompting user for clarification."},
            )
            return finalize(
                AgentStatus.CLARIFICATION_NEEDED,
                cfg.clarification_prompt,
                StopReason.NEED_USER_INPUT,
                error_code="MISSING_ORDER_ID",
            )

        # ----------------------------------------------------
        # Step 2: Tool Execution - Order Lookup
        # ----------------------------------------------------
        step_index += 1
        if step_index > cfg.max_steps:
            return finalize(AgentStatus.LIMIT_EXCEEDED, "Step limit exceeded.", StopReason.MAX_STEPS, error_code="MAX_STEPS_EXCEEDED")

        self._emit(
            input_data,
            EventType.PLANNING,
            step_index,
            {"thought": f"Order ID '{extracted_order_id}' identified. Executing order_lookup tool."},
        )

        total_tool_calls += 1
        if total_tool_calls > cfg.max_tool_calls:
            return finalize(AgentStatus.LIMIT_EXCEEDED, "Tool call limit exceeded.", StopReason.MAX_TOOL_CALLS, error_code="MAX_TOOL_CALLS_EXCEEDED")

        order_res = self._call_tool(
            "order_lookup",
            {"order_id": extracted_order_id},
            f"call_order_{total_tool_calls}",
            step_index,
            input_data,
        )

        # Handle Order Lookup Errors (Unknown Order, Timeout, etc.)
        if order_res.status == "ERROR":
            err_code = order_res.error.get("error_code") if order_res.error else "UNKNOWN_ERROR"
            
            # Retry logic if configured
            if err_code == "TIMEOUT_ERROR" and cfg.retry_count > 0:
                retries_left = cfg.retry_count
                while retries_left > 0 and order_res.status == "ERROR":
                    retries_left -= 1
                    total_tool_calls += 1
                    if total_tool_calls > cfg.max_tool_calls:
                        return finalize(AgentStatus.LIMIT_EXCEEDED, "Tool call limit exceeded during retry.", StopReason.MAX_TOOL_CALLS, error_code="MAX_TOOL_CALLS_EXCEEDED")
                    order_res = self._call_tool(
                        "order_lookup",
                        {"order_id": extracted_order_id},
                        f"call_order_retry_{total_tool_calls}",
                        step_index,
                        input_data,
                    )

            if order_res.status == "ERROR":
                err_code = order_res.error.get("error_code") if order_res.error else "UNKNOWN_ERROR"
                if err_code == "ORDER_NOT_FOUND":
                    return finalize(
                        AgentStatus.ERROR,
                        f"I could not find an order matching '{extracted_order_id}'. Please double check the ID and try again.",
                        StopReason.ERROR_ABORT,
                        error_code="ORDER_NOT_FOUND",
                    )
                elif err_code == "TIMEOUT_ERROR":
                    return finalize(
                        AgentStatus.TIMEOUT,
                        "Our order lookup service is temporarily delayed. Please try your request again in a few moments.",
                        StopReason.TIME_EXCEEDED,
                        error_code="TIMEOUT_ERROR",
                    )
                else:
                    return finalize(
                        AgentStatus.ERROR,
                        "An unexpected service error occurred while retrieving order details.",
                        StopReason.ERROR_ABORT,
                        error_code=err_code,
                    )

        order_data = order_res.data or {}

        # ----------------------------------------------------
        # Step 3: Check for Incomplete Tool Result
        # ----------------------------------------------------
        required_fields = ["status", "shipping_method"]
        missing_fields = [f for f in required_fields if f not in order_data or order_data[f] is None]
        if missing_fields:
            self._emit(
                input_data,
                EventType.PLANNING,
                step_index,
                {"thought": f"Order data is incomplete; missing required fields: {missing_fields}."},
            )
            return finalize(
                AgentStatus.ERROR,
                f"Order '{extracted_order_id}' was located, but some fulfillment details ({', '.join(missing_fields)}) are currently incomplete in our system. Please check back shortly.",
                StopReason.ERROR_ABORT,
                error_code="INCOMPLETE_TOOL_RESULT",
                error_details={"missing_fields": missing_fields},
            )

        # ----------------------------------------------------
        # Step 4: Tool Execution - Shipping Policy Lookup
        # ----------------------------------------------------
        step_index += 1
        if step_index > cfg.max_steps:
            return finalize(AgentStatus.LIMIT_EXCEEDED, "Step limit exceeded.", StopReason.MAX_STEPS, error_code="MAX_STEPS_EXCEEDED")

        shipping_method = order_data.get("shipping_method")
        self._emit(
            input_data,
            EventType.PLANNING,
            step_index,
            {"thought": f"Querying shipping policy for method '{shipping_method}'."},
        )

        total_tool_calls += 1
        if total_tool_calls > cfg.max_tool_calls:
            return finalize(AgentStatus.LIMIT_EXCEEDED, "Tool call limit exceeded.", StopReason.MAX_TOOL_CALLS, error_code="MAX_TOOL_CALLS_EXCEEDED")

        policy_res = self._call_tool(
            "shipping_policy_lookup",
            {"shipping_method": shipping_method},
            f"call_policy_{total_tool_calls}",
            step_index,
            input_data,
        )

        policy_data = policy_res.data or {}

        # ----------------------------------------------------
        # Step 5: Check for Conflicting Evidence
        # ----------------------------------------------------
        order_status = order_data.get("status")
        tracking_status = order_data.get("tracking_status")

        # Conflict 1: Explicit tracking_status vs order_status mismatch
        if tracking_status and order_status == "DELIVERED" and tracking_status != "DELIVERED":
            self._emit(
                input_data,
                EventType.PLANNING,
                step_index,
                {"thought": f"Conflicting information detected: order says {order_status} but tracking says {tracking_status}."},
            )
            return finalize(
                AgentStatus.ERROR,
                f"We noticed a discrepancy in records for order '{extracted_order_id}': the order status shows '{order_status}', "
                f"but shipment tracking shows '{tracking_status}'. Our records are updating; please reach out to human support if urgent.",
                StopReason.UNRESOLVABLE_CONFLICT,
                error_code="CONFLICTING_INFORMATION",
                error_details={"order_status": order_status, "tracking_status": tracking_status},
            )

        # ----------------------------------------------------
        # Step 6: Synthesis & Response Validation
        # ----------------------------------------------------
        carrier = policy_data.get("carrier", "Carrier")
        est_days = f"{policy_data.get('estimated_days_min', 1)}-{policy_data.get('estimated_days_max', 5)} business days"
        items_summary = ", ".join(f"{it['quantity']}x {it['name']}" for it in order_data.get("items", [])) or "Items"

        draft_answer = (
            f"Order {extracted_order_id} is currently {order_status}. "
            f"It contains {items_summary} totaling ${order_data.get('total_amount', 0.0):.2f}. "
            f"It is shipped via {carrier} ({shipping_method.title()} Shipping, estimated {est_days})."
        )

        # Validate response safely with response_validation tool if limits allow
        if total_tool_calls < cfg.max_tool_calls and step_index < cfg.max_steps:
            step_index += 1
            total_tool_calls += 1
            val_res = self._call_tool(
                "response_validation",
                {"response_text": draft_answer},
                f"call_validate_{total_tool_calls}",
                step_index,
                input_data,
            )
            if val_res.status == "SUCCESS" and not val_res.data.get("is_valid"):
                draft_answer = f"Order {extracted_order_id} is {order_status}."

        return finalize(AgentStatus.SUCCESS, draft_answer, StopReason.GOAL_REACHED)

