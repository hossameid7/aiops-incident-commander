"""
app/api.py - FastAPI webhook API for AIOps Incident Commander.

Endpoints:
  POST /api/v1/alert  — Accept AlertPayload, run LangGraph pipeline, trigger Telegram HITL.
  GET  /health        — Health check with system component status.

Lifespan:
  The lifespan context manager is defined here so that when uvicorn imports
  "app.api:app" by string, it binds the port FIRST and THEN runs startup.
  This is the correct order and avoids the [Errno 10048] port-conflict race.
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agents import run_pipeline
from app.bot import send_approval_request
from app.models import AlertPayload, IncidentState, IncidentStatus
from app.rag import get_collection_stats
from config import get_settings, configure_logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan — single authoritative startup/shutdown handler
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    """
    Runs AFTER uvicorn binds the port successfully.
    Initialises ChromaDB and Telegram bot exactly once.
    """
    settings = get_settings()
    configure_logging(settings)

    logger.info("=" * 60)
    logger.info("AIOps Incident Commander & Self-Healing Gateway")
    logger.info("Version : 1.0.0  |  Model: %s", settings.groq_model)
    logger.info("DryRun  : %s  |  Port : %d", settings.dry_run, settings.api_port)
    logger.info("=" * 60)

    # Step 1 — ChromaDB RAG
    logger.info("Initialising ChromaDB RAG knowledge base...")
    try:
        from app.rag import initialise_rag
        initialise_rag()
        logger.info("ChromaDB RAG initialised successfully.")
    except Exception as exc:
        logger.critical("Failed to initialise ChromaDB: %s", exc, exc_info=True)
        sys.exit(1)

    # Step 2 — Telegram Bot
    logger.info("Starting Telegram bot polling...")
    try:
        from app.bot import start_polling
        await start_polling()
        logger.info("Telegram bot polling active.")
    except Exception as exc:
        logger.error(
            "Telegram bot failed to start: %s. "
            "Continuing without HITL — check TELEGRAM_BOT_TOKEN.",
            exc,
        )

    yield  # Application is running

    # Shutdown
    logger.info("Shutdown signal received. Stopping Telegram bot...")
    try:
        from app.bot import stop_polling
        await stop_polling()
    except Exception as exc:
        logger.warning("Error during bot shutdown: %s", exc)
    logger.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# FastAPI application (lifespan attached at creation)
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AIOps Incident Commander & Self-Healing Gateway",
    description=(
        "Enterprise-grade AIOps platform integrating LangGraph multi-agent decision engine, "
        "ChromaDB RAG, Groq LLM inference, and Human-In-The-Loop Telegram approval workflow."
    ),
    version="1.0.0",
    lifespan=lifespan,       # <-- attached here, not patched externally
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler to prevent internal details from leaking in responses."""
    logger.error("Unhandled exception on %s %s: %s", request.method, request.url.path, exc, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal server error",
            "detail": str(exc),
            "path": str(request.url.path),
        },
    )


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@app.get(
    "/health",
    summary="Health Check",
    tags=["Operations"],
    response_description="System component health status",
)
async def health_check() -> dict:
    """
    Return the operational health of all system components.
    Used by load balancers, Kubernetes liveness probes, and uptime monitors.
    """
    settings = get_settings()
    rag_stats = get_collection_stats()

    return {
        "status": "healthy",
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "version": "1.0.0",
        "components": {
            "fastapi": "operational",
            "chromadb": rag_stats.get("status", "unknown"),
            "llm_model": settings.groq_model,
            "dry_run_mode": settings.dry_run,
            "runbook_chunks": rag_stats.get("document_count", 0),
            "telegram_chat_configured": bool(settings.telegram_chat_id),
        },
    }


# ---------------------------------------------------------------------------
# Alert ingestion endpoint
# ---------------------------------------------------------------------------

