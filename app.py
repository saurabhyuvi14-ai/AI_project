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
from src.tracer import init_db, list_runs

# Default local database path
DEFAULT_DB_PATH = "data/agentops.db"


def run_evaluation_data(db_path: Optional[str] = None) -> Tuple[SuiteReport, SuiteReport, ComparisonReport]:
    """Execute the baseline and candidate suites and compute the comparison report."""
    if db_path:
        init_db(db_path)
    return run_baseline_comparison(db_path=db_path)


def format_rate(rate_dict: Dict[str, Any], key: str, count_key: str, total_key: str) -> str:
    """Format a metric dictionary into a readable string showing count and denominator."""
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

    print("BASELINE METRICS (Counts & Denominators):")
    print(f"  • Task Success Rate   : {format_rate(b_ts, 'task_success_rate', 'passed_count', 'total_cases')}")
    print(f"  • Tool Failure Rate    : {format_rate(b_tf, 'tool_failure_rate', 'failed_tool_calls', 'total_tool_calls')}")
    print(f"  • Recovery Rate        : {format_rate(b_rec, 'recovery_rate', 'recovered_count', 'error_cases_total')}")
    print(f"  • Safety Violations    : {format_rate(b_sv, 'safety_violation_rate', 'violation_count', 'total_cases')}")
    print("-" * 80)

    # Case Outcomes
    print("CASE OUTCOMES:")
    print(f"{'Case ID':<26} | {'Baseline':<8} | {'Candidate':<10} | {'Delta Status':<16}")
    print("-" * 80)
    for row in comparison.case_comparisons:
        print(f"{row['case_id']:<26} | {row['baseline_verdict']:<8} | {row['candidate_verdict']:<10} | {row['delta_status']:<16}")
    print("-" * 80)

    # Comparison Gating
    print(f"Comparison Verdict : {comparison.verdict}")
    print(f"Reason             : {comparison.reason}")
    print(f"Safety Gate        : {comparison.safety_status}")
    print(f"Regression Rate    : {comparison.regression_rate.get('regressed_count', 0)}/{comparison.regression_rate.get('baseline_passed_count', 0)}")
    print("=" * 80)


def main():
    """Main entrypoint: renders Streamlit UI if running under streamlit, else CLI."""
    try:
        import streamlit as st
        has_streamlit = True
    except ImportError:
        has_streamlit = False

    if not has_streamlit or not os.environ.get("STREAMLIT_SERVER_PORT") and "streamlit" not in sys.modules.get("__main__", "").__file__:
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
    st.caption("Local Prototype: Deterministic benchmark suite, repeatable scoring, and repair candidate gating.")

    # Sidebar controls
    st.sidebar.header("Execution Controls")
    db_path = st.sidebar.text_input("Trace Store SQLite Path", value=DEFAULT_DB_PATH)
    mode = st.sidebar.radio(
        "Evaluation View",
        options=["Run Standard Baseline (v1.0.0)", "Run Controlled Baseline Comparison", "Inspect Stored Runs"],
    )

    if st.sidebar.button("Execute Evaluation / Refresh"):
        st.session_state["cached_eval"] = None

    # Load or compute data
    if "cached_eval" not in st.session_state or st.session_state["cached_eval"] is None:
        with st.spinner("Running deterministic test suite through tracing interface..."):
            b_rep, c_rep, comp = run_evaluation_data(db_path=db_path)
            st.session_state["cached_eval"] = (b_rep, c_rep, comp)

    baseline_report, candidate_report, comparison = st.session_state["cached_eval"]

    if mode == "Run Standard Baseline (v1.0.0)":
        st.subheader(f"📊 Baseline Agent Evaluation ({baseline_report.agent_version})")

        # Top metric cards with counts & denominators
        col1, col2, col3, col4 = st.columns(4)
        ts = baseline_report.metrics["task_success"]
        tf = baseline_report.metrics["tool_failures"]
        rec = baseline_report.metrics["recovery"]
        sv = baseline_report.metrics["safety_violations"]

        col1.metric("Task Success Rate", format_rate(ts, "task_success_rate", "passed_count", "total_cases"))
        col2.metric("Tool Failure Rate", format_rate(tf, "tool_failure_rate", "failed_tool_calls", "total_tool_calls"))
        col3.metric("Recovery Rate", format_rate(rec, "recovery_rate", "recovered_count", "error_cases_total"))
        col4.metric("Safety Violations", format_rate(sv, "safety_violation_rate", "violation_count", "total_cases"))

        st.markdown("### Test Case Outcomes")
        table_rows = []
        for e in baseline_report.evaluations:
            table_rows.append({
                "Case ID": e.case_id,
                "Verdict": "✅ PASS" if e.passed else "❌ FAIL",
                "Score": f"{e.score:.1f}",
                "Tool Calls": e.details.get("observed_tool_calls", 0),
                "Failure Category": e.failure_category.value if e.failure_category else "None",
                "Evidence IDs Count": len(e.evidence_event_ids),
            })
        st.dataframe(table_rows, use_container_width=True)

        st.markdown("### Case Detail Inspector")
        case_options = [e.case_id for e in baseline_report.evaluations]
        selected_case = st.selectbox("Select Case to Inspect", case_options)
        selected_eval = next(e for e in baseline_report.evaluations if e.case_id == selected_case)

        with st.expander("Inspection Details", expanded=True):
            st.write("**Observed Status:**", selected_eval.details.get("observed_status"))
            st.write("**Checks Breakdown:**", selected_eval.checks)
            st.write("**Evidence Event IDs:**", selected_eval.evidence_event_ids)

    elif mode == "Run Controlled Baseline Comparison":
        st.subheader("⚖️ Side-by-Side Agent Comparison & Gating")
        st.info("Compares the standard agent (`v1.0.0`) against the controlled second agent (`v0.9.0-fastfail`).")

        # Gate status callout
        if comparison.verdict == "ACCEPTED":
            st.success(f"**Gate Status: ACCEPTED** — {comparison.reason}")
        elif comparison.verdict == "REJECTED":
            st.warning(f"**Gate Status: REJECTED** — {comparison.reason}")
        else:
            st.info(f"**Gate Status: NEUTRAL** — {comparison.reason}")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Safety Gate", comparison.safety_status)
        rfr = comparison.regression_rate
        c2.metric("Regressions", f"{rfr.get('regressed_count', 0)}/{rfr.get('baseline_passed_count', 0)}")
        b_tsr = comparison.metrics_comparison["task_success_rate"]["baseline"]
        c_tsr = comparison.metrics_comparison["task_success_rate"]["candidate"]
        c3.metric("Baseline TSR", f"{b_tsr * 100:.1f}%")
        c4.metric("Candidate TSR", f"{c_tsr * 100:.1f}%", delta=f"{(c_tsr - b_tsr)*100:.1f}%")

        st.markdown("### Side-by-Side Case Matrix")
        st.dataframe(comparison.case_comparisons, use_container_width=True)

        st.caption("Note: Metrics and comparisons reflect actual test runs; no improvements are claimed without verifiable execution traces.")

    else:
        st.subheader("🗄️ Stored Runs in Trace Database")
        runs = list_runs(db_path)
        if not runs:
            st.write("No stored runs found in trace database.")
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
                for r in runs
            ]
            st.dataframe(run_data, use_container_width=True)


if __name__ == "__main__":
    main()
