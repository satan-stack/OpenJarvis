"""ClawCodeAgent -- wraps the claw Rust CLI as a subprocess.

Spawns the ``claw`` binary from the
`satan-stack/claw-code <https://github.com/satan-stack/claw-code>`_ project
in one-shot ``prompt`` mode and consumes its JSON output.

This is the Rust counterpart to :class:`ClaudeCodeAgent`. Both agents wrap a
Claude-Code-style harness, but ``claw`` ships as a single Rust binary with
no Node.js dependency. They share the same :class:`BaseAgent` surface so
presets and orchestrators can swap between them by changing one config
key.

The ``engine`` parameter is accepted for interface conformance with
:class:`BaseAgent` but is not used -- inference is handled entirely by the
claw binary, which talks to the configured model provider directly.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, List, Optional

from openjarvis.agents._stubs import AgentContext, AgentResult, BaseAgent
from openjarvis.core.events import EventBus
from openjarvis.core.registry import AgentRegistry
from openjarvis.core.types import ToolResult
from openjarvis.engine._stubs import InferenceEngine

logger = logging.getLogger(__name__)

# Permission modes accepted by the claw CLI.
_PERMISSION_MODES = ("read-only", "workspace-write", "danger-full-access")
_DEFAULT_PERMISSION_MODE = "workspace-write"


@AgentRegistry.register("claw_code")
class ClawCodeAgent(BaseAgent):
    """Agent that wraps the ``claw`` Rust CLI via a subprocess.

    Invokes ``claw --output-format json prompt <input>`` in the configured
    workspace and parses the resulting JSON. The ``engine`` argument is
    accepted for :class:`BaseAgent` interface conformance but is not used
    -- claw drives inference directly against its own configured provider
    (Anthropic, xAI, OpenAI-compatible, DashScope, or any local server
    speaking those wire formats).

    Authentication is forwarded via environment variables. Set whichever
    of ``ANTHROPIC_API_KEY``, ``ANTHROPIC_AUTH_TOKEN``, ``OPENAI_API_KEY``,
    ``OPENAI_BASE_URL``, ``XAI_API_KEY``, or ``DASHSCOPE_API_KEY`` matches
    the model you intend ``claw`` to use; the agent passes the parent
    environment through unchanged.
    """

    agent_id = "claw_code"
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
        permission_mode: str = _DEFAULT_PERMISSION_MODE,
        resume: str = "",
        timeout: int = 300,
        extra_args: Optional[List[str]] = None,
    ) -> None:
        super().__init__(
            engine,
            model,
            bus=bus,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        if permission_mode not in _PERMISSION_MODES:
            raise ValueError(
                f"permission_mode must be one of {_PERMISSION_MODES}, "
                f"got {permission_mode!r}"
            )

        self._binary = binary or os.environ.get("CLAW_BINARY", "") or "claw"
        self._workspace = workspace or os.getcwd()
        self._session_id = session_id
        self._allowed_tools = allowed_tools
        self._permission_mode = permission_mode
        self._resume = resume
        self._timeout = timeout
        self._extra_args = list(extra_args or [])

    # ------------------------------------------------------------------
    # Binary discovery
    # ------------------------------------------------------------------

    def _resolve_binary(self) -> str:
        """Return an absolute path to the claw binary, or raise.

        Resolution order:

        1. The explicit ``binary`` argument / ``CLAW_BINARY`` env var if it
           points at an existing file.
        2. ``shutil.which("claw")`` -- works for ``cargo install`` users
           with ``~/.cargo/bin`` on ``PATH``.
        3. The debug build at ``./rust/target/debug/claw`` relative to the
           configured workspace, for users running straight out of a
           cloned ``claw-code`` checkout.
        """
        candidate = Path(self._binary).expanduser()
        if candidate.is_file():
            return str(candidate)

        on_path = shutil.which(self._binary)
        if on_path:
            return on_path

        debug_build = Path(self._workspace) / "rust" / "target" / "debug" / "claw"
        if debug_build.is_file():
            return str(debug_build)

        raise RuntimeError(
            "ClawCodeAgent could not locate the `claw` binary. Build it from "
            "https://github.com/satan-stack/claw-code (`cargo build --workspace` "
            "in the `rust/` directory) and either add it to your PATH or set "
            "the CLAW_BINARY environment variable to its absolute path."
        )

    # ------------------------------------------------------------------
    # CLI argument assembly
    # ------------------------------------------------------------------

    def _build_argv(self, binary: str, prompt: str) -> list[str]:
        argv: list[str] = [binary, "--output-format", "json"]

        if self._model:
            argv.extend(["--model", self._model])
        if self._permission_mode:
            argv.extend(["--permission-mode", self._permission_mode])
        if self._allowed_tools:
            argv.extend(["--allowedTools", ",".join(self._allowed_tools)])
        if self._resume:
            argv.extend(["--resume", self._resume])

        argv.extend(self._extra_args)
        argv.extend(["prompt", prompt])
        return argv

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        """Execute a single ``claw prompt`` invocation and return the result."""
        self._emit_turn_start(input)

        try:
            binary = self._resolve_binary()
        except RuntimeError as exc:
            self._emit_turn_end(turns=1, error=True)
            return AgentResult(
                content=str(exc),
                turns=1,
                metadata={"error": True, "error_type": "binary_missing"},
            )

        argv = self._build_argv(binary, input)

        try:
            proc = subprocess.run(
                argv,
                cwd=self._workspace,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired:
            self._emit_turn_end(turns=1, error=True)
            return AgentResult(
                content=f"claw agent timed out after {self._timeout}s.",
                turns=1,
                metadata={"error": True, "error_type": "timeout"},
            )
        except FileNotFoundError as exc:
            self._emit_turn_end(turns=1, error=True)
            return AgentResult(
                content=f"claw binary not executable: {exc}",
                turns=1,
                metadata={"error": True, "error_type": "binary_missing"},
            )

        if proc.returncode != 0:
            stderr = proc.stderr.strip() if proc.stderr else "Unknown error"
            logger.error("claw exited with code %d: %s", proc.returncode, stderr)
            self._emit_turn_end(turns=1, error=True)
            return AgentResult(
                content=f"claw agent failed: {stderr}",
                turns=1,
                metadata={"error": True, "returncode": proc.returncode},
            )

        content, tool_results, metadata = self._parse_output(proc.stdout)

        self._emit_turn_end(turns=1)
        return AgentResult(
            content=content,
            tool_results=tool_results,
            turns=1,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Output parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_output(
        stdout: str,
    ) -> tuple[str, list[ToolResult], dict[str, Any]]:
        """Parse ``claw --output-format json`` stdout into agent fields.

        claw emits a single JSON object on stdout. We tolerate it being
        embedded in surrounding log noise by scanning for the first ``{``
        and the last ``}``. If the payload is not valid JSON we return
        the raw stdout as plain content.
        """
        text = stdout.strip()
        if not text:
            return "", [], {}

        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return text, [], {}

        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return text, [], {"parse_error": True}

        if not isinstance(data, dict):
            return text, [], {"parse_error": True}

        # claw uses a few different field names depending on the verb. Try
        # the common ones and fall back to a string round-trip of the
        # whole payload so the user never loses information.
        content = (
            data.get("content")
            or data.get("message")
            or data.get("output")
            or data.get("result")
            or ""
        )
        if not isinstance(content, str):
            content = json.dumps(content)

        raw_tools = data.get("tool_results") or data.get("tools") or []
        tool_results: list[ToolResult] = []
        if isinstance(raw_tools, list):
            for tr in raw_tools:
                if not isinstance(tr, dict):
                    continue
                tool_results.append(
                    ToolResult(
                        tool_name=tr.get("tool_name")
                        or tr.get("name", "unknown"),
                        content=tr.get("content")
                        or tr.get("output", ""),
                        success=tr.get("success", True),
                    )
                )

        metadata: dict[str, Any] = {}
        for key in ("session_id", "model", "usage", "cost", "turns", "finish_reason"):
            if key in data:
                metadata[key] = data[key]
        if not metadata and "metadata" in data and isinstance(data["metadata"], dict):
            metadata = dict(data["metadata"])

        return content, tool_results, metadata


__all__ = ["ClawCodeAgent"]
