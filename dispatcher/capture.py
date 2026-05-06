"""
dispatcher/capture.py
=====================
Router for the --capture-session PROVIDER flag.

Delegates entirely to each agent's capture_session() method.
Each agent knows how to capture its own session:
  - auto-fill credentials
  - detect login completion
  - save storage_state / cookies / tokens
  - authorize the pending job

To add capture support to a new agent, override capture_session() in that
agent's class. AgentBase raises NotImplementedError by default.
"""

from loguru import logger

from dispatcher import db
from dispatcher.agent_loader import load_agent


def capture_session(provider: str, config: dict):
    """Loads the agent and delegates to its capture_session() method.

    If capture fails for any reason (browser closed, timeout, exception),
    the job is reset to requires_intervention so the user can try again.
    Without this, the job stays in requires_intervention but create_job()
    blocks new jobs for that provider, effectively locking the agent.
    """
    try:
        cls   = load_agent(provider)
        agent = cls(config)
    except Exception as e:
        logger.error(f"[{provider}] Could not load agent: {e}")
        _reset_intervention(provider, f"Could not load agent: {e}")
        return

    try:
        agent.capture_session()
    except NotImplementedError:
        logger.error(
            f"[{provider}] Session capture not supported — "
            "this agent does not use a browser session"
        )
        _reset_intervention(provider, "Session capture not supported")
    except Exception as e:
        logger.exception(f"[{provider}] Session capture failed: {e}")
        _reset_intervention(provider, f"Capture failed: {e}")


def _reset_intervention(provider: str, reason: str):
    """Keeps the job in requires_intervention so the user can retry via Play."""
    try:
        job = db.get_intervention_job(provider)
        if job:
            db.update_job_intervention(job["id"], reason)
            logger.info(f"[{provider}] Job reset to requires_intervention — user can try Play again")
    except Exception as e:
        logger.warning(f"[{provider}] Could not reset job: {e}")
