"""
app/models.py - Pydantic v2 schema definitions for the AIOps Incident Commander.

All models use strict field typing and validation via Pydantic v2.
The IncidentState TypedDict carries the full pipeline context through LangGraph.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, TypedDict

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    """Alert severity levels mapped from Zabbix / Prometheus conventions."""
    INFORMATION = "INFORMATION"
    WARNING = "WARNING"
    AVERAGE = "AVERAGE"
    HIGH = "HIGH"
    DISASTER = "DISASTER"


class RiskLevel(str, Enum):
    """Risk classification for proposed remediation actions."""
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class IncidentStatus(str, Enum):
    """Lifecycle status of an incident through the HITL pipeline."""
    PENDING_ANALYSIS = "PENDING_ANALYSIS"
    ANALYSED = "ANALYSED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    RESOLVED = "RESOLVED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------------
# Input Schemas (Webhook Ingestion)
# ---------------------------------------------------------------------------

class AlertPayload(BaseModel):
    """
    Webhook payload received from an external monitoring system
    (Zabbix, Prometheus AlertManager, Grafana, etc.).
    """
    model_config = {"str_strip_whitespace": True}

    server_name: str = Field(
        ...,
        min_length=1,
        max_length=253,
        description="Fully-qualified domain name or IP of the affected host.",
        examples=["prod-db-01.internal"],
    )
    metric: str = Field(
        ...,
        min_length=1,
        max_length=512,
        description="Name of the metric that breached its threshold.",
        examples=["vm.memory.utilization"],
    )
    error_log: str = Field(
        ...,
        min_length=1,
        description="Raw log excerpt or description of the anomaly.",
        examples=["OOMKiller invoked: process 'java' killed, rss=16384 MB"],
    )
    severity: Severity = Field(
        ...,
        description="Alert severity classification.",
        examples=[Severity.HIGH],
    )
    trigger_value: Optional[str] = Field(
        None,
        description="Current metric value that triggered the alert.",
        examples=["95.4%"],
    )
    host_groups: Optional[list[str]] = Field(
        default_factory=list,
        description="Zabbix host groups associated with the host.",
        examples=[["Linux servers", "Production"]],
    )

    @field_validator("server_name")
    @classmethod
    def validate_server_name(cls, v: str) -> str:
        if not v.replace("-", "").replace(".", "").replace("_", "").isalnum():
            raise ValueError(
                "server_name must contain only alphanumeric characters, hyphens, dots, or underscores."
            )
        return v.lower()


# ---------------------------------------------------------------------------
# Agent 1 Output Schema
# ---------------------------------------------------------------------------

class RCAAnalysis(BaseModel):
    """
    Root Cause Analysis result produced by Agent 1.
    """
    root_cause: str = Field(
        ...,
        description="Natural-language explanation of the probable root cause.",
    )
    confidence_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Model confidence in the root cause, from 0.0 (none) to 1.0 (certain).",
    )
    affected_service: str = Field(
        ...,
        description="Name of the primary service or process deemed responsible.",
    )
    supporting_evidence: list[str] = Field(
        default_factory=list,
        description="Log snippets or metric patterns that corroborate the root cause.",
    )
    runbook_context: Optional[str] = Field(
        None,
        description="Relevant excerpt retrieved from the ChromaDB runbook knowledge base.",
    )


# ---------------------------------------------------------------------------
# Agent 2 Output Schema
# ---------------------------------------------------------------------------

class RemediationPlan(BaseModel):
    """
    Remediation plan formulated by Agent 2.
    """
    action_title: str = Field(
        ...,
        description="Short, human-readable title for this remediation action.",
        examples=["Restart Memory-Leaking Java Service"],
    )
    command: str = Field(
        ...,
        description="Exact shell command, Ansible ad-hoc, or playbook invocation to execute.",
        examples=["ansible-playbook -i inventory/production memory_leak.yml -e 'target=prod-db-01'"],
    )
    risk_level: RiskLevel = Field(
        ...,
        description="Assessed risk of executing this command in production.",
    )
    rollback_plan: str = Field(
        ...,
        description="Step-by-step instructions to revert the action if it fails.",
    )
    estimated_duration_seconds: Optional[int] = Field(
        None,
        ge=0,
        description="Approximate wall-clock time the remediation is expected to take.",
    )
    preconditions: list[str] = Field(
        default_factory=list,
        description="Requirements that must be verified before executing the command.",
    )


# ---------------------------------------------------------------------------
# LangGraph State (TypedDict)
# ---------------------------------------------------------------------------

class IncidentState(TypedDict, total=False):
    """
    Shared mutable state object passed between LangGraph nodes.

    Using TypedDict (not BaseModel) as required by LangGraph's state management.
    Fields are intentionally Optional-by-absence so each node can populate
    its own portion of the state without knowing the full lifecycle.
    """
    # --- Input ---
    alert: AlertPayload

    # --- Agent 1 output ---
    rca: RCAAnalysis

    # --- Agent 2 output ---
    remediation: RemediationPlan

    # --- Agent 3 output ---
    approval_message: str          # Formatted Telegram message text
    telegram_message_id: int       # Message ID after sending to Telegram
    telegram_chat_id: str          # Chat ID used for the HITL interaction

    # --- Execution output ---
    execution_log: str
    execution_success: bool

    # --- Pipeline metadata ---
    incident_id: str               # UUID assigned at ingestion
    status: IncidentStatus
    error: Optional[str]           # Human-readable description of any pipeline error
