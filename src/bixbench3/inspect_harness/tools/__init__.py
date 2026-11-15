"""Custom Inspect tools for BixBench3 agents."""

from .subagent import run_subagent, run_subagents
from .web_access import request_web_access

__all__ = ["request_web_access", "run_subagent", "run_subagents"]
