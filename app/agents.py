"""
app/agents.py - LangGraph multi-agent pipeline for AIOps Incident Commander.

Pipeline: AlertPayload -> [Agent1: RCA] -> [Agent2: Remediation] -> [Agent3: Gatekeeper]

Each node is a pure function that receives the full IncidentState TypedDict
and returns a partial dict of updates to merge into the state.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import StateGraph, END

from app.models import (
    IncidentState,
    IncidentStatus,
    RCAAnalysis,
    RemediationPlan,
    RiskLevel,
)
from app.rag import query_runbooks
from config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM factory
# ---------------------------------------------------------------------------

def _get_llm(temperature: float = 0.1) -> ChatGroq:
    """Return a configured ChatGroq instance."""
    settings = get_settings()
    return ChatGroq(
        api_key=settings.groq_api_key,
        model=settings.groq_model,
        temperature=temperature,
        max_tokens=2048,
    )


# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict:
    """
    Extract and parse the first JSON object found in an LLM response.
    Handles markdown code fences and stray text before/after the JSON block.
    """
    # Try to extract from ```json ... ``` fences first
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        return json.loads(fence_match.group(1))

    # Fallback: find first { ... } block
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        return json.loads(brace_match.group(0))

    raise ValueError(f"No valid JSON object found in LLM response:\n{text[:500]}")


# ---------------------------------------------------------------------------
# Agent 1: Root Cause Analyzer
# ---------------------------------------------------------------------------

def agent_rca_analyzer(state: IncidentState) -> dict[str, Any]:
    """
    Perform root cause analysis using the alert payload and ChromaDB RAG context.

    Input state keys consumed: alert
    Output state keys produced: rca, status
    """
    alert = state["alert"]
    incident_id = state.get("incident_id", "unknown")
    logger.info("Incident %s | Agent 1 (RCA Analyzer) starting.", incident_id)

    # Retrieve relevant runbook context
    rag_query = f"{alert.metric} {alert.error_log} {alert.severity.value}"
    runbook_context = query_runbooks(rag_query)
    logger.debug("Incident %s | RAG context retrieved (%d chars).", incident_id, len(runbook_context))

    system_prompt = """You are a Senior Site Reliability Engineer specialising in root cause analysis.
Your task is to analyse an infrastructure alert and produce a structured root cause analysis.

IMPORTANT: You MUST respond with a valid JSON object only. No explanatory text outside the JSON.

Required JSON schema:
{
  "root_cause": "<detailed explanation of the probable root cause>",
  "confidence_score": <float between 0.0 and 1.0>,
  "affected_service": "<name of the primary process/service at fault>",
  "supporting_evidence": ["<evidence item 1>", "<evidence item 2>"],
  "runbook_context": "<brief summary of the most relevant runbook guidance>"
}"""

    user_message = f"""Analyse the following infrastructure alert:

SERVER: {alert.server_name}
METRIC: {alert.metric}
SEVERITY: {alert.severity.value}
TRIGGER VALUE: {alert.trigger_value or 'N/A'}
HOST GROUPS: {', '.join(alert.host_groups or ['N/A'])}
ERROR LOG:
{alert.error_log}

RELEVANT RUNBOOK KNOWLEDGE BASE CONTEXT:
{runbook_context}

Produce a comprehensive root cause analysis following the JSON schema in your instructions."""

    try:
        llm = _get_llm(temperature=0.05)
        response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])
        raw = response.content
        logger.debug("Incident %s | Agent 1 raw response:\n%s", incident_id, raw[:300])

        parsed = _extract_json(raw)
        rca = RCAAnalysis(
            root_cause=parsed.get("root_cause", "Unable to determine root cause."),
            confidence_score=float(parsed.get("confidence_score", 0.5)),
            affected_service=parsed.get("affected_service", "unknown"),
            supporting_evidence=parsed.get("supporting_evidence", []),
            runbook_context=parsed.get("runbook_context", runbook_context[:300]),
        )
        logger.info(
            "Incident %s | RCA complete. Cause: %s | Confidence: %.2f",
            incident_id,
            rca.root_cause[:80],
            rca.confidence_score,
        )
        return {"rca": rca, "status": IncidentStatus.ANALYSED}

    except Exception as exc:
        logger.error("Incident %s | Agent 1 failed: %s", incident_id, exc, exc_info=True)
        # Produce a degraded fallback RCA to allow the pipeline to continue
        rca = RCAAnalysis(
            root_cause=f"Automated analysis unavailable. Raw alert: {alert.error_log[:200]}",
            confidence_score=0.1,
            affected_service=alert.metric,
            supporting_evidence=[alert.error_log],
            runbook_context=runbook_context[:300],
        )
        return {"rca": rca, "status": IncidentStatus.ANALYSED, "error": str(exc)}


# ---------------------------------------------------------------------------
# Agent 2: Remediation Planner
# ---------------------------------------------------------------------------

def agent_remediation_planner(state: IncidentState) -> dict[str, Any]:
    """
    Formulate a concrete remediation plan based on the RCA output.

    Input state keys consumed: alert, rca
    Output state keys produced: remediation
    """
    alert = state["alert"]
    rca = state["rca"]
    incident_id = state.get("incident_id", "unknown")
    logger.info("Incident %s | Agent 2 (Remediation Planner) starting.", incident_id)

    system_prompt = """You are an expert DevOps/SRE engineer specialising in automated remediation.
