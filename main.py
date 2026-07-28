"""
main.py - Application entrypoint for AIOps Incident Commander & Self-Healing Gateway.

Architecture decision:
  - The lifespan is attached directly inside app/api.py so that uvicorn can
    reference the app by import string "app.api:app". This ensures uvicorn
    binds the port FIRST and then runs the lifespan — the correct order.
  - main.py only configures and launches uvicorn.
"""

from __future__ import annotations

import logging

import uvicorn

from config import get_settings, configure_logging

logger = logging.getLogger(__name__)


def run_server() -> None:
    """Configure and start the Uvicorn ASGI server."""
    settings = get_settings()
    configure_logging(settings)

    logger.info("=" * 60)
    logger.info("AIOps Incident Commander & Self-Healing Gateway")
    logger.info("Version : 1.0.0  |  Model: %s", settings.groq_model)
    logger.info("DryRun  : %s  |  Port: %d", settings.dry_run, settings.api_port)
    logger.info("=" * 60)

    config = uvicorn.Config(
        app="app.api:app",           # string reference — uvicorn imports it fresh
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        access_log=True,
        reload=False,
    )
    server = uvicorn.Server(config)
    server.run()


if __name__ == "__main__":
    run_server()
