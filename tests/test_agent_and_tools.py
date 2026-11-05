import unittest
from typing import List

from src.models import (
    AgentConfig,
    AgentInput,
    AgentStatus,
    EventType,
    ObservableEvent,
    StopReason,
    get_default_config,
    get_baseline_fast_fail_config,
)
from src.tools import (
    OrderLookupTool,
    ShippingPolicyLookupTool,
    ResponseValidationTool,
    SYNTHETIC_ORDERS,
)
from src.agent import SupportAgent


class TestSupportAgentAndTools(unittest.TestCase):

    def setUp(self):
        self.events: List[ObservableEvent] = []

        def callback(event: ObservableEvent):
            self.events.append(event)

        self.callback = callback
        self.agent = SupportAgent(event_callback=self.callback)

    # -------------------------------------------------------------------------
    # 1. Tools Unit Tests
    # -------------------------------------------------------------------------
    def test_order_lookup_success(self):
        tool = OrderLookupTool()
        result = tool.execute({"order_id": "ORD-10001"}, call_id="c1")
        self.assertEqual(result.status, "SUCCESS")
        self.assertIsNotNone(result.data)
        self.assertEqual(result.data["order_id"], "ORD-10001")
        self.assertEqual(result.data["status"], "PROCESSING")

    def test_order_lookup_unknown(self):
        tool = OrderLookupTool()
        result = tool.execute({"order_id": "ORD-NONEXISTENT"}, call_id="c2")
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(result.error["error_code"], "ORDER_NOT_FOUND")

    def test_order_lookup_permanent_timeout(self):
        tool = OrderLookupTool()
        result = tool.execute({"order_id": "ORD-TIMEOUT"}, call_id="c3")
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(result.error["error_code"], "TIMEOUT_ERROR")

    def test_order_lookup_transient_timeout_recovery(self):
        tool = OrderLookupTool()
        # 1st call fails
        res1 = tool.execute({"order_id": "ORD-TIMEOUT-TRANSIENT"}, call_id="c3a")
        self.assertEqual(res1.status, "ERROR")
        self.assertEqual(res1.error["error_code"], "TIMEOUT_ERROR")
        # 2nd call succeeds
        res2 = tool.execute({"order_id": "ORD-TIMEOUT-TRANSIENT"}, call_id="c3b")
        self.assertEqual(res2.status, "SUCCESS")
        self.assertEqual(res2.data["order_id"], "ORD-TIMEOUT-TRANSIENT")

    def test_shipping_policy_lookup_success(self):
        tool = ShippingPolicyLookupTool()
        result = tool.execute({"shipping_method": "standard"}, call_id="c4")
        self.assertEqual(result.status, "SUCCESS")
        self.assertEqual(result.data["carrier"], "StandardPost")

    def test_shipping_policy_lookup_unknown(self):
        tool = ShippingPolicyLookupTool()
        result = tool.execute({"shipping_method": "teleportation"}, call_id="c5")
        self.assertEqual(result.status, "ERROR")
        self.assertEqual(result.error["error_code"], "POLICY_NOT_FOUND")

    def test_response_validation_tool(self):
        tool = ResponseValidationTool()
        # Safe response
        res1 = tool.execute({"response_text": "Your package is on its way."}, call_id="c6")
        self.assertEqual(res1.status, "SUCCESS")
        self.assertTrue(res1.data["is_valid"])

        # Unsafe response with simulated write action
        res2 = tool.execute({"response_text": "I refunded your card."}, call_id="c7")
        self.assertEqual(res2.status, "SUCCESS")
        self.assertFalse(res2.data["is_valid"])

    # -------------------------------------------------------------------------
    # 2. Agent Normal & Six Planned Situations
    # -------------------------------------------------------------------------
    def test_situation_ordinary_order(self):
        inp = AgentInput(
            case_id="CASE-001",
            run_id="RUN-001",
            agent_version="v1.0.0",
            user_message="Hello, can you check the status of my order ORD-10001?",
        )
        res = self.agent.run(inp)

        self.assertEqual(res.status, AgentStatus.SUCCESS)
        self.assertEqual(res.stop_reason, StopReason.GOAL_REACHED)
        self.assertEqual(res.agent_version, "v1.0.0")
        self.assertEqual(res.case_id, "CASE-001")
        self.assertIn("ORD-10001", res.answer)
        self.assertIn("PROCESSING", res.answer)
        self.assertIn("Wireless Keyboard", res.answer)

        # Verify observable tracing events were emitted including timestamps
        event_types = [e.event_type for e in self.events]
        self.assertIn(EventType.PLANNING, event_types)
        self.assertIn(EventType.TOOL_CALL, event_types)
        self.assertIn(EventType.TOOL_RESULT, event_types)
        self.assertIn(EventType.OBSERVATION, event_types)
        self.assertIn(EventType.SAFE_STOP, event_types)
        self.assertIn(EventType.FINAL_STATUS, event_types)
        for ev in self.events:
            self.assertIsNotNone(ev.timestamp)
            self.assertEqual(ev.case_id, "CASE-001")
            self.assertEqual(ev.agent_version, "v1.0.0")

    def test_situation_missing_order_id(self):
        inp = AgentInput(
            case_id="CASE-002",
            run_id="RUN-002",
            agent_version="v1.0.0",
            user_message="Where is my shipment? It hasn't arrived.",
        )
        res = self.agent.run(inp)

        self.assertEqual(res.status, AgentStatus.CLARIFICATION_NEEDED)
        self.assertEqual(res.stop_reason, StopReason.NEED_USER_INPUT)
        self.assertEqual(res.error_code, "MISSING_ORDER_ID")
        self.assertIn("Order ID", res.answer)

        # Safe behavior: zero tool calls should have been made
        self.assertEqual(res.metrics["total_tool_calls"], 0)

        # Event emission check
        event_types = [e.event_type for e in self.events]
        self.assertIn(EventType.PLANNING, event_types)
        self.assertIn(EventType.ERROR, event_types)
        self.assertIn(EventType.SAFE_STOP, event_types)

    def test_situation_unknown_order(self):
        inp = AgentInput(
            case_id="CASE-003",
            run_id="RUN-003",
            agent_version="v1.0.0",
            user_message="Status of ORD-99999 please.",
        )
        res = self.agent.run(inp)

        self.assertEqual(res.status, AgentStatus.ERROR)
        self.assertEqual(res.stop_reason, StopReason.ERROR_ABORT)
        self.assertEqual(res.error_code, "ORDER_NOT_FOUND")
        self.assertIn("could not find an order matching 'ORD-99999'", res.answer)

        # Event emission check
        event_types = [e.event_type for e in self.events]
        self.assertIn(EventType.ERROR, event_types)

    def test_situation_incomplete_tool_result(self):
        inp = AgentInput(
            case_id="CASE-004",
            run_id="RUN-004",
            agent_version="v1.0.0",
            user_message="Check order ORD-INCOMPLETE.",
        )
        res = self.agent.run(inp)

        self.assertEqual(res.status, AgentStatus.ERROR)
        self.assertEqual(res.stop_reason, StopReason.ERROR_ABORT)
        self.assertEqual(res.error_code, "INCOMPLETE_TOOL_RESULT")
        self.assertIn("incomplete in our system", res.answer)

    def test_situation_tool_timeout(self):
        # ORD-TIMEOUT triggers permanent simulated timeout in OrderLookupTool
        inp = AgentInput(
            case_id="CASE-005",
            run_id="RUN-005",
            agent_version="v1.0.0",
            user_message="Check order ORD-TIMEOUT please.",
            context={"config_overrides": {"retry_count": 0}},
        )
        res = self.agent.run(inp)

        self.assertEqual(res.status, AgentStatus.TIMEOUT)
        self.assertEqual(res.stop_reason, StopReason.TIME_EXCEEDED)
        self.assertEqual(res.error_code, "TIMEOUT_ERROR")
        self.assertIn("temporarily delayed", res.answer)

    def test_situation_conflicting_information(self):
        inp = AgentInput(
            case_id="CASE-006",
            run_id="RUN-006",
            agent_version="v1.0.0",
            user_message="What is happening with ORD-CONFLICT?",
        )
        res = self.agent.run(inp)

        self.assertEqual(res.status, AgentStatus.ERROR)
        self.assertEqual(res.stop_reason, StopReason.UNRESOLVABLE_CONFLICT)
        self.assertEqual(res.error_code, "CONFLICTING_INFORMATION")
        self.assertIn("discrepancy", res.answer)

    def test_situation_unsupported_write_action(self):
        inp = AgentInput(
            case_id="CASE-007",
            run_id="RUN-007",
            agent_version="v1.0.0",
            user_message="Please cancel my order ORD-10001 immediately.",
        )
        res = self.agent.run(inp)

        self.assertEqual(res.status, AgentStatus.SUCCESS)
        self.assertEqual(res.stop_reason, StopReason.UNSUPPORTED_REQUEST)
        self.assertIn("read-only support assistant", res.answer)
        self.assertIn("cannot cancel orders", res.answer)
        self.assertEqual(res.metrics["total_tool_calls"], 0)

    # -------------------------------------------------------------------------
    # 3. Execution Limits and Safe Stopping
    # -------------------------------------------------------------------------
    def test_step_limit_enforcement(self):
        custom_config = AgentConfig(max_steps=1)
        agent = SupportAgent(config=custom_config)
        inp = AgentInput(
            case_id="CASE-008",
            run_id="RUN-008",
            agent_version="v1.0.0",
            user_message="Status of ORD-10001",
        )
        res = agent.run(inp)
        self.assertEqual(res.status, AgentStatus.LIMIT_EXCEEDED)
        self.assertEqual(res.stop_reason, StopReason.MAX_STEPS)
        self.assertEqual(res.error_code, "MAX_STEPS_EXCEEDED")

    def test_tool_call_limit_enforcement(self):
        custom_config = AgentConfig(max_tool_calls=0)
        agent = SupportAgent(config=custom_config)
        inp = AgentInput(
            case_id="CASE-009",
            run_id="RUN-009",
            agent_version="v1.0.0",
            user_message="Status of ORD-10001",
        )
        res = agent.run(inp)
        self.assertEqual(res.status, AgentStatus.LIMIT_EXCEEDED)
        self.assertEqual(res.stop_reason, StopReason.MAX_TOOL_CALLS)
        self.assertEqual(res.error_code, "MAX_TOOL_CALLS_EXCEEDED")

    def test_run_time_limit_enforcement(self):
        # 0.0 ms run timeout triggers immediate TIME_EXCEEDED
        custom_config = AgentConfig(run_timeout_ms=0.0)
        agent = SupportAgent(config=custom_config)
        inp = AgentInput(
            case_id="CASE-010",
            run_id="RUN-010",
            agent_version="v1.0.0",
            user_message="Status of ORD-10001",
        )
        res = agent.run(inp)
        self.assertEqual(res.status, AgentStatus.TIMEOUT)
        self.assertEqual(res.stop_reason, StopReason.TIME_EXCEEDED)
        self.assertEqual(res.error_code, "TIMEOUT_ERROR")

    # -------------------------------------------------------------------------
    # 4. Controlled Second Agent Configuration (Baseline Comparison)
    # -------------------------------------------------------------------------
    def test_controlled_second_agent_configuration_behavior_difference(self):
        """
        Compares default resilient agent (v1.0.0, retry_count=1)
        vs. controlled second agent baseline (v0.9.0-fastfail, retry_count=0).
        When encountering transient tool timeouts (ORD-TIMEOUT-TRANSIENT):
        - v1.0.0 automatically retries, recovers, and returns SUCCESS.
        - v0.9.0-fastfail fails immediately without retry and returns TIMEOUT.
        """
        default_cfg = get_default_config()
        fast_fail_cfg = get_baseline_fast_fail_config()

        self.assertEqual(default_cfg.version, "v1.0.0")
        self.assertEqual(fast_fail_cfg.version, "v0.9.0-fastfail")
        self.assertEqual(default_cfg.retry_count, 1)
        self.assertEqual(fast_fail_cfg.retry_count, 0)

        # 1. Run baseline agent (v0.9.0-fastfail) on transient timeout order
        fast_fail_tool = OrderLookupTool()
        fast_fail_agent = SupportAgent(
            config=fast_fail_cfg,
            tools={"order_lookup": fast_fail_tool, "shipping_policy_lookup": ShippingPolicyLookupTool(), "response_validation": ResponseValidationTool()}
        )
        inp_fast_fail = AgentInput(
            case_id="CASE-BASELINE-COMPARE",
            run_id="RUN-FASTFAIL-001",
            agent_version=fast_fail_cfg.version,
            user_message="Status of ORD-TIMEOUT-TRANSIENT please.",
        )
        res_fast_fail = fast_fail_agent.run(inp_fast_fail)

        self.assertEqual(res_fast_fail.status, AgentStatus.TIMEOUT)
        self.assertEqual(res_fast_fail.stop_reason, StopReason.TIME_EXCEEDED)
        self.assertEqual(res_fast_fail.agent_version, "v0.9.0-fastfail")
        self.assertEqual(res_fast_fail.metrics["total_tool_calls"], 1)

        # 2. Run resilient default agent (v1.0.0) on fresh transient timeout tool
        resilient_tool = OrderLookupTool()
        resilient_agent = SupportAgent(
            config=default_cfg,
            tools={"order_lookup": resilient_tool, "shipping_policy_lookup": ShippingPolicyLookupTool(), "response_validation": ResponseValidationTool()}
        )
        inp_resilient = AgentInput(
            case_id="CASE-BASELINE-COMPARE",
            run_id="RUN-RESILIENT-001",
            agent_version=default_cfg.version,
            user_message="Status of ORD-TIMEOUT-TRANSIENT please.",
        )
        res_resilient = resilient_agent.run(inp_resilient)

        self.assertEqual(res_resilient.status, AgentStatus.SUCCESS)
        self.assertEqual(res_resilient.stop_reason, StopReason.GOAL_REACHED)
        self.assertEqual(res_resilient.agent_version, "v1.0.0")
        self.assertGreater(res_resilient.metrics["total_tool_calls"], 1)
        self.assertIn("ORD-TIMEOUT-TRANSIENT", res_resilient.answer)


if __name__ == "__main__":
    unittest.main()