Given a root cause analysis, you must produce a precise, executable remediation plan.

IMPORTANT: Respond with a valid JSON object ONLY. No text outside the JSON.

Required JSON schema:
{
  "action_title": "<short human-readable title for this action>",
  "command": "<exact Ansible playbook command or bash command to execute>",
  "risk_level": "<LOW | MEDIUM | HIGH>",
  "rollback_plan": "<step-by-step rollback instructions>",
  "estimated_duration_seconds": <integer>,
  "preconditions": ["<precondition 1>", "<precondition 2>"]
}

Risk Level Guidelines:
  LOW    - Service restart, cache clear, log cleanup. No data loss risk.
  MEDIUM - Configuration change, process kill, storage expansion.
  HIGH   - Host reboot, database migration, infrastructure reconfiguration."""

    user_message = f"""Based on the following Root Cause Analysis, formulate a remediation plan:

INCIDENT CONTEXT:
  Server: {alert.server_name}
  Metric: {alert.metric}
  Severity: {alert.severity.value}
  Error Log: {alert.error_log[:300]}

ROOT CAUSE ANALYSIS:
  Root Cause: {rca.root_cause}
  Confidence: {rca.confidence_score:.0%}
  Affected Service: {rca.affected_service}
  Supporting Evidence: {', '.join(rca.supporting_evidence[:3])}
  Runbook Guidance: {rca.runbook_context or 'N/A'}

Provide a safe, targeted remediation command. Prefer Ansible playbooks for production.
Use the server name '{alert.server_name}' in the command's target parameter."""

    try:
        llm = _get_llm(temperature=0.1)
        response = llm.invoke([
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_message),
        ])
        raw = response.content
        logger.debug("Incident %s | Agent 2 raw response:\n%s", incident_id, raw[:300])

        parsed = _extract_json(raw)

        # Normalise risk_level to our enum
        raw_risk = str(parsed.get("risk_level", "MEDIUM")).upper()
        try:
            risk = RiskLevel(raw_risk)
        except ValueError:
            risk = RiskLevel.MEDIUM

        remediation = RemediationPlan(
            action_title=parsed.get("action_title", "Execute Remediation"),
            command=parsed.get("command", f"ansible-playbook -i inventory/production site.yml -e 'target={alert.server_name}'"),
            risk_level=risk,
            rollback_plan=parsed.get("rollback_plan", "Restore from last known-good configuration backup."),
            estimated_duration_seconds=parsed.get("estimated_duration_seconds"),
            preconditions=parsed.get("preconditions", []),
        )
        logger.info(
            "Incident %s | Remediation plan formulated. Action: '%s' | Risk: %s",
            incident_id,
            remediation.action_title,
            remediation.risk_level.value,
        )
        return {"remediation": remediation}

    except Exception as exc:
        logger.error("Incident %s | Agent 2 failed: %s", incident_id, exc, exc_info=True)
        remediation = RemediationPlan(
            action_title="Manual Investigation Required",
            command=f"ssh ops@{alert.server_name} 'sudo systemctl status; free -h; df -h'",
            risk_level=RiskLevel.LOW,
            rollback_plan="No automated changes were applied.",
            estimated_duration_seconds=300,
            preconditions=["Verify SSH connectivity", "Check current service status"],
        )
        return {"remediation": remediation, "error": str(exc)}


# ---------------------------------------------------------------------------
# Agent 3: Gatekeeper & Validator
# ---------------------------------------------------------------------------

