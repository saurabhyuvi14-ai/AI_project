"""
AgentOps: Minimal Streamlit evaluation summary and comparison screen.
Compatible with local offline mock mode and persistent SQLite trace store.

Run with:
    streamlit run app.py
Or test directly in terminal:
    python3 app.py
"""

import os
import sys
from typing import Any, Dict, List, Optional, Tuple

from src.models import (
    get_default_config,
    get_baseline_fast_fail_config,
)
from src.agent import SupportAgent
from src.evaluation import (
    Case,
    EvaluationResult,
    SuiteReport,
    ComparisonReport,
    build_canonical_case_list,
    run_suite_evaluation,
    compare_agents,
    run_baseline_comparison,
)
from src.tracer import (
    init_db,
    list_runs,
    get_run_trace,
    run_trace_to_dict,
)

# Default local database path
DEFAULT_DB_PATH = "data/agentops.db"


def run_evaluation_data(db_path: Optional[str] = None) -> Tuple[SuiteReport, SuiteReport, ComparisonReport]:
    """Execute the baseline and candidate suites and compute the comparison report."""
    if db_path:
        init_db(db_path)
    return run_baseline_comparison(db_path=db_path)


def format_rate(rate_dict: Dict[str, Any], key: str, count_key: str, total_key: str) -> str:
    """Format a metric dictionary into a readable string showing raw count and denominator."""
    val = rate_dict.get(key)
    count = rate_dict.get(count_key, 0)
    total = rate_dict.get(total_key, 0)
    if val is None:
        return f"N/A ({count}/{total})"
    return f"{count}/{total} ({val * 100:.1f}%)"


def render_cli_summary(baseline_report: SuiteReport, fastfail_report: SuiteReport, comparison: ComparisonReport) -> None:
    """Renders a clean terminal summary for non-Streamlit environments or test runs."""
    print("=" * 80)
    print("AgentOps — Offline Evaluation & Comparison Summary")
    print("=" * 80)
    print(f"Baseline Agent Version : {baseline_report.agent_version}")
    print(f"Candidate Agent Version: {fastfail_report.agent_version}")
    print("-" * 80)

    # Metrics Summary
    b_ts = baseline_report.metrics["task_success"]
    b_tf = baseline_report.metrics["tool_failures"]
    b_rec = baseline_report.metrics["recovery"]
    b_sv = baseline_report.metrics["safety_violations"]
    b_fla = baseline_report.metrics["failure_localization"]
    rfr = comparison.regression_rate
    rsr = comparison.repair_success

    print("BASELINE METRICS (Raw Counts & Denominators):")
    print(f"  • Task Success Rate       : {format_rate(b_ts, 'task_success_rate', 'passed_count', 'total_cases')}")
    print(f"  • Tool Failure Rate        : {format_rate(b_tf, 'tool_failure_rate', 'failed_tool_calls', 'total_tool_calls')}")
    print(f"  • Recovery Rate            : {format_rate(b_rec, 'recovery_rate', 'recovered_count', 'error_cases_total')}")
    print(f"  • Safety Violations        : {format_rate(b_sv, 'safety_violation_rate', 'violation_count', 'total_cases')}")
    print(f"  • Localization Accuracy    : {format_rate(b_fla, 'localization_accuracy', 'matched_count', 'failed_cases_total')}")
    print("-" * 80)

    # Case Outcomes
    print("CASE OUTCOMES:")
    print(f"{'Case ID':<26} | {'Baseline':<8} | {'Candidate':<10} | {'Delta Status':<16}")
    print("-" * 80)
    for row in comparison.case_comparisons:
        print(f"{row['case_id']:<26} | {row['baseline_verdict']:<8} | {row['candidate_verdict']:<10} | {row['delta_status']:<16}")
    print("-" * 80)

    # Comparison Gating
    print("CANDIDATE COMPARISON & GATING (Counts & Denominators):")
    print(f"  • Comparison Verdict     : {comparison.verdict}")
    print(f"  • Gating Reason          : {comparison.reason}")
    print(f"  • Safety Gate            : {comparison.safety_status}")
    print(f"  • Regression Rate (RFR)  : {format_rate(rfr, 'regression_rate', 'regressed_count', 'baseline_passed_count')}")
    print(f"  • Repair Success (RSR)   : {format_rate(rsr, 'repair_success_rate', 'repaired_count', 'targeted_failing_count')}")
    print("=" * 80)


