"""
dispatcher/notifier.py
======================
Batched email notifications via smtplib.

notify() queues events — does NOT send immediately.
flush() sends ONE consolidated email per processing cycle.

Events:
    requires_intervention  — agent needs manual login
    error                  — unexpected error after retries
    agent_updated          — agent was auto-updated from GitHub
    tamper_detected        — agent file failed integrity check
"""

import html as _html
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from loguru import logger


_config:  dict                       = {}
_pending: list[tuple[str, str, str]] = []   # (provider, event, detail)


def init(config: dict):
    """Reads SMTP settings from config["app"] (system_config table)."""
    global _config
    app = config.get("app", {})
    _config = {
        "enabled":   str(app.get("smtp_enabled", "false")).lower() == "true",
        "smtp_host": app.get("smtp_host", "smtp.gmail.com"),
        "smtp_port": int(app.get("smtp_port", "587")),
        "username":  app.get("smtp_username", ""),
        "password":  app.get("smtp_password", ""),   # already decrypted by db.get_system_config()
        "recipient": app.get("smtp_recipient", ""),
    }


def _enabled() -> bool:
    return (
        _config.get("enabled", False)
        and _config.get("username")
        and _config.get("recipient")
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def notify(provider: str, event: str, detail: str = ""):
    """
    Queues a notification. Does NOT send an email immediately.
    Call flush() at the end of a processing cycle to send the consolidated email.
    """
    _pending.append((provider, event, detail))
    logger.debug(f"[{provider}] Notification queued: {event}")


def flush():
    """
    Sends ONE consolidated email with all queued notifications grouped by type.
    Clears the queue regardless of whether the email was sent successfully.
    """
    global _pending

    if not _pending:
        return

    pending = list(_pending)
    _pending.clear()

    if not _enabled():
        return

    # Group by event type
    interventions = [(p, d) for p, e, d in pending if e == "requires_intervention"]
    errors        = [(p, d) for p, e, d in pending if e == "error"]
    tampers       = [(p, d) for p, e, d in pending if e == "tamper_detected"]
    updates       = [(p, d) for p, e, d in pending if e == "agent_updated"]
    others        = [(p, e, d) for p, e, d in pending
                     if e not in ("requires_intervention", "error", "tamper_detected", "agent_updated")]

    subject = _build_subject(interventions, errors, tampers, updates, others)
    body    = _build_body(interventions, errors, tampers, updates, others)
    html    = _build_html(interventions, errors, tampers, updates, others)

    _send(subject, body, html)


# ── Email building ─────────────────────────────────────────────────────────────

def _build_subject(interventions, errors, tampers, updates, others) -> str:
    parts = []
    if tampers:
        parts.append(f"⚠ SECURITY: {len(tampers)} tamper alert(s)")
    if interventions:
        parts.append(f"{len(interventions)} agent(s) require attention")
    if errors:
        parts.append(f"{len(errors)} error(s)")
    if updates:
        parts.append(f"{len(updates)} update(s) installed")
    if others:
        parts.append(f"{len(others)} notice(s)")

    return "ATANA — " + " · ".join(parts)


def _build_body(interventions, errors, tampers, updates, others) -> str:
    now      = datetime.now()
    date_str = now.strftime("%d/%m/%Y %H:%M")
    lines    = [f"ATANA Agents — Summary — {date_str}\n"]

    if tampers:
        lines.append("=" * 40)
        lines.append("SECURITY ALERTS")
        lines.append("=" * 40)
        for provider, detail in tampers:
            lines.append(f"  • {provider.upper()}: {detail}")
        lines.append("")

    if interventions:
        lines.append("=" * 40)
        lines.append("REQUIRES HUMAN INTERVENTION")
        lines.append("=" * 40)
        for provider, detail in interventions:
            lines.append(f"  • {provider.upper()}: {detail}")
        lines.append("")
        lines.append("  → Open the ATANA agent manager and click Play on each agent listed above.")
        lines.append("")

    if errors:
        lines.append("=" * 40)
        lines.append("ERRORS")
        lines.append("=" * 40)
        for provider, detail in errors:
            lines.append(f"  • {provider.upper()}: {detail}")
        lines.append("")
        lines.append("  → Check the logs in the ATANA agent manager for more details.")
        lines.append("")

    if updates:
        lines.append("=" * 40)
        lines.append("AGENTS UPDATED")
        lines.append("=" * 40)
        for provider, detail in updates:
            lines.append(f"  • {provider.upper()}: {detail}")
        lines.append("")

    if others:
        lines.append("=" * 40)
        lines.append("OTHER NOTICES")
        lines.append("=" * 40)
        for provider, event, detail in others:
            lines.append(f"  • {provider.upper()} [{event}]: {detail}")
        lines.append("")

    lines.append("— ATANA Agents")
    return "\n".join(lines)


def _build_html(interventions, errors, tampers, updates, others) -> str:
    def rows(items_2: list) -> list[str]:
        return [f"<b>{_html.escape(p.upper())}</b>: {_html.escape(d)}" for p, d in items_2]

    def rows_3(items_3: list) -> list[str]:
        return [f"<b>{_html.escape(p.upper())}</b> [{_html.escape(e)}]: {_html.escape(d)}" for p, e, d in items_3]

    now      = datetime.now()
    date_str = now.strftime("%d/%m/%Y %H:%M")

    def section(title, icon, items, bg, border):
        if not items:
            return ""
        rows_html = "".join(
            f'<tr><td style="padding:8px 12px;border-bottom:1px solid #f5f5f5;'
            f'font-size:13px;color:#333;line-height:1.6">{i}</td></tr>'
            for i in items
        )
        return f"""
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:16px;border-radius:6px;overflow:hidden;border:1px solid {border}">
          <tr>
            <td style="background:{bg};padding:10px 14px;font-size:12px;font-weight:700;
                       color:#fff;letter-spacing:0.5px;text-transform:uppercase">
              {icon}&nbsp; {_html.escape(title)}
            </td>
          </tr>
          {rows_html}
        </table>"""

    sections = (
        section("Alerta de seguridad",      "&#9888;",  rows(tampers),       "#c0392b", "#f5c6cb") +
        section("Requiere intervención",    "&#9654;",  rows(interventions), "#e67e22", "#fde8c8") +
        section("Errores",                  "&#10006;", rows(errors),        "#c0392b", "#f5c6cb") +
        section("Agentes actualizados",     "&#10003;", rows(updates),       "#27ae60", "#c3e6cb") +
        section("Otros avisos",             "&#8226;",  rows_3(others),      "#6c757d", "#dee2e6")
    )

    return f"""<!DOCTYPE html>
<html lang="es">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f4;font-family:Arial,Helvetica,sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;padding:32px 16px">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%">

        <!-- HEADER -->
        <tr>
          <td style="background:#F08C00;border-radius:8px 8px 0 0;padding:24px 28px;text-align:center">
            <img src="https://atana.com.ar/wp-content/uploads/2022/08/cropped-logo-clean-1-1.png"
                 alt="ATANA" height="44"
                 style="display:block;margin:0 auto;max-height:44px;filter:brightness(0) invert(1)">
            <p style="margin:10px 0 0;font-size:12px;color:rgba(255,255,255,0.85);letter-spacing:0.3px">
              Agentes de Descarga Automática
            </p>
          </td>
        </tr>

        <!-- SUBHEADER -->
        <tr>
          <td style="background:#fff;padding:16px 28px 0;border-left:1px solid #e8e8e8;border-right:1px solid #e8e8e8">
            <p style="margin:0;font-size:13px;color:#888">
              Resumen de ejecución &nbsp;·&nbsp; {date_str}
            </p>
            <hr style="border:none;border-top:1px solid #f0f0f0;margin:12px 0 0">
          </td>
        </tr>

        <!-- BODY -->
        <tr>
          <td style="background:#fff;padding:20px 28px;border-left:1px solid #e8e8e8;border-right:1px solid #e8e8e8">
            {sections}
          </td>
        </tr>

        <!-- FOOTER -->
        <tr>
          <td style="background:#fafafa;border:1px solid #e8e8e8;border-top:none;
                     border-radius:0 0 8px 8px;padding:16px 28px;text-align:center">
            <p style="margin:0;font-size:11px;color:#aaa;line-height:1.6">
              Mensaje automático generado por ATANA Agents &nbsp;·&nbsp;
              <a href="https://atana.com.ar" style="color:#F08C00;text-decoration:none">atana.com.ar</a>
            </p>
          </td>
        </tr>

      </table>
    </td></tr>
  </table>
</body>
</html>"""


# ── Send ───────────────────────────────────────────────────────────────────────

def _send(subject: str, body: str, html: str = ""):
    try:
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = _config["username"]
        msg["To"]      = _config["recipient"]

        msg.attach(MIMEText(body, "plain", "utf-8"))
        if html:
            msg.attach(MIMEText(html, "html", "utf-8"))

        with smtplib.SMTP(_config["smtp_host"], _config["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            server.login(_config["username"], _config["password"])
            server.sendmail(_config["username"], _config["recipient"], msg.as_string())

        logger.info(f"Email sent: {subject}")

    except Exception as e:
        logger.warning(f"Error sending email: {e}")
