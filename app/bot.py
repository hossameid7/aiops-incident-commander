"""
app/bot.py - Telegram Bot for Human-In-The-Loop (HITL) incident approval.

Uses python-telegram-bot v20+ async API with ApplicationBuilder.
Provides:
  - send_approval_request(): Sends an incident approval message with inline keyboard.
  - Callback handlers for [Approve Execution] and [Reject] buttons.
  - Real-time execution log editing on the same Telegram message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from app.executor import execute_remediation
from app.models import IncidentState, IncidentStatus, RemediationPlan
from config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory pending incidents store
# ---------------------------------------------------------------------------
# Maps callback_data key -> IncidentState snapshot for HITL resolution.
# In a production system, this would be persisted in Redis or a database.
_pending_incidents: dict[str, IncidentState] = {}

# ---------------------------------------------------------------------------
# Callback data constants
# ---------------------------------------------------------------------------
APPROVE_PREFIX = "approve:"
REJECT_PREFIX = "reject:"


# ---------------------------------------------------------------------------
# Module-level Application singleton
# ---------------------------------------------------------------------------
_application: Optional[Application] = None


def get_application() -> Application:
    """Return the module-level Telegram Application, building it on first call."""
    global _application
    if _application is None:
        settings = get_settings()
        _application = (
            ApplicationBuilder()
            .token(settings.telegram_bot_token)
            .build()
        )
        _register_handlers(_application)
        logger.info("Telegram Application built and handlers registered.")
    return _application


def _register_handlers(app: Application) -> None:
    """Register all command and callback query handlers."""
    app.add_handler(CommandHandler("start", _cmd_start))
    app.add_handler(CommandHandler("status", _cmd_status))
    app.add_handler(CallbackQueryHandler(_handle_approve, pattern=f"^{APPROVE_PREFIX}"))
    app.add_handler(CallbackQueryHandler(_handle_reject, pattern=f"^{REJECT_PREFIX}"))


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def _cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command — confirm bot is active and return chat ID."""
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"*AIOps Incident Commander Bot is active.*\n\n"
        f"This bot handles Human-In-The-Loop approval for automated incident remediation.\n\n"
        f"Your Chat ID: `{chat_id}`\n"
        f"Set this as `TELEGRAM_CHAT_ID` in your `.env` file.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def _cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status command — show count of pending incidents."""
    count = len(_pending_incidents)
    msg = (
        f"*AIOps Bot Status*\n"
        f"Pending incidents awaiting approval: `{count}`"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


# ---------------------------------------------------------------------------
# Callback query handlers
# ---------------------------------------------------------------------------

async def _handle_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the [Approve Execution] button press.
    Triggers remediation execution and edits the Telegram message with live results.
    """
    query = update.callback_query
    await query.answer()

    incident_id = query.data.replace(APPROVE_PREFIX, "")
    state = _pending_incidents.get(incident_id)

    if not state:
        await query.edit_message_text(
            f"*Error:* Incident `{incident_id}` not found in pending queue.\n"
            "It may have already been processed or the bot was restarted.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    remediation: RemediationPlan = state.get("remediation")
    if not remediation:
        await query.edit_message_text(
            f"*Error:* No remediation plan found for incident `{incident_id}`.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # Update message to show execution in progress
    await query.edit_message_text(
        f"*Execution Approved — Running Remediation*\n\n"
        f"Incident: `{incident_id}`\n"
        f"Action: *{remediation.action_title}*\n"
        f"Risk Level: `{remediation.risk_level.value}`\n\n"
        f"_Executing... Please wait._",
        parse_mode=ParseMode.MARKDOWN,
    )

    logger.info("Incident %s | APPROVED by operator via Telegram.", incident_id)

    # Execute the remediation asynchronously
    success, execution_log = await execute_remediation(
        command=remediation.command,
        action_title=remediation.action_title,
        incident_id=incident_id,
    )

    # Update state
    state["execution_success"] = success
    state["execution_log"] = execution_log
    state["status"] = IncidentStatus.RESOLVED if success else IncidentStatus.FAILED

    # Remove from pending queue
    _pending_incidents.pop(incident_id, None)

    status_icon = "✅" if success else "❌"
    status_text = "RESOLVED" if success else "FAILED"

    # Truncate log to fit within Telegram's 4096 char message limit
    log_preview = execution_log[:800].replace("`", "'")

    result_message = (
        f"*Remediation Execution Report* {status_icon}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Incident: `{incident_id}`\n"
        f"Status: *{status_text}*\n"
        f"Action: *{remediation.action_title}*\n\n"
        f"*Execution Log:*\n"
        f"```\n{log_preview}\n```"
    )

    try:
        await query.edit_message_text(result_message, parse_mode=ParseMode.MARKDOWN)
    except TelegramError as te:
        logger.warning("Could not edit Telegram message: %s", te)
        # Fallback: send as a new message
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=result_message,
            parse_mode=ParseMode.MARKDOWN,
        )

    logger.info("Incident %s | Execution completed. Success=%s", incident_id, success)


async def _handle_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle the [Reject] button press.
    Marks the incident as rejected and updates the Telegram message.
    """
    query = update.callback_query
    await query.answer()

    incident_id = query.data.replace(REJECT_PREFIX, "")
    state = _pending_incidents.pop(incident_id, None)

    if state:
        state["status"] = IncidentStatus.REJECTED
        logger.warning("Incident %s | REJECTED by operator via Telegram.", incident_id)

    await query.edit_message_text(
        f"*Remediation Rejected* ❌\n\n"
        f"Incident `{incident_id}` has been rejected by the operator.\n"
        "No automated changes have been applied to the target host.\n\n"
        "_Please investigate manually and apply fixes as appropriate._",
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# Public API — used by api.py
# ---------------------------------------------------------------------------

async def send_approval_request(state: IncidentState) -> Optional[int]:
    """
    Send an interactive approval message to the configured Telegram chat.

    Args:
        state: The final IncidentState produced by the LangGraph pipeline.
               Must contain 'approval_message', 'incident_id', and 'remediation'.

    Returns:
        The Telegram message_id of the sent message, or None on failure.
    """
    settings = get_settings()
    chat_id = settings.telegram_chat_id
    incident_id = state.get("incident_id", "unknown")
    approval_message = state.get("approval_message", "Incident requires approval.")

    if not chat_id:
        logger.error(
            "TELEGRAM_CHAT_ID is not set. Cannot send approval request for incident %s. "
            "Start the bot with /start to get your Chat ID.",
            incident_id,
        )
        return None

    # Register incident in pending queue
    _pending_incidents[incident_id] = state

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                text="✅  Approve Execution",
                callback_data=f"{APPROVE_PREFIX}{incident_id}",
            ),
            InlineKeyboardButton(
                text="❌  Reject",
                callback_data=f"{REJECT_PREFIX}{incident_id}",
            ),
        ]
    ])

    try:
        app = get_application()
        message = await app.bot.send_message(
            chat_id=chat_id,
            text=approval_message,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )
        logger.info(
            "Incident %s | Approval request sent to Telegram chat %s (message_id=%d).",
            incident_id,
            chat_id,
            message.message_id,
        )
        return message.message_id

    except TelegramError as te:
        logger.error(
            "Incident %s | Failed to send Telegram message: %s", incident_id, te
        )
        _pending_incidents.pop(incident_id, None)
        return None


async def start_polling() -> None:
    """
    Start the Telegram bot in polling mode as an async background task.
    This should be called once at application startup.
    """
    app = get_application()
    await app.initialize()
    await app.start()
    logger.info("Telegram bot polling started.")
    await app.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


async def stop_polling() -> None:
    """Gracefully stop the Telegram bot polling."""
    global _application
    if _application and _application.updater and _application.updater.running:
        await _application.updater.stop()
        await _application.stop()
        await _application.shutdown()
        logger.info("Telegram bot polling stopped.")