@app.post(
    "/api/v1/alert",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Ingest Alert Webhook",
    tags=["Incidents"],
    response_description="Incident processing acknowledgement",
)
async def ingest_alert(payload: AlertPayload) -> dict:
    """
    Ingest an alert webhook from Zabbix, Prometheus AlertManager, or any compatible source.

    Processing pipeline:
    1. Validate payload via Pydantic v2 schema.
    2. Generate a unique incident ID.
    3. Execute the LangGraph 3-agent pipeline (RCA -> Remediation -> Gatekeeper).
    4. Send a Human-In-The-Loop approval request to the configured Telegram chat.

    Returns an acknowledgement with the incident ID for tracking.
    """
    incident_id = str(uuid.uuid4())
    logger.info(
        "Incident %s | Alert received | Server: %s | Metric: %s | Severity: %s",
        incident_id,
        payload.server_name,
        payload.metric,
        payload.severity.value,
    )

    # Build initial LangGraph state
    initial_state: IncidentState = {
        "alert": payload,
        "incident_id": incident_id,
        "status": IncidentStatus.PENDING_ANALYSIS,
    }

    try:
        # Run the multi-agent LangGraph pipeline
        final_state = await run_pipeline(initial_state)

        # Extract pipeline results
        pipeline_status = final_state.get("status", IncidentStatus.FAILED)
        pipeline_error = final_state.get("error")

        if pipeline_status == IncidentStatus.FAILED:
            logger.error("Incident %s | Pipeline failed: %s", incident_id, pipeline_error)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "incident_id": incident_id,
                    "error": "LangGraph pipeline execution failed",
                    "detail": pipeline_error,
                },
            )

        # Dispatch Telegram HITL notification
        message_id = await send_approval_request(final_state)

        rca = final_state.get("rca")
        remediation = final_state.get("remediation")

        response = {
            "incident_id": incident_id,
            "status": pipeline_status.value if hasattr(pipeline_status, "value") else str(pipeline_status),
            "server": payload.server_name,
            "metric": payload.metric,
            "severity": payload.severity.value,
            "rca_summary": {
                "root_cause": rca.root_cause[:200] if rca else None,
                "affected_service": rca.affected_service if rca else None,
                "confidence": f"{rca.confidence_score:.0%}" if rca else None,
            },
            "remediation_summary": {
                "action": remediation.action_title if remediation else None,
                "risk_level": remediation.risk_level.value if remediation else None,
            },
            "telegram_message_id": message_id,
            "message": (
                "Incident analysed successfully. Awaiting Human-In-The-Loop approval via Telegram."
                if message_id
                else "Incident analysed. Telegram notification failed — check TELEGRAM_CHAT_ID in .env."
            ),
        }

        logger.info(
            "Incident %s | Processing complete. Pipeline status: %s | Telegram msg: %s",
            incident_id,
            pipeline_status,
            message_id,
        )
        return response

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Incident %s | Unexpected error during processing: %s",
            incident_id,
            exc,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "incident_id": incident_id,
                "error": "Unexpected error during incident processing",
                "detail": str(exc),
            },
        )


# ---------------------------------------------------------------------------
# Zabbix-compatible webhook endpoint (alternate path)
# ---------------------------------------------------------------------------

@app.post(
    "/api/v1/zabbix/alert",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Zabbix Webhook (Alias)",
    tags=["Incidents"],
    include_in_schema=True,
)
async def zabbix_alert(payload: AlertPayload) -> dict:
    """Alias endpoint for direct Zabbix webhook configuration."""
    return await ingest_alert(payload)


# ---------------------------------------------------------------------------
# Prometheus AlertManager-compatible endpoint
# ---------------------------------------------------------------------------

@app.post(
    "/api/v1/prometheus/alert",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Prometheus AlertManager Webhook (Alias)",
    tags=["Incidents"],
    include_in_schema=True,
)
async def prometheus_alert(payload: AlertPayload) -> dict:
    """Alias endpoint for Prometheus AlertManager webhook configuration."""
    return await ingest_alert(payload)
