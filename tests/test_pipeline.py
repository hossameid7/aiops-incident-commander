"""
tests/test_pipeline.py - Unit and integration tests for AIOps Incident Commander.

Test coverage:
  1. Pydantic v2 model validation (AlertPayload, RCAAnalysis, RemediationPlan).
  2. ChromaDB RAG query function.
  3. LangGraph agent pipeline mock execution.
  4. FastAPI endpoint integration tests via httpx AsyncClient.
  5. Executor dry-run simulation.
"""

from __future__ import annotations

import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import AsyncClient, ASGITransport
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Models tests
# ---------------------------------------------------------------------------

class TestAlertPayload:
    """Validate AlertPayload Pydantic v2 schema."""

    def test_valid_payload(self):
        from app.models import AlertPayload, Severity
        payload = AlertPayload(
            server_name="prod-web-01.internal",
            metric="vm.memory.utilization",
            error_log="OOMKiller invoked: process 'java' killed",
            severity=Severity.HIGH,
            trigger_value="92.4%",
            host_groups=["Production", "Linux servers"],
        )
        assert payload.server_name == "prod-web-01.internal"
        assert payload.severity == Severity.HIGH
        assert payload.trigger_value == "92.4%"

    def test_server_name_lowercased(self):
        from app.models import AlertPayload, Severity
        payload = AlertPayload(
            server_name="PROD-DB-01.INTERNAL",
            metric="disk.used",
            error_log="No space left on device",
            severity=Severity.DISASTER,
        )
        assert payload.server_name == "prod-db-01.internal"

    def test_missing_required_fields_raises(self):
        from app.models import AlertPayload
        with pytest.raises(ValidationError):
            AlertPayload(server_name="host1")  # type: ignore

    def test_invalid_severity_raises(self):
        from app.models import AlertPayload
        with pytest.raises(ValidationError):
            AlertPayload(
                server_name="host1",
                metric="mem",
                error_log="error",
                severity="CRITICAL_INVALID",  # not a valid Severity
            )


class TestRCAAnalysis:
    """Validate RCAAnalysis schema."""

    def test_valid_rca(self):
        from app.models import RCAAnalysis
        rca = RCAAnalysis(
            root_cause="Java heap exhaustion due to memory leak in request handler pool.",
            confidence_score=0.87,
            affected_service="java-api-service",
            supporting_evidence=["OOM Kill event at 02:14 UTC", "Heap grew from 4GB to 16GB in 20 min"],
        )
        assert rca.confidence_score == pytest.approx(0.87)
        assert "java" in rca.affected_service

    def test_confidence_score_out_of_bounds(self):
        from app.models import RCAAnalysis
        with pytest.raises(ValidationError):
            RCAAnalysis(
                root_cause="test",
                confidence_score=1.5,  # must be <= 1.0
                affected_service="svc",
            )

    def test_confidence_score_negative(self):
        from app.models import RCAAnalysis
        with pytest.raises(ValidationError):
            RCAAnalysis(
                root_cause="test",
                confidence_score=-0.1,  # must be >= 0.0
                affected_service="svc",
            )


class TestRemediationPlan:
    """Validate RemediationPlan schema."""

    def test_valid_plan(self):
        from app.models import RemediationPlan, RiskLevel
        plan = RemediationPlan(
            action_title="Restart Java Service",
            command="ansible-playbook -i inventory/production memory_leak.yml -e 'target=prod-web-01'",
            risk_level=RiskLevel.LOW,
            rollback_plan="Start the service again with systemctl start java-api",
        )
        assert plan.risk_level == RiskLevel.LOW

    def test_invalid_risk_level(self):
        from app.models import RemediationPlan
        with pytest.raises(ValidationError):
            RemediationPlan(
                action_title="Test",
                command="echo test",
                risk_level="EXTREME",  # invalid
                rollback_plan="none",
            )


# ---------------------------------------------------------------------------
# RAG tests
# ---------------------------------------------------------------------------

class TestRAG:
    """Test ChromaDB RAG query functionality."""

    def test_query_runbooks_returns_string(self):
        """query_runbooks should return a string in all cases."""
        from app.rag import query_runbooks
        # Without initialisation, it should return a safe message
        result = query_runbooks("high memory usage java heap")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_query_runbooks_with_initialised_collection(self, tmp_path, monkeypatch):
        """After initialisation, a query should return relevant passages."""
        import chromadb
        from chromadb.utils import embedding_functions
        from unittest.mock import patch
        import app.rag as rag_module

        # Create a temporary in-memory ChromaDB client
        client = chromadb.EphemeralClient()
        embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name="all-MiniLM-L6-v2"
        )
        collection = client.get_or_create_collection(
            name="test_runbooks",
            embedding_function=embed_fn,
        )
        collection.add(
            ids=["mem_chunk0"],
            documents=["High memory usage can be caused by a memory leak in the Java process."],
            metadatas=[{"source": "memory_leak.txt", "chunk": 0}],
        )

        # Patch the module-level collection
        with patch.object(rag_module, "_collection", collection):
            result = rag_module.query_runbooks("java memory leak", n_results=1)
            assert "memory" in result.lower()
            assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Executor tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestExecutor:
    """Test remediation executor in dry-run mode."""

    async def test_dry_run_execution_succeeds(self, monkeypatch):
        """In DRY_RUN mode, execute_remediation should always succeed."""
        from config import get_settings
        settings = get_settings()
        monkeypatch.setattr(settings, "dry_run", True)

        with patch("app.executor.get_settings", return_value=settings):
            from app.executor import execute_remediation
            success, log = await execute_remediation(
                command="ansible-playbook -i inventory memory_leak.yml",
                action_title="Test Dry Run",
                incident_id="test-001",
            )

        assert success is True
        assert "SIMULATED" in log or "DRY-RUN" in log

    async def test_execution_log_contains_command(self, monkeypatch):
        """Execution log must reference the command that was run."""
        from config import get_settings
        settings = get_settings()
        monkeypatch.setattr(settings, "dry_run", True)

        with patch("app.executor.get_settings", return_value=settings):
            from app.executor import execute_remediation
            test_cmd = "sudo systemctl restart my-service"
            success, log = await execute_remediation(
                command=test_cmd,
                action_title="Restart Service",
                incident_id="test-002",
            )

        assert test_cmd in log


