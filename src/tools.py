from typing import Any, Dict, Optional
from src.models import ToolCallResult

# Synthetic in-memory database
SYNTHETIC_ORDERS: Dict[str, Dict[str, Any]] = {
    # Ordinary complete order
    "ORD-10001": {
        "order_id": "ORD-10001",
        "customer_id": "CUST-101",
        "status": "PROCESSING",
        "shipping_method": "standard",
        "created_at": "2026-11-01T10:00:00Z",
        "items": [
            {"sku": "SKU-KB-01", "name": "Wireless Keyboard", "quantity": 1, "unit_price": 49.99}
        ],
        "total_amount": 49.99,
        "currency": "USD"
    },
    "ORD-10002": {
        "order_id": "ORD-10002",
        "customer_id": "CUST-102",
        "status": "SHIPPED",
        "shipping_method": "express",
        "created_at": "2026-10-31T09:30:00Z",
        "items": [
            {"sku": "SKU-MO-02", "name": "Ergonomic Mouse", "quantity": 2, "unit_price": 25.00}
        ],
        "total_amount": 50.00,
        "currency": "USD"
    },
    # Incomplete result fixture: missing required status & shipping_method fields
    "ORD-INCOMPLETE": {
        "order_id": "ORD-INCOMPLETE",
        "customer_id": "CUST-103",
        # Missing "status"
        # Missing "shipping_method"
        "total_amount": 19.99,
        "currency": "USD"
    },
    # Conflicting information fixture: order lookup claims DELIVERED with standard shipping
    "ORD-CONFLICT": {
        "order_id": "ORD-CONFLICT",
        "customer_id": "CUST-104",
        "status": "DELIVERED",
        "shipping_method": "standard",
        "created_at": "2026-11-03T08:00:00Z", # ordered today, contradicts standard 3-5 day delivery policy
        "items": [
            {"sku": "SKU-MN-03", "name": "Monitor Stand", "quantity": 1, "unit_price": 35.00}
        ],
        "total_amount": 35.00,
        "currency": "USD",
        "tracking_status": "IN_TRANSIT" # explicitly conflicting with order status DELIVERED
    },
    # Timeout simulation trigger fixture
    "ORD-TIMEOUT": {
        "order_id": "ORD-TIMEOUT",
        "customer_id": "CUST-105"
    }
}

SYNTHETIC_SHIPPING_POLICIES: Dict[str, Dict[str, Any]] = {
    "standard": {
        "shipping_method": "standard",
        "estimated_days_min": 3,
        "estimated_days_max": 5,
        "cost": 5.99,
        "carrier": "StandardPost",
        "policy_description": "Standard ground delivery within 3-5 business days."
    },
    "express": {
        "shipping_method": "express",
        "estimated_days_min": 1,
        "estimated_days_max": 2,
        "cost": 14.99,
        "carrier": "QuickExpress",
        "policy_description": "Expedited 1-2 business day air delivery."
    },
    "overnight": {
        "shipping_method": "overnight",
        "estimated_days_min": 1,
        "estimated_days_max": 1,
        "cost": 24.99,
        "carrier": "OvernightAir",
        "policy_description": "Next business day delivery."
    }
}


class OrderLookupTool:
    """Mock tool to fetch order details deterministically from synthetic fixtures."""
    name: str = "order_lookup"

    def execute(self, parameters: Dict[str, Any], call_id: str, simulated_timeout: bool = False) -> ToolCallResult:
        order_id = parameters.get("order_id")
        if not order_id or not isinstance(order_id, str):
            return ToolCallResult(
                tool_name=self.name,
                call_id=call_id,
                status="ERROR",
                error={"error_code": "INVALID_INPUT", "message": "Missing or invalid 'order_id' parameter."}
            )

        # Trigger simulated timeout if flag is on or reserved order_id is used
        if simulated_timeout or order_id == "ORD-TIMEOUT":
            return ToolCallResult(
                tool_name=self.name,
                call_id=call_id,
                status="ERROR",
                error={"error_code": "TIMEOUT_ERROR", "message": f"Execution exceeded timeout limit for {order_id}."}
            )

        if order_id not in SYNTHETIC_ORDERS:
            return ToolCallResult(
                tool_name=self.name,
                call_id=call_id,
                status="ERROR",
                error={"error_code": "ORDER_NOT_FOUND", "message": f"No order found matching order_id '{order_id}'."}
            )

        order_data = SYNTHETIC_ORDERS[order_id]
        return ToolCallResult(
            tool_name=self.name,
            call_id=call_id,
            status="SUCCESS",
            data=dict(order_data)
        )


class ShippingPolicyLookupTool:
    """Mock tool to query shipping policies for a shipping method."""
    name: str = "shipping_policy_lookup"

    def execute(self, parameters: Dict[str, Any], call_id: str, simulated_timeout: bool = False) -> ToolCallResult:
        method = parameters.get("shipping_method")
        if not method or not isinstance(method, str):
            return ToolCallResult(
                tool_name=self.name,
                call_id=call_id,
                status="ERROR",
                error={"error_code": "INVALID_INPUT", "message": "Missing or invalid 'shipping_method' parameter."}
            )

        normalized_method = method.strip().lower()
        if simulated_timeout or normalized_method == "timeout":
            return ToolCallResult(
                tool_name=self.name,
                call_id=call_id,
                status="ERROR",
                error={"error_code": "TIMEOUT_ERROR", "message": "Shipping policy service timed out."}
            )

        if normalized_method not in SYNTHETIC_SHIPPING_POLICIES:
            return ToolCallResult(
                tool_name=self.name,
                call_id=call_id,
                status="ERROR",
                error={"error_code": "POLICY_NOT_FOUND", "message": f"No policy found for method '{method}'."}
            )

        return ToolCallResult(
            tool_name=self.name,
            call_id=call_id,
            status="SUCCESS",
            data=dict(SYNTHETIC_SHIPPING_POLICIES[normalized_method])
        )


class ResponseValidationTool:
    """Mock tool to validate draft customer responses for safety, readability, and policy compliance."""
    name: str = "response_validation"

    def execute(self, parameters: Dict[str, Any], call_id: str, simulated_timeout: bool = False) -> ToolCallResult:
        response_text = parameters.get("response_text")
        if response_text is None or not isinstance(response_text, str):
            return ToolCallResult(
                tool_name=self.name,
                call_id=call_id,
                status="ERROR",
                error={"error_code": "INVALID_INPUT", "message": "Missing 'response_text' parameter."}
            )

        if simulated_timeout:
            return ToolCallResult(
                tool_name=self.name,
                call_id=call_id,
                status="ERROR",
                error={"error_code": "TIMEOUT_ERROR", "message": "Validation service timed out."}
            )

        issues = []
        # Check safety: No raw exceptions or stack traces
        if "traceback" in response_text.lower() or "exception" in response_text.lower():
            issues.append("Contains technical exception or traceback.")
        # Check safety: No mock internal write action claims
        forbidden_phrases = ["refunded your card", "cancelled your order", "updated your address in the database"]
        for phrase in forbidden_phrases:
            if phrase in response_text.lower():
                issues.append(f"Contains unauthorized write action claim: '{phrase}'.")

        is_valid = len(issues) == 0
        return ToolCallResult(
            tool_name=self.name,
            call_id=call_id,
            status="SUCCESS",
            data={
                "is_valid": is_valid,
                "issues": issues,
                "word_count": len(response_text.split()),
                "passed_checks": ["read_only_safety", "no_internal_stack_trace"] if is_valid else []
            }
        )