def main():
    """Main entrypoint: renders Streamlit UI if running under streamlit, else CLI."""
    try:
        import streamlit as st
        has_streamlit = True
    except ImportError:
        has_streamlit = False

    if not has_streamlit or (not os.environ.get("STREAMLIT_SERVER_PORT") and "streamlit" not in sys.modules.get("__main__", "").__file__):
        # Run in offline CLI mode
        b_rep, c_rep, comp = run_evaluation_data(DEFAULT_DB_PATH)
        render_cli_summary(b_rep, c_rep, comp)
        print("\n[Notice] To launch interactive UI, install and run with: streamlit run app.py")
        return

    import streamlit as st

    st.set_page_config(
        page_title="AgentOps Evaluation & Observability",
        page_icon="🛡️",
        layout="wide",
    )

    st.title("🛡️ AgentOps — AI Agent Evaluation, Observability & Comparison")
    st.caption("Local Prototype: Deterministic benchmark suite, repeatable scoring, trace inspection, and repair candidate gating.")

    # Sidebar controls
    st.sidebar.header("Execution Controls")
    db_path = st.sidebar.text_input("Trace Store SQLite Path", value=DEFAULT_DB_PATH)

    view_tab = st.sidebar.radio(
        "Navigation",
        options=[
            "Test Batch & Metrics",
            "Agent Comparison View",
            "Trace Inspector",
            "Stored Runs Catalog",
        ],
    )

    # Initialize session state for batch caching
    if "cached_eval" not in st.session_state:
        st.session_state["cached_eval"] = None

    if st.sidebar.button("▶️ Start New Test Batch"):
        st.session_state["cached_eval"] = None

    # Error handling for database access
    try:
        if db_path and not os.path.exists(os.path.dirname(os.path.abspath(db_path))):
            os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    except Exception as exc:
        st.sidebar.error(f"Invalid database directory path: {exc}")

    # =========================================================================
    # TAB 1: Test Batch & Evaluation Metrics
    # =========================================================================
    if view_tab == "Test Batch & Metrics":
        st.subheader("📊 Test Batch Execution & Evaluation Metrics")

        # Loading state when starting/refreshing a test batch
        if st.session_state["cached_eval"] is None:
            with st.spinner("Running deterministic 6-case evaluation batch in offline mock mode..."):
                try:
                    b_rep, c_rep, comp = run_evaluation_data(db_path=db_path)
                    st.session_state["cached_eval"] = (b_rep, c_rep, comp)
                    st.success("Test batch executed and persisted to trace store successfully!")
                except Exception as exc:
                    st.error(f"Error executing test batch: {exc}")
                    return

        baseline_report, _, _ = st.session_state["cached_eval"]

        # Top metric cards with raw counts & denominators
        st.markdown("#### Evaluation Measures")
        col1, col2, col3, col4 = st.columns(4)
        ts = baseline_report.metrics["task_success"]
        tf = baseline_report.metrics["tool_failures"]
        rec = baseline_report.metrics["recovery"]
        sv = baseline_report.metrics["safety_violations"]

        col1.metric("Task Success Rate", format_rate(ts, "task_success_rate", "passed_count", "total_cases"))
        col2.metric("Tool Failure Rate", format_rate(tf, "tool_failure_rate", "failed_tool_calls", "total_tool_calls"))
        col3.metric("Recovery Rate", format_rate(rec, "recovery_rate", "recovered_count", "error_cases_total"))
        col4.metric("Safety Violations", format_rate(sv, "safety_violation_rate", "violation_count", "total_cases"))

        st.markdown(f"**Agent Version Under Test:** `{baseline_report.agent_version}` | **Role:** `{baseline_report.config_role}`")
        st.markdown("---")

        # Case outcomes table
        st.markdown("#### Case Outcomes")
        table_rows = []
        for e in baseline_report.evaluations:
            table_rows.append({
                "Case ID": e.case_id,
                "Verdict": "✅ PASS" if e.passed else "❌ FAIL",
                "Score": f"{e.score:.1f}",
                "Tool Calls": e.details.get("observed_tool_calls", 0),
                "Failure Category": e.failure_category.value if e.failure_category else "None",
                "Evidence IDs Count": len(e.evidence_event_ids),
                "Run ID": e.run_id,
            })
        st.dataframe(table_rows, use_container_width=True)

        # Case drill-down
        st.markdown("#### Inspect Individual Case Decision")
        case_options = [e.case_id for e in baseline_report.evaluations]
        selected_case = st.selectbox("Select Case", case_options)
        selected_eval = next(e for e in baseline_report.evaluations if e.case_id == selected_case)

        with st.expander(f"Decision Details for {selected_case}", expanded=True):
            st.write("**Observed Status:**", selected_eval.details.get("observed_status"))
            st.write("**Checks Breakdown:**", selected_eval.checks)
            st.write("**Details:**", selected_eval.details)
            st.write("**Evidence Event IDs:**", selected_eval.evidence_event_ids)

    # =========================================================================
    # TAB 2: Agent Comparison View
    # =========================================================================
    elif view_tab == "Agent Comparison View":
        st.subheader("⚖️ Side-by-Side Agent Comparison & Safe Repair Gating")
        st.caption("Compares the standard baseline agent (`v1.0.0`) against the controlled second agent (`v0.9.0-fastfail`) across the identical 6 cases.")

        if st.session_state["cached_eval"] is None:
            with st.spinner("Executing comparison benchmark across both agent configurations..."):
                try:
                    b_rep, c_rep, comp = run_evaluation_data(db_path=db_path)
                    st.session_state["cached_eval"] = (b_rep, c_rep, comp)
                except Exception as exc:
                    st.error(f"Error running comparison: {exc}")
                    return

        baseline_report, candidate_report, comparison = st.session_state["cached_eval"]

        # Gate status callout
        if comparison.verdict == "ACCEPTED":
            st.success(f"**Gate Decision: ACCEPTED** — {comparison.reason}")
        elif comparison.verdict == "REJECTED":
            st.warning(f"**Gate Decision: REJECTED** — {comparison.reason}")
        else:
            st.info(f"**Gate Decision: NEUTRAL** — {comparison.reason}")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Safety Gate", comparison.safety_status)
        rfr = comparison.regression_rate
        c2.metric("Regressions (RFR)", format_rate(rfr, "regression_rate", "regressed_count", "baseline_passed_count"))
        rsr = comparison.repair_success
        c3.metric("Repair Success (RSR)", format_rate(rsr, "repair_success_rate", "repaired_count", "targeted_failing_count"))
        b_tsr = comparison.metrics_comparison["task_success_rate"]["baseline"]
        c_tsr = comparison.metrics_comparison["task_success_rate"]["candidate"]
        c4.metric(f"TSR: {baseline_report.agent_version} vs {candidate_report.agent_version}",
                  f"{c_tsr * 100:.1f}%", delta=f"{(c_tsr - b_tsr)*100:.1f}%")

        st.markdown("#### Side-by-Side Case Matrix")
        st.dataframe(comparison.case_comparisons, use_container_width=True)

        st.caption("Note: Results reflect actual test runs; no improvements are claimed without verifiable execution traces.")

    # =========================================================================
    # TAB 3: Trace Inspector
    # =========================================================================
    elif view_tab == "Trace Inspector":
        st.subheader("🔍 Run Trace Inspector")
        st.caption("Inspects chronological execution events and tool payloads using the tracing interface.")

        # Read runs using the tracing interface list_runs()
        try:
            stored_runs = list_runs(db_path)
        except Exception as exc:
            st.error(f"Error reading from trace store: {exc}")
            stored_runs = []

        if not stored_runs:
            st.info("No stored runs found in the trace store. Click 'Start New Test Batch' in the sidebar to generate traces.")
            return

        run_id_map = {f"{r.run_id} | {r.case_id} ({r.agent_version}) - {r.status}": r.run_id for r in stored_runs}
        selected_label = st.selectbox("Select Run to Inspect", list(run_id_map.keys()))
        selected_run_id = run_id_map[selected_label]

        with st.spinner(f"Loading trace for run {selected_run_id}..."):
            trace = get_run_trace(db_path, selected_run_id)

        if trace is None:
            st.error(f"Trace for run ID '{selected_run_id}' could not be loaded from trace store.")
            return

        trace_dict = run_trace_to_dict(trace)
        run_meta = trace_dict["run"]
        events = trace_dict["events"]

        # Run overview
        col1, col2, col3, col4 = st.columns(4)
        col1.write(f"**Case ID:** `{run_meta.get('case_id')}`")
        col2.write(f"**Status:** `{run_meta.get('status')}`")
        col3.write(f"**Stop Reason:** `{run_meta.get('stop_reason')}`")
        metrics_meta = run_meta.get("metrics") or {}
        col4.write(f"**Latency:** `{metrics_meta.get('latency_ms', 0)} ms`")

        st.write(f"**Final Answer:** {run_meta.get('final_output')}")
        if run_meta.get("error_code"):
            st.warning(f"Error Code: {run_meta.get('error_code')}")

        st.markdown("#### Chronological Events Timeline")
        if not events:
            st.write("No events recorded for this run.")
        else:
            event_summary_table = [
                {
                    "Seq": ev.get("sequence_no"),
                    "Timestamp": ev.get("timestamp"),
                    "Event Type": ev.get("event_type"),
                    "Stage": ev.get("stage"),
                    "Tool": ev.get("tool_name") or "-",
                    "Status": ev.get("status") or "-",
                    "Duration (ms)": ev.get("duration_ms") or 0.0,
                    "Error": ev.get("error_code") or "-",
                }
                for ev in events
            ]
            st.dataframe(event_summary_table, use_container_width=True)

            # Drill down into specific event payloads
            st.markdown("#### Event Payload Inspector")
            event_indices = [f"Event #{ev.get('sequence_no')} ({ev.get('event_type')})" for ev in events]
            selected_ev_label = st.selectbox("Select Event", event_indices)
            selected_idx = event_indices.index(selected_ev_label)
            selected_event = events[selected_idx]

            col_in, col_out = st.columns(2)
            with col_in:
                st.write("**Input Payload:**")
                st.json(selected_event.get("input"))
            with col_out:
                st.write("**Output Payload:**")
                st.json(selected_event.get("output"))

    # =========================================================================
    # TAB 4: Stored Runs Catalog
    # =========================================================================
    else:
        st.subheader("🗄️ Stored Runs Catalog")
        try:
            stored_runs = list_runs(db_path)
        except Exception as exc:
            st.error(f"Error reading from trace store: {exc}")
            stored_runs = []

        if not stored_runs:
            st.info("Empty State: No stored runs found in the trace store. Run a test batch to persist runs.")
        else:
            run_data = [
                {
                    "Run ID": r.run_id,
                    "Case ID": r.case_id,
                    "Agent Version": r.agent_version,
                    "Config Role": r.config_role or "original",
                    "Status": r.status,
                    "Stop Reason": r.stop_reason,
                    "Start Time": r.start_time,
                }
                for r in stored_runs
            ]
            st.dataframe(run_data, use_container_width=True)


if __name__ == "__main__":
    main()
