# Day 1 Design Document: Support Agent & Mock Tools

**Project:** AI Agent Evaluation, Observability, and Safe Self-Repair Platform  
**Scope:** Customer Support Agent Component & Simulated Shop Mock Tools  
**Status:** Draft / Day 1 Proposal (No implementation code yet)

---

## 1. Support Agent Goal & Expected Output

### Goal
The primary objective of the Support Agent is to resolve customer inquiries regarding orders in a simulated e-commerce environment safely, accurately, and deterministically. The agent must strictly act as a read-only support assistant, inspecting order states and shipment tracking information without taking direct write actions (e.g., no payment deductions, order cancellations, or database modifications).

### Expected Output
When replying to a user inquiry, the agent's response must be polite, concise, and structured. Depending on the scenario, it should return:
- **Order Status Summary:** Order ID, current lifecycle status (`PENDING`, `PROCESSING`, `SHIPPED`, `DELIVERED`, `CANCELLED`), and timestamp of last status update.
- **Itemized Details:** List of purchased items (item name, quantity, price) and total order amount.
- **Fulfillment / Delivery Info:** Tracking number, assigned courier service, estimated delivery date, and current transit status.
- **Actionable Clarification / Next Steps:** Clear instructions or questions for the customer if an order ID is missing or unknown, avoiding internal technical jargon or raw error dumps.

---

## 2. Six Planned Situations Handling

The agent must handle the following six core evaluation scenarios:

| Scenario | Trigger / Description | Agent Expected Behavior |
| :--- | :--- | :--- |
| **1. Ordinary Order** | Customer provides a valid `order_id` present in the synthetic database. All tools respond successfully. | Retrieves order and shipment records; synthesizes and presents complete order status and delivery tracking clearly. |
| **2. Missing Order ID** | Customer asks about order status or package arrival without specifying an `order_id`. | Detects missing identifier before calling tools; pauses tool usage and politely prompts the user for their Order ID (or registered email address). |
| **3. Unknown Order** | Customer provides an `order_id` that does not match any entry in the mock system. | Order lookup returns an `ORDER_NOT_FOUND` error; agent acknowledges the error safely, double-checks the format, and asks user to verify the ID without hallucinations. |
| **4. Incomplete Tool Result** | A tool succeeds partially but returns payload with null/missing essential fields (e.g., missing tracking status or empty item list). | Agent detects missing fields against the schema, explains specifically which detail is currently unavailable, displays available verified info, and advises re-checking later. |
| **5. Tool Timeout** | A mock tool simulation experiences high latency or hangs past configured execution limits. | Tool call times out cleanly, triggering mock retry or fallback; agent reports a temporary service delay without crashing or infinite re-polling, offering to retry. |
| **6. Conflicting Information** | Discrepancy between tools (e.g., `OrderService` says status is `DELIVERED`, but `ShippingService` reports status as `IN_TRANSIT` or `PENDING_PICKUP`). | Agent identifies the inconsistency, does not guess which source is right, explains that records are synchronizing, provides both verified statuses with timestamps, and suggests contacting human escalation if urgent. |

---

## 3. Mock Tools Specification

All tools operate over in-memory synthetic fixtures with mockable latency and error injection capabilities.

### Tool 1: `lookup_order_details`
* **Purpose:** Fetches high-level metadata, order lifecycle state, customer ID, and pricing for a specified order.
* **Input Fields:**
  * `order_id` (string, required): Format `ORD-XXXXX`.
* **Output Fields:**
  * `order_id` (string): Unique identifier.
  * `status` (string): One of `PENDING`, `PROCESSING`, `SHIPPED`, `DELIVERED`, `CANCELLED`.
  * `customer_id` (string): ID of customer who placed the order.
  * `created_at` (string): ISO-8601 timestamp.
  * `total_amount` (float): Total bill amount in USD.
  * `currency` (string): E.g., `"USD"`.
* **Possible Errors:**
  * `INVALID_INPUT`: Malformed `order_id` format.
  * `ORDER_NOT_FOUND`: Order does not exist.
  * `TIMEOUT_ERROR`: Simulated latency exceeded limit.
  * `INTERNAL_ERROR`: Simulated transient backend failure.

### Tool 2: `get_order_items`
* **Purpose:** Retrieves line items associated with an order.
* **Input Fields:**
  * `order_id` (string, required): Format `ORD-XXXXX`.
* **Output Fields:**
  * `order_id` (string): Matching order ID.
  * `items` (array of objects):
    * `sku` (string): Stock keeping unit.
    * `name` (string): Product title.
    * `quantity` (integer): Quantity purchased.
    * `unit_price` (float): Price per unit.
* **Possible Errors:**
  * `ORDER_NOT_FOUND`: No item manifest found for specified ID.
  * `PAYLOAD_CORRUPT`: Simulated empty or null item list (for incomplete tool result tests).
  * `TIMEOUT_ERROR`: Simulated latency exceeded limit.

### Tool 3: `track_shipment`
* **Purpose:** Queries carrier and delivery milestones for a given order or tracking number.
* **Input Fields:**
  * `order_id` (string, optional): Order identifier.
  * `tracking_number` (string, optional): Carrier tracking code.
  *(At least one must be provided)*
