"""Shared quality filter for claw-driven agents.

Some local models (especially small instruction-tuned ones) respond to
claw's tool-aware system prompt by emitting a fake JSON tool call
instead of an actual answer. This module owns the heuristic that
detects those failure modes so the smart router and the ensemble
script agree on what counts as a usable reply.
"""

from __future__ import annotations

import re

# Strip ```...``` code fences before inspection -- some models wrap
# their hallucinated tool call in a json fence.
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*\n?|\n?```\s*$", re.M)

# A reply that starts with a JSON object whose first key is one of
# these is almost certainly a hallucinated tool call.
_TOOL_HALLUCINATION = re.compile(
    r'^\s*\{\s*"(name|tool|tool_name|function|arguments|parameters)"',
    re.S,
)


def looks_useful(text: str) -> bool:
    """Return True when *text* looks like a real reply, not a failure mode.

    Filters out:
      - empty / whitespace-only output
      - hallucinated JSON tool calls (with or without a code fence)
      - the agent's own error envelopes ("claw agent ...")
    """
    if not text or not text.strip():
        return False
    stripped = _FENCE.sub("", text).strip()
    if not stripped:
        return False
    if _TOOL_HALLUCINATION.match(stripped):
        return False
    if text.startswith("claw agent"):
        return False
    return True


__all__ = ["looks_useful"]
