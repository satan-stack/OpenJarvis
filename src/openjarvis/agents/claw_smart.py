"""ClawSmartAgent -- token-saving router around :class:`ClawCodeAgent`.

Routes Anthropic-alias model requests through a local model first,
falling back to the real Anthropic alias only when the local reply
trips a quality gate. The point is to save Anthropic tokens for the
cases the local model genuinely can't handle, while keeping `opus`
always-direct because there's no useful local equivalent.

Default routing (override via the ``model_routes`` constructor arg):

============  ================================================
Asked model   Cascade chain
============  ================================================
``haiku``     ``["openai/qwen2.5-coder:1.5b", "haiku"]``
``sonnet``    ``["openai/qwen2.5-coder:1.5b", "sonnet"]``
``opus``      ``["opus"]``  -- always Anthropic
anything      ``[<as-is>]`` -- pass-through
============  ================================================

The agent reuses :class:`ClawCodeAgent` per cascade step, so all the
existing knobs (permission mode, allowed tools, workspace, timeout,
extra args) work unchanged.

Authentication:

- The local step needs ``OPENAI_BASE_URL`` + ``OPENAI_API_KEY`` (or any
  base URL claw's OpenAI-compatible backend can hit, including Ollama).
- The Anthropic fallback needs ``ANTHROPIC_API_KEY`` (or ``ANTHROPIC_AUTH_TOKEN``).
  If the fallback is invoked and the credential is missing, claw will
  return its own error envelope and the agent will surface it.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from openjarvis.agents._claw_quality import looks_useful as _default_quality_gate
from openjarvis.agents._stubs import AgentContext, AgentResult, BaseAgent
from openjarvis.agents.claw_code import ClawCodeAgent
from openjarvis.core.events import EventBus
from openjarvis.core.registry import AgentRegistry
from openjarvis.engine._stubs import InferenceEngine

# Local model used as the cheap-first hop for haiku / sonnet. Override
# globally via the ``model_routes`` constructor arg or per-call.
DEFAULT_LOCAL_MODEL = "openai/qwen2.5-coder:1.5b"

DEFAULT_MODEL_ROUTES: Dict[str, List[str]] = {
    "haiku": [DEFAULT_LOCAL_MODEL, "haiku"],
    "sonnet": [DEFAULT_LOCAL_MODEL, "sonnet"],
    "opus": ["opus"],
}


@AgentRegistry.register("claw_smart")
class ClawSmartAgent(BaseAgent):
    """Smart router that cascades qwen2.5 → Anthropic for cheap aliases.

    All ``ClawCodeAgent`` constructor arguments are accepted and
    forwarded to each cascade step. The only new parameters are
    ``model_routes`` (which alias maps to which cascade chain) and
    ``quality_gate`` (the predicate that decides whether to stop or
    keep cascading).
    """

    agent_id = "claw_smart"
    accepts_tools = False
    _default_temperature = 0.7
    _default_max_tokens = 1024

    def __init__(
        self,
        engine: InferenceEngine,
        model: str,
        *,
        bus: Optional[EventBus] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        binary: str = "",
        workspace: str = "",
        session_id: str = "",
        allowed_tools: Optional[List[str]] = None,
        permission_mode: str = "workspace-write",
        resume: str = "",
        timeout: int = 300,
        extra_args: Optional[List[str]] = None,
        model_routes: Optional[Dict[str, List[str]]] = None,
        quality_gate: Optional[Callable[[str], bool]] = None,
    ) -> None:
        super().__init__(
            engine,
            model,
            bus=bus,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self._binary = binary
        self._workspace = workspace
        self._session_id = session_id
        self._allowed_tools = allowed_tools
        self._permission_mode = permission_mode
        self._resume = resume
        self._timeout = timeout
        self._extra_args = list(extra_args or [])
        self._model_routes = dict(model_routes or DEFAULT_MODEL_ROUTES)
        self._quality_gate = quality_gate or _default_quality_gate

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _resolve_chain(self, model: str) -> List[str]:
        """Return the cascade chain for *model*.

        Pass-through for anything not in the route table.
        """
        return list(self._model_routes.get(model, [model]))

    def _spawn(self, model: str) -> ClawCodeAgent:
        return ClawCodeAgent(
            engine=self._engine,
            model=model,
            bus=self._bus,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            binary=self._binary,
            workspace=self._workspace,
            session_id=self._session_id,
            allowed_tools=self._allowed_tools,
            permission_mode=self._permission_mode,
            resume=self._resume,
            timeout=self._timeout,
            extra_args=self._extra_args,
        )

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        self._emit_turn_start(input)

        chain = self._resolve_chain(self._model)
        attempts: list[dict[str, Any]] = []
        last: Optional[AgentResult] = None

        for step, model in enumerate(chain):
            agent = self._spawn(model)
            result = agent.run(input, context=context, **kwargs)
            attempts.append(
                {
                    "model": model,
                    "step": step,
                    "ok": self._quality_gate(result.content),
                    "metadata": result.metadata,
                }
            )
            last = result
            if self._quality_gate(result.content) and not result.metadata.get("error"):
                # Annotate the winning result with the cascade trace so
                # downstream telemetry can see what was tried.
                merged_meta = dict(result.metadata or {})
                merged_meta.update(
                    {
                        "claw_smart_chain": chain,
                        "claw_smart_winner": model,
                        "claw_smart_attempts": attempts,
                    }
                )
                self._emit_turn_end(turns=1)
                return AgentResult(
                    content=result.content,
                    tool_results=result.tool_results,
                    turns=result.turns,
                    metadata=merged_meta,
                )

        # Every link in the chain failed the quality gate; return the
        # last attempt's content (so the user still sees something) but
        # mark the result as a smart-cascade exhaustion.
        self._emit_turn_end(turns=1, smart_cascade_exhausted=True)
        if last is None:
            return AgentResult(
                content=(
                    f"claw_smart: empty cascade chain for model {self._model!r}. "
                    "Configure model_routes."
                ),
                turns=1,
                metadata={
                    "error": True,
                    "error_type": "empty_chain",
                    "claw_smart_chain": chain,
                },
            )
        merged_meta = dict(last.metadata or {})
        merged_meta.update(
            {
                "claw_smart_chain": chain,
                "claw_smart_winner": None,
                "claw_smart_attempts": attempts,
                "claw_smart_exhausted": True,
            }
        )
        return AgentResult(
            content=last.content,
            tool_results=last.tool_results,
            turns=last.turns,
            metadata=merged_meta,
        )


__all__ = ["ClawSmartAgent", "DEFAULT_MODEL_ROUTES", "DEFAULT_LOCAL_MODEL"]