# ---------------------------------------------------------------------------
# FastAPI integration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestAPIEndpoints:
    """Integration tests for FastAPI endpoints using httpx AsyncClient."""

    @pytest.fixture
    def mock_pipeline_state(self):
        from app.models import (
            AlertPayload, RCAAnalysis, RemediationPlan,
            IncidentStatus, RiskLevel, Severity
        )
        alert = AlertPayload(
            server_name="test-server-01",
            metric="vm.memory.utilization",
            error_log="Memory usage at 95%",
            severity=Severity.HIGH,
        )
        rca = RCAAnalysis(
            root_cause="Java process memory leak.",
            confidence_score=0.85,
            affected_service="java-app",
        )
        remediation = RemediationPlan(
            action_title="Restart Service",
            command="sudo systemctl restart java-app",
            risk_level=RiskLevel.LOW,
            rollback_plan="Start service again.",
        )
        return {
            "alert": alert,
            "rca": rca,
            "remediation": remediation,
            "incident_id": "test-incident-abc",
            "status": IncidentStatus.PENDING_APPROVAL,
            "approval_message": "Test approval message",
        }

    async def test_health_endpoint(self):
        """GET /health should return 200 with healthy status."""
        from app.api import app as fastapi_app
        async with AsyncClient(
            transport=ASGITransport(app=fastapi_app),
            base_url="http://test"
        ) as client:
            response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "components" in data

    async def test_alert_endpoint_valid_payload(self, mock_pipeline_state):
        """POST /api/v1/alert with a valid payload should return 202."""
        from app.api import app as fastapi_app

        with (
            patch("app.api.run_pipeline", new_callable=AsyncMock, return_value=mock_pipeline_state),
            patch("app.api.send_approval_request", new_callable=AsyncMock, return_value=12345),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=fastapi_app),
                base_url="http://test"
            ) as client:
                response = await client.post(
                    "/api/v1/alert",
                    json={
                        "server_name": "prod-db-01",
                        "metric": "vm.memory.utilization",
                        "error_log": "OOM killer triggered",
                        "severity": "HIGH",
                    },
                )
        assert response.status_code == 202
        data = response.json()
        assert "incident_id" in data
        assert data["telegram_message_id"] == 12345

    async def test_alert_endpoint_invalid_payload(self):
        """POST /api/v1/alert with missing required fields should return 422."""
        from app.api import app as fastapi_app
        async with AsyncClient(
            transport=ASGITransport(app=fastapi_app),
            base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/v1/alert",
                json={"server_name": "only-this-field"},
            )
        assert response.status_code == 422

    async def test_health_shows_dry_run_mode(self):
        """Health endpoint should reflect dry_run configuration."""
        from app.api import app as fastapi_app
        from config import get_settings
        async with AsyncClient(
            transport=ASGITransport(app=fastapi_app),
            base_url="http://test"
        ) as client:
            response = await client.get("/health")
        data = response.json()
        assert "dry_run_mode" in data["components"]


# ---------------------------------------------------------------------------
# LangGraph pipeline smoke test (mocked LLM)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestPipelineSmoke:
    """Smoke test the full LangGraph pipeline with mocked Groq responses."""

    async def test_pipeline_end_to_end(self):
        """Run the full 3-agent pipeline with mocked LLM calls."""
        from app.models import AlertPayload, Severity, IncidentStatus
        from app.agents import run_pipeline

        alert = AlertPayload(
            server_name="smoke-test-host-01",
            metric="vfs.fs.size",
            error_log="No space left on device: /var/log partition at 97%",
            severity=Severity.DISASTER,
        )
        initial_state = {
            "alert": alert,
            "incident_id": "smoke-test-001",
            "status": IncidentStatus.PENDING_ANALYSIS,
        }

        mock_rca_json = '{"root_cause": "Disk full due to log accumulation.", "confidence_score": 0.9, "affected_service": "rsyslog", "supporting_evidence": ["97% disk usage"], "runbook_context": "Clear old logs"}'
        mock_remediation_json = '{"action_title": "Clean Log Files", "command": "sudo find /var/log -name *.gz -mtime +7 -delete", "risk_level": "LOW", "rollback_plan": "Logs are not recoverable.", "estimated_duration_seconds": 30, "preconditions": ["Verify log rotation config"]}'

        mock_response_rca = MagicMock()
        mock_response_rca.content = mock_rca_json
        mock_response_remediation = MagicMock()
        mock_response_remediation.content = mock_remediation_json

        with patch("app.agents._get_llm") as mock_llm_factory:
            mock_llm = MagicMock()
            mock_llm.invoke.side_effect = [
                mock_response_rca,
                mock_response_remediation,
            ]
            mock_llm_factory.return_value = mock_llm

            final_state = await run_pipeline(initial_state)

        assert final_state.get("rca") is not None
        assert final_state.get("remediation") is not None
        assert final_state.get("approval_message") is not None
        assert final_state.get("status") in [
            IncidentStatus.PENDING_APPROVAL,
            IncidentStatus.ANALYSED,
            IncidentStatus.FAILED,
        ]
        assert "smoke-test-001" in final_state.get("approval_message", "")
