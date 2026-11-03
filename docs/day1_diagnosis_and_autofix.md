# Day 1 Design: Diagnosis and Safe Autofix

**Project:** AI Agent Evaluation, Observability, and Safe Self-Repair Platform

**Scope:** Diagnose failed evaluation runs and evaluate narrowly constrained settings repairs
**Status:** Day 1 proposal; design only, no implementation

## 1. Goal and safety boundary

The diagnosis agent explains a failed run using stored evidence and may propose one allowlisted settings change. The platform tests that change on an isolated copy. A person must approve the candidate before it can be kept.

The agent is advisory: it cannot change a live agent or its settings. It cannot edit arbitrary source code, perform real transactions, or send external messages. A candidate is never applied merely because its tests pass.

## 2. Diagnosis inputs

For one evaluation run, provide only the data needed to understand and reproduce the failure:

- Run ID, case ID, agent and tool configuration versions, and run timestamps.
- The stored trace: ordered event IDs, step and tool names, sanitized inputs and outputs, errors, and durations.
- The case expectation: required behavior, assertions, and relevant safety constraints.
- The failure summary: failed assertions, expected versus actual results, and evaluation status.
- The applicable settings snapshot and schema/version, so the agent can identify which allowlisted setting could be relevant.

Exclude secrets and unnecessary personal data. Redact credentials and sensitive values in trace inputs and outputs before they are exposed to the diagnosis agent. If required evidence is absent, redacted in a way that prevents diagnosis, or inconsistent, the agent must say so and abstain rather than fill gaps by guessing.

## 3. Read-only inspection tools

Expose only these scoped, read-only operations to the diagnosis agent:

| Tool | Purpose | Input | Output |
|---|---|---|---|
| `inspect_run(run_id)` | Read the stored run and ordered trace events | One run ID | Run metadata and sanitized events, each with a stable event ID |
| `get_case_expectation(case_id)` | Read the evaluation contract for the case | One case ID | Expected behavior, assertions, and safety requirements |
| `get_failure_summary(run_id)` | Read the evaluator's result | One run ID | Failed assertions and expected/actual outcome summary |

These tools must not write, retry, or mutate the run, case, agent, or settings. They must return only the requested run or case. The agent has no other tools during diagnosis.

## 4. Evidence-based diagnosis

Every diagnosis must separate:

- **Observed facts:** Directly present in the stored trace, expectation, or failure summary. Cite each fact with its source and stable event ID, for example: “Observed: `lookup_order_details` returned `TIMEOUT_ERROR` (trace event `evt-104`).”
- **Hypotheses:** Possible explanations inferred from those facts. Label them as hypotheses, cite the supporting event IDs, and state what evidence is missing. Do not describe a hypothesis as confirmed root cause.

Use the smallest relevant set of event IDs, and do not invent, alter, or cite IDs that are not in the supplied run. If evidence does not support a repair, return “no supported repair” with the reason.

## 5. Allowlisted repair types

The first version may propose at most one setting change per candidate, from this allowlist:

1. **Bounded retry adjustment:** Change the configured retry count for an idempotent, read-only mock tool from `0` to `1` or from `1` to `2`, only when trace evidence supports a transient timeout or failure. Never retry a non-idempotent action.
2. **Clearer tool description:** Replace the description of an existing mock tool with a concise clarification of its existing purpose, required identifier, or input format. Do not add capabilities, change its schema, or grant access.
3. **Ask for a missing ID:** Adjust the existing agent instruction to ask the user for a missing order ID before calling lookup tools. Do not add guesses, identity inference, or extra collection of personal data.

The candidate must identify the exact allowlisted setting, current value, proposed value, rationale, and supporting event IDs. Retry caps and valid setting schemas must be enforced by the platform, not trusted to the diagnosis agent. Any repair outside this allowlist is unsupported.

## 6. Candidate and evaluation lifecycle

1. **Diagnose:** Read the run, expectation, and failure summary; report facts, hypotheses, and either one supported candidate or no repair.
2. **Create candidate on a copy:** Validate the proposed change against the allowlist and settings schema, then apply it to a disposable copy of the exact agent/settings version used for the run. Preserve the original and record the candidate diff.
3. **Rerun the failed case:** Evaluate the same case with the candidate copy and unchanged inputs and evaluator.
4. **Rerun the regression suite:** Evaluate the full agreed suite against the candidate copy.
5. **Compare:** Compare the baseline and candidate case results, suite results, safety assertions, and relevant run metrics. Report the exact setting diff and evidence; do not hide failures behind an aggregate score.
6. **Human decision:** Present the diagnosis, citations, candidate diff, and comparison to a reviewer. Keep the candidate isolated while awaiting approval. Approval may promote only that reviewed settings change; rejection discards the copy. A new or modified candidate requires a new evaluation and approval.

Testing is not approval. There is no automatic promotion, even when the candidate improves the score.

## 7. Mandatory rejection conditions

Reject the candidate and do not offer it for approval if any of the following applies:

- It enables or risks a safety violation, real transaction, external message, live-agent change, or unapproved data access.
- The failed case remains failed, a required safety assertion fails, or any regression case becomes worse or newly fails.
- Results are missing, inconclusive, or cannot be compared against the baseline under the same evaluator and inputs.
- The setting is invalid under the schema, outside its allowed range, targets the wrong agent/tool/version, or changes more than the proposed single setting.
- The repair type or setting is not on the allowlist, or the diagnosis lacks trace evidence supporting it.
- The candidate attempts arbitrary source-code edits, changes tool capabilities or schemas, or exceeds any step, call, or time limit.
- The candidate copy cannot be isolated from the original or its changes cannot be reliably discarded.

Rejection must include the reason and relevant evidence. A rejected candidate must never be silently retried with a broader change.

## 8. Proposed execution limits

These are initial safeguards for team review; the existing Day 1 agent proposal also suggests four reasoning steps, three tool calls, two-second tool timeouts, and a ten-second run timeout.

| Phase | Proposed hard limit |
|---|---|
| Diagnosis | At most 4 reasoning steps, 3 read-only tool calls total (at most one call to each inspection tool), 2 seconds per tool call, and 10 seconds wall-clock total |
| Candidate creation | At most 1 candidate and 1 allowlisted setting change; no additional agent tools; 10 seconds wall-clock |
| Repair evaluation | Exactly one run of the failed case and one run of the full regression suite; no test reruns; 10 seconds maximum per test case and 120 seconds wall-clock total for both runs |

If a limit is reached, stop and mark the diagnosis or evaluation incomplete. An incomplete evaluation is not eligible for approval.

## 9. Team decisions to confirm

1. Which trace fields must be retained, how long they are retained, and what redaction rules apply to tool inputs and outputs?
2. What is the canonical event-ID format, and which run, expectation, and failure-summary fields must the inspection tools return?
3. Which tool operations are guaranteed idempotent and therefore eligible for a retry adjustment?
4. Are the three repair types and their proposed value bounds acceptable? Who owns the settings schema and allowlist?
5. Which cases constitute the full regression suite, what comparison metrics are required, and what does “worse” mean for each metric?
6. Are the proposed diagnosis and evaluation time/call budgets appropriate for the actual test suite?
7. Who may approve or reject a candidate, how is that decision recorded, and what exact mechanism promotes an approved settings-only change?
8. What should happen when evidence is incomplete, evaluation is nondeterministic, or a run exceeds a limit? The proposal is to abstain or reject rather than infer success.