* **Output Fields:**
  * `tracking_number` (string): Assigned tracking number.
  * `carrier` (string): E.g., `"MockPost"`, `"QuickShip"`.
  * `shipping_status` (string): `LABEL_CREATED`, `PICKED_UP`, `IN_TRANSIT`, `OUT_FOR_DELIVERY`, `DELIVERED`.
  * `estimated_delivery` (string): ISO-8601 date.
  * `last_checkpoint` (string): Location and description of latest scan.
* **Possible Errors:**
  * `TRACKING_NOT_FOUND`: No shipment record found.
  * `CARRIER_UNAVAILABLE`: Mock courier API downtime.
  * `TIMEOUT_ERROR`: Simulated network delay exceeded timeout threshold.

---

## 4. Safe Agent Behavior Guidelines

To ensure deterministic, safe operations and avoid hallucinations:
1. **Missing Information:**
   - The agent must not guess or hallucinate missing identifiers (such as guessing an order ID from the user name).
   - It must halt automated tool loops and ask a direct, courteous clarifying question.
2. **Invalid Information:**
   - Any validation error returned by a mock tool must be translated into user-friendly diagnostic guidance (e.g., "The order ID '123' appears invalid. Typical IDs look like 'ORD-12345'.").
   - Must avoid exposing raw stack traces, SQL, or internal mock system exceptions.
3. **Conflicting Information:**
   - If two tools return contradictory states, the agent must highlight the discrepancy transparently rather than selecting one arbitrarily.
   - Example response: *"Your order record shows 'Delivered', but the tracking carrier status still indicates 'In Transit'. Our records may be updating; please allow 24 hours or let us connect you to human support."*
4. **Strict Read-Only Guarantee:**
   - The agent has no access to state-mutating functions (no refunds, cancellations, address updates). If requested, it refers the user to human intervention.

---

## 5. Execution Limits (Team Recommendations)

To prevent runaway loops, infinite recursion, and denial-of-service in evaluation runs, we propose the following bounds:

| Parameter | Proposed Default | Recommended Range | Description / Rationale |
| :--- | :--- | :--- | :--- |
| **Max Steps (Reasoning Turns)** | `4 steps` | `3 - 6 steps` | Sufficient for standard inspect-and-reply sequence; prevents repetitive thought loops. |
| **Max Tool Calls (Total per run)** | `3 calls` | `3 - 5 calls` | Supports `lookup_order_details` + `get_order_items` + `track_shipment` with at most 1 retry. |
| **Tool Timeout** | `2.0 seconds` | `1.0 - 3.0 seconds` | Synthetic tools should respond instantaneously; >2s triggers timeout handling. |
| **Total Session Timeout** | `10.0 seconds` | `8.0 - 15.0 seconds` | Maximum wall-clock duration for the agent to conclude the user request. |

*(Note: These figures are recommendations for the team to review and ratify.)*

---

## 6. Configurable Settings for Future Safe Autofix

When observability metrics indicate repeated agent failures or anomalies, the safe self-repair platform will test configuration adjustments on an isolated copy before human approval. The parameters eligible for automated repair include:

1. **`retry_count` / `max_retries_per_tool`:**
   - Default: `1`.
   - Autofix adaptation: Adjust to `2` for transient timeout clusters, or `0` if failing fast is preferred.
2. **`tool_timeout_seconds`:**
   - Default: `2.0s`.
   - Autofix adaptation: Increment up to `4.0s` if carrier mock service shows heavy artificial jitter.
3. **`clarification_prompt_template`:**
   - Autofix adaptation: Update system prompt instructions to rephrase how missing order numbers are requested, improving prompt adherence.
4. **`conflict_resolution_strategy`:**
   - Options: `WARN_AND_SURFACE` (default), `PREFER_CARRIER_TIMESTAMP`, or `FLAG_HUMAN_REVIEW`.
   - Autofix adaptation: Refine system guidance on reconciling conflicting timestamps between order and shipping services.
5. **`schema_fallback_rules`:**
   - Autofix adaptation: Toggle whether incomplete payloads trigger an immediate apology or a partial presentation with caveats.

---

## 7. Open Questions and Team Decisions

The team needs to align on and confirm the following design choices:

1. **Synthetic Data Storage Format:** Should our mock database be a static JSON file (`data/mock_orders.json`) or an in-memory SQLite / Python dictionary fixture?
2. **Evaluation Metric Criteria:** What constitutes a strict "PASS" vs "FAIL" for each of the six scenarios? (e.g., exact keyword matching vs LLM-as-a-judge evaluation).
3. **Observability Log Structure:** Should we emit standardized OpenTelemetry-style JSON logs (step, tool name, inputs, outputs, latency, tokens) to local disk for the self-repair platform to analyze?
4. **Autofix Copy & Human Approval Gate:** How should candidate configuration fixes be isolated (e.g., running tests in a branched config dict or temporary directory) and presented to the human reviewer for sign-off?