def agent_gatekeeper(state: IncidentState) -> dict[str, Any]:
    """
    Validate the full incident state and produce a formatted Telegram approval message.

    Input state keys consumed: alert, rca, remediation
    Output state keys produced: approval_message, status
    """
    incident_id = state.get("incident_id", "unknown")
    logger.info("Incident %s | Agent 3 (Gatekeeper) starting.", incident_id)

    alert = state.get("alert")
    rca = state.get("rca")
    remediation = state.get("remediation")

    # Validate all required state components are present
    if not all([alert, rca, remediation]):
        missing = [k for k, v in {"alert": alert, "rca": rca, "remediation": remediation}.items() if not v]
        error_msg = f"Gatekeeper validation failed. Missing state components: {missing}"
        logger.error("Incident %s | %s", incident_id, error_msg)
        return {
            "approval_message": f"*PIPELINE ERROR*\n{error_msg}",
            "status": IncidentStatus.FAILED,
            "error": error_msg,
        }

    # Risk emoji mapping for Telegram display
    risk_icons = {RiskLevel.LOW: "🟢", RiskLevel.MEDIUM: "🟡", RiskLevel.HIGH: "🔴"}
    severity_icons = {"DISASTER": "🚨", "HIGH": "🔥", "AVERAGE": "⚠️", "WARNING": "🔔", "INFORMATION": "ℹ️"}

    risk_icon = risk_icons.get(remediation.risk_level, "⚪")
    sev_icon = severity_icons.get(alert.severity.value, "⚠️")
    confidence_pct = f"{rca.confidence_score:.0%}"
    duration = f"{remediation.estimated_duration_seconds}s" if remediation.estimated_duration_seconds else "Unknown"

    preconditions_text = ""
    if remediation.preconditions:
        items = "\n".join(f"  • {p}" for p in remediation.preconditions[:4])
        preconditions_text = f"\n*Preconditions:*\n{items}"

    approval_message = (
        f"*AIOps Incident Commander — Approval Required*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{sev_icon} *Severity:* `{alert.severity.value}`\n"
        f"*Incident ID:* `{incident_id}`\n"
        f"*Host:* `{alert.server_name}`\n"
        f"*Metric:* `{alert.metric}`\n\n"
        f"*Root Cause Analysis*\n"
        f"┌─────────────────────────\n"
        f"│ *Cause:* {rca.root_cause[:200]}\n"
        f"│ *Service:* `{rca.affected_service}`\n"
        f"│ *Confidence:* `{confidence_pct}`\n"
        f"└─────────────────────────\n\n"
        f"*Proposed Remediation*\n"
        f"┌─────────────────────────\n"
        f"│ *Action:* {remediation.action_title}\n"
        f"│ *Risk:* {risk_icon} `{remediation.risk_level.value}`\n"
        f"│ *Est. Duration:* `{duration}`\n"
        f"│ *Command:*\n"
        f"│ `{remediation.command[:200]}`\n"
        f"└─────────────────────────"
        f"{preconditions_text}\n\n"
        f"*Rollback:* {remediation.rollback_plan[:150]}\n\n"
        f"_Do you authorise execution of this remediation action?_"
    )

    logger.info(
        "Incident %s | Gatekeeper approved pipeline. Approval message formatted (%d chars).",
        incident_id,
        len(approval_message),
    )
    return {
        "approval_message": approval_message,
        "status": IncidentStatus.PENDING_APPROVAL,
    }


# ---------------------------------------------------------------------------
# LangGraph Pipeline Builder
# ---------------------------------------------------------------------------

def build_pipeline() -> Any:
    """
    Construct and compile the LangGraph StateGraph pipeline.

    Returns:
        A compiled LangGraph runnable ready to be invoked with an IncidentState.
    """
    workflow = StateGraph(IncidentState)

    # Register nodes
    workflow.add_node("rca_analyzer", agent_rca_analyzer)
    workflow.add_node("remediation_planner", agent_remediation_planner)
    workflow.add_node("gatekeeper", agent_gatekeeper)

    # Define execution edges (linear pipeline)
    workflow.set_entry_point("rca_analyzer")
    workflow.add_edge("rca_analyzer", "remediation_planner")
    workflow.add_edge("remediation_planner", "gatekeeper")
    workflow.add_edge("gatekeeper", END)

    compiled = workflow.compile()
    logger.info("LangGraph pipeline compiled successfully (3 nodes).")
    return compiled


# Module-level singleton pipeline (compiled once at import time)
_pipeline = None


def get_pipeline() -> Any:
    """Return the module-level compiled pipeline, building it on first call."""
    global _pipeline
    if _pipeline is None:
        _pipeline = build_pipeline()
    return _pipeline


async def run_pipeline(state: IncidentState) -> IncidentState:
    """
    Execute the full LangGraph pipeline asynchronously.

    Args:
        state: Initial IncidentState with 'alert' and 'incident_id' populated.

    Returns:
        The final IncidentState after all agents have run.
    """
    pipeline = get_pipeline()
    logger.info("Incident %s | Pipeline invocation started.", state.get("incident_id"))
    # LangGraph's ainvoke supports async execution
    final_state: IncidentState = await pipeline.ainvoke(state)
    logger.info(
        "Incident %s | Pipeline completed. Final status: %s",
        final_state.get("incident_id"),
        final_state.get("status"),
    )
    return final_state
