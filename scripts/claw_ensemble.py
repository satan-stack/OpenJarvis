"""Parallel claw-code ensemble: race multiple models, take the first good answer.

Spawns ``ClawCodeAgent`` against several Ollama models concurrently. The
first response that passes a quality filter wins; remaining workers are
cancelled.

The point is *latency*: on CPU, the smallest competent model is usually
fastest, but it sometimes hallucinates (fake tool calls, empty replies).
A second, slightly-larger model running in parallel acts as insurance --
if the small model trips, the second's reply is already in flight.

Usage:
    export CLAW_BINARY=/path/to/claw OPENAI_BASE_URL=http://127.0.0.1:11434/v1 \
           OPENAI_API_KEY=ollama
    python scripts/claw_ensemble.py "your prompt here"
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from openjarvis.agents._claw_quality import looks_useful
from openjarvis.agents.claw_code import ClawCodeAgent

# Ordered by preference. First entry is the "primary" -- if it returns
# clean content we never wait for the others.
DEFAULT_MODELS: list[str] = [
    "openai/qwen2.5-coder:1.5b",   # fast + code-specialized
    "openai/llama3.2:3b",          # general-purpose backup
]

def run_one(model: str, prompt: str, *, workspace: str, timeout: int) -> dict:
    agent = ClawCodeAgent(
        engine=None,
        model=model,
        workspace=workspace,
        permission_mode="read-only",
        timeout=timeout,
    )
    t0 = time.monotonic()
    result = agent.run(prompt)
    return {
        "model": model,
        "elapsed": time.monotonic() - t0,
        "content": result.content,
        "metadata": result.metadata,
    }


def race(
    prompt: str,
    *,
    models: list[str] | None = None,
    workspace: str = ".",
    timeout: int = 900,
) -> dict:
    """Dispatch all models concurrently; return the first clean response.

    Best on multi-GPU or CPU+GPU hardware where models run in true
    parallel. On a CPU-only host the two workers contend for the same
    cores and total wall time is *worse* than the fastest single model
    -- prefer :func:`cascade` there.
    """
    models = models or DEFAULT_MODELS
    fallback: dict | None = None

    with ThreadPoolExecutor(max_workers=len(models)) as pool:
        futures = {pool.submit(run_one, m, prompt, workspace=workspace, timeout=timeout): m for m in models}
        for fut in as_completed(futures):
            try:
                out = fut.result()
            except Exception as exc:  # noqa: BLE001
                fallback = fallback or {"model": futures[fut], "content": f"<error: {exc}>", "elapsed": 0, "metadata": {}}
                continue
            if looks_useful(out["content"]):
                for other, _ in futures.items():
                    if other is not fut:
                        other.cancel()
                return out
            fallback = fallback or out
        return fallback or {"model": "<none>", "content": "", "elapsed": 0, "metadata": {}}


def cascade(
    prompt: str,
    *,
    models: list[str] | None = None,
    workspace: str = ".",
    timeout: int = 900,
) -> dict:
    """Try models one by one; return the first whose output passes the gate.

    The right strategy on a single CPU: one model fully owns the cores,
    finishes as fast as that model can; a backup only runs if the
    primary trips the hallucination filter.
    """
    models = models or DEFAULT_MODELS
    last: dict | None = None
    for m in models:
        try:
            out = run_one(m, prompt, workspace=workspace, timeout=timeout)
        except Exception as exc:  # noqa: BLE001
            last = {"model": m, "content": f"<error: {exc}>", "elapsed": 0, "metadata": {}}
            continue
        last = out
        if looks_useful(out["content"]):
            return out
    return last or {"model": "<none>", "content": "", "elapsed": 0, "metadata": {}}


def main() -> int:
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <prompt>", file=sys.stderr)
        return 64

    prompt = " ".join(sys.argv[1:])
    workspace = os.environ.get("CLAW_WORKSPACE", ".")
    raw_models = os.environ.get("CLAW_ENSEMBLE_MODELS")
    models = [m.strip() for m in raw_models.split(",")] if raw_models else None
    timeout = int(os.environ.get("CLAW_ENSEMBLE_TIMEOUT", "900"))
    mode = os.environ.get("CLAW_ENSEMBLE_MODE", "cascade").lower()

    runner = race if mode == "race" else cascade
    t0 = time.monotonic()
    out = runner(prompt, models=models, workspace=workspace, timeout=timeout)
    total = time.monotonic() - t0

    print(f"=== winner: {out['model']} ({out['elapsed']:.1f}s; race wall {total:.1f}s) ===")
    print(out["content"])
    if usage := out["metadata"].get("usage"):
        print(f"\n[tokens in={usage.get('input_tokens')} out={usage.get('output_tokens')}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
