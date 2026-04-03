"""
Agent seam: response schema helpers.

This codebase currently builds the response schema inside `agent_turn()`;
we re-export the entrypoint for modular seams without changing behavior.
"""

from src.agent.agent import agent_turn

__all__ = ["agent_turn"]

