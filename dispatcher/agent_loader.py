"""
dispatcher/agent_loader.py
==========================
Loads agent classes from the static registry.

Agents are bundled inside the exe (PyInstaller hidden imports).
No external files, no Ed25519 signatures, no SHA256 verification needed.

Agents that are not present in the repo (excluded from sync) are simply
omitted from the registry — the dispatcher skips unknown providers gracefully.
"""
from agents.base import AgentBase

_REGISTRY: dict[str, type[AgentBase]] = {}

# Core agents — always present
from agents.fiserv      import FiservAgent
from agents.mercadopago import MercadoPagoAgent

_REGISTRY["fiserv"]      = FiservAgent
_REGISTRY["mercadopago"] = MercadoPagoAgent

def load_agent(name: str) -> type[AgentBase]:
    """Returns the agent class for the given provider name."""
    cls = _REGISTRY.get(name)
    if not cls:
        raise ValueError(f"Unknown agent: '{name}'. Known providers: {list(_REGISTRY)}")
    return cls


def known_providers() -> list[str]:
    """Returns all registered provider names."""
    return list(_REGISTRY.keys())
