"""
app/executor.py - Remediation execution layer.

This module is responsible for executing (or safely simulating) the remediation
commands produced by the LangGraph agent pipeline upon Human-In-The-Loop approval.

In DRY_RUN mode (default), commands are logged but never executed on the OS.
Set DRY_RUN=false in .env only in controlled, non-production environments.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import subprocess
from datetime import datetime, timezone
from typing import Optional

from config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_execution_log(
    command: str,
    returncode: int,
    stdout: str,
    stderr: str,
    dry_run: bool,
    start_ts: str,
    end_ts: str,
) -> str:
    """Format a structured execution log string."""
    mode = "[DRY-RUN SIMULATION]" if dry_run else "[LIVE EXECUTION]"
    status = "SUCCESS" if returncode == 0 else f"FAILED (exit code {returncode})"
    lines = [
        f"Execution Report {mode}",
        f"Started  : {start_ts}",
        f"Finished : {end_ts}",
        f"Status   : {status}",
        f"Command  : {command}",
        "",
        "--- stdout ---",
        stdout.strip() if stdout.strip() else "(no output)",
        "",
        "--- stderr ---",
        stderr.strip() if stderr.strip() else "(no output)",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public execution API
# ---------------------------------------------------------------------------

async def execute_remediation(
    command: str,
    action_title: str,
    incident_id: str,
    timeout_seconds: int = 120,
) -> tuple[bool, str]:
    """
    Execute a remediation command asynchronously.

    Args:
        command: The shell command or Ansible invocation to run.
        action_title: Human-readable name of the action (for logging).
        incident_id: UUID of the incident (for correlation in logs).
        timeout_seconds: Maximum wall-clock time before the command is killed.

    Returns:
        A tuple of (success: bool, execution_log: str).
    """
    settings = get_settings()
    start_ts = _timestamp()
    logger.info(
        "Incident %s | Executing remediation '%s' | DRY_RUN=%s",
        incident_id,
        action_title,
        settings.dry_run,
    )

    if settings.dry_run:
        # Simulate execution with a brief sleep to mimic real-world latency
        await asyncio.sleep(2)
        simulated_stdout = (
            f"[SIMULATED] Command '{command}' would have been executed on the target host.\n"
            f"Ansible would have connected via SSH, verified preconditions, and applied changes.\n"
            f"All affected services would have been restarted and health-checked.\n"
            f"Incident ID: {incident_id}"
        )
        log = _build_execution_log(
            command=command,
            returncode=0,
            stdout=simulated_stdout,
            stderr="",
            dry_run=True,
            start_ts=start_ts,
            end_ts=_timestamp(),
        )
        logger.info("Incident %s | DRY-RUN completed successfully.", incident_id)
        return True, log

    # --- Live execution path (DRY_RUN=false) ---
    try:
        args = shlex.split(command)
        logger.warning(
            "Incident %s | LIVE execution commencing: %s",
            incident_id,
            command,
        )
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=float(timeout_seconds)
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            error_msg = (
                f"Command timed out after {timeout_seconds}s and was killed.\n"
                f"Command: {command}"
            )
            logger.error("Incident %s | %s", incident_id, error_msg)
            log = _build_execution_log(
                command=command,
                returncode=-1,
                stdout="",
                stderr=error_msg,
                dry_run=False,
                start_ts=start_ts,
                end_ts=_timestamp(),
            )
            return False, log

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        success = proc.returncode == 0

        log = _build_execution_log(
            command=command,
            returncode=proc.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            dry_run=False,
            start_ts=start_ts,
            end_ts=_timestamp(),
        )

        if success:
            logger.info("Incident %s | Command completed successfully.", incident_id)
        else:
            logger.error(
                "Incident %s | Command failed with exit code %d.",
                incident_id,
                proc.returncode,
            )
        return success, log

    except FileNotFoundError:
        error_msg = (
            f"Executable not found for command: {command}\n"
            "Ensure Ansible, SSH client, or required tools are installed and in PATH."
        )
        logger.error("Incident %s | %s", incident_id, error_msg)
        log = _build_execution_log(
            command=command,
            returncode=127,
            stdout="",
            stderr=error_msg,
            dry_run=False,
            start_ts=start_ts,
            end_ts=_timestamp(),
        )
        return False, log

    except Exception as exc:
        error_msg = f"Unexpected execution error: {exc}"
        logger.error("Incident %s | %s", incident_id, error_msg, exc_info=True)
        log = _build_execution_log(
            command=command,
            returncode=-1,
            stdout="",
            stderr=error_msg,
            dry_run=False,
            start_ts=start_ts,
            end_ts=_timestamp(),
        )
        return False, log


async def execute_ansible_playbook(
    playbook_name: str,
    target_host: str,
    extra_vars: Optional[dict] = None,
    incident_id: str = "unknown",
) -> tuple[bool, str]:
    """
    Convenience wrapper for executing Ansible playbooks with inventory targeting.

    Args:
        playbook_name: Basename of the playbook YAML file (e.g., 'memory_leak.yml').
        target_host: Hostname or IP to target.
        extra_vars: Additional Ansible extra-vars dict.
        incident_id: For log correlation.

    Returns:
        (success, execution_log) tuple.
    """
    settings = get_settings()
    playbook_path = f"{settings.ansible_playbook_dir}/{playbook_name}"
    vars_str = " ".join(f"{k}={v}" for k, v in (extra_vars or {}).items())
    extra_vars_flag = f"-e '{vars_str} target={target_host}'" if vars_str else f"-e 'target={target_host}'"

    command = f"ansible-playbook -i inventory/production {playbook_path} {extra_vars_flag}"
    return await execute_remediation(
        command=command,
        action_title=f"Ansible: {playbook_name}",
        incident_id=incident_id,
    )
