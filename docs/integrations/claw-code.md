# Claw Code integration

OpenJarvis ships three Claude-Code-style coding agents:

| Agent         | Backend                                                  | Runtime dependency      |
|---------------|----------------------------------------------------------|-------------------------|
| `claude_code` | `@anthropic-ai/claude-code` SDK via Node.js subprocess   | Node.js ≥ 22            |
| `claw_code`   | `claw` Rust CLI from [satan-stack/claw-code][cc]         | A built `claw` binary   |
| `claw_smart`  | Token-saving router: qwen2.5 first, Anthropic on failure | Same as `claw_code`     |

[cc]: https://github.com/satan-stack/claw-code

Both agents implement the same `BaseAgent` surface, so any preset, recipe,
or orchestrator that accepts one accepts the other. Pick `claw_code` when:

- You don't want a Node.js dependency on the host.
- You want to point Jarvis at non-Anthropic providers that `claw` already
  supports out of the box (xAI, OpenAI-compatible, OpenRouter, Ollama,
  DashScope/Qwen, any Anthropic-compatible local server).
- You're already running `claw` and want OpenJarvis to schedule and trace
  it via the standard agent registry.

## 1. Build the `claw` binary

```bash
git clone https://github.com/satan-stack/claw-code
cd claw-code/rust
cargo build --workspace
```

The debug binary lands at `rust/target/debug/claw`. For a production-grade
build use `cargo build --workspace --release` and pick up `target/release/claw`.

## 2. Tell OpenJarvis where the binary lives

The agent resolves the binary in this order:

1. The `binary` config key (or `CLAW_BINARY` environment variable) if it
   points at an existing file.
2. Whatever `claw` resolves to on `PATH` (use this if you ran
   `cargo install --path .`).
3. `<workspace>/rust/target/debug/claw` for users running straight out of
   a cloned `claw-code` checkout.

The simplest setup:

```bash
export CLAW_BINARY=$HOME/code/claw-code/rust/target/release/claw
```

## 3. Provide model credentials

`claw` drives inference itself, so OpenJarvis just forwards the parent
environment. Export whichever credential matches the model you want to
use:

```bash
# Anthropic
export ANTHROPIC_API_KEY=sk-ant-...

# xAI (Grok)
export XAI_API_KEY=xai-...

# OpenAI / OpenRouter / Ollama via the OpenAI-compatible backend
export OPENAI_API_KEY=sk-...
export OPENAI_BASE_URL=https://openrouter.ai/api/v1   # optional
```

See [`USAGE.md` in claw-code][cc-usage] for the full provider matrix.

[cc-usage]: https://github.com/satan-stack/claw-code/blob/main/USAGE.md#supported-providers--models

## 4. Use the preset

```bash
jarvis init --preset claw-code
jarvis ask --agent claw_code "summarize this repository"
```

Or drop the relevant block straight into your `~/.openjarvis/config.toml`:

```toml
[agent]
default_agent = "claw_code"

[agent.claw_code]
permission_mode = "workspace-write"   # read-only | workspace-write | danger-full-access
allowed_tools = ["read", "glob", "edit"]
timeout = 300

[intelligence]
default_model = "sonnet"              # any alias / model claw understands
```

## 5. Programmatic use

```python
from openjarvis.agents.claw_code import ClawCodeAgent
from openjarvis.engine._stubs import InferenceEngine  # placeholder; unused by claw_code

agent = ClawCodeAgent(
    engine=InferenceEngine(),       # required by BaseAgent, ignored by claw_code
    model="sonnet",
    workspace="/path/to/repo",
    permission_mode="workspace-write",
    allowed_tools=["read", "glob", "edit"],
    timeout=300,
)

result = agent.run("Refactor src/auth.rs to use async/await")
print(result.content)
print(result.metadata)
```

## Configuration reference

| Key               | Default              | Notes                                                              |
|-------------------|----------------------|--------------------------------------------------------------------|
| `binary`          | `claw`               | Absolute path or `PATH`-resolvable name; `CLAW_BINARY` env wins.   |
| `workspace`       | `os.getcwd()`        | `claw` runs with this as cwd; sessions land under `.claw/`.        |
| `permission_mode` | `workspace-write`    | `read-only` / `workspace-write` / `danger-full-access`.            |
| `allowed_tools`   | `None`               | Forwarded as `--allowedTools a,b,c`.                               |
| `resume`          | `""`                 | Pass `"latest"` (or a session id) to continue a prior `claw` run.  |
| `timeout`         | `300`                | Wall-clock seconds before the subprocess is killed.                |
| `extra_args`      | `[]`                 | Extra flags appended before `prompt <input>` for power users.      |

## Picking a model on CPU-only hardware

claw's system prompt is large (~3K tokens), so most of each turn on a
CPU box is spent reading the prompt. The right model is the smallest
one that still follows claw's tool-calling protocol cleanly. Verified
trade-offs on a modern x86 CPU with no GPU:

| Model                       | Cold turn | Warm turn | Notes                                                |
|-----------------------------|-----------|-----------|------------------------------------------------------|
| `openai/qwen2.5-coder:1.5b` | ~120 s    | **~10 s** | Best CPU pick; code-specialized; clean instructions. |
| `openai/llama3.2:3b`        | ~250 s    | ~30 s     | General-purpose backup; sometimes hallucinates tools.|
| `openai/qwen2.5-coder:7b`   | 8-12 min  | 2-4 min   | Smarter but too slow on CPU for interactive use.     |
| `openai/tinyllama:1.1b`     | ~60 s     | ~5 s      | Too weak to follow claw's tool protocol reliably.    |
| `sonnet` / `haiku`          | n/a       | 2-8 s     | Anthropic direct; `ANTHROPIC_API_KEY` required.      |

## `claw_smart`: cheap-first routing for haiku / sonnet

When you ask the agent for `haiku` or `sonnet`, you usually don't
*need* Anthropic — a small local code model handles the request 95% of
the time. `claw_smart` exploits that by routing through
`qwen2.5-coder:1.5b` first and only spending Anthropic tokens when the
local reply trips the same hallucination filter the ensemble uses.

| Asked model | Cascade chain                                       |
|-------------|-----------------------------------------------------|
| `haiku`     | `["openai/qwen2.5-coder:1.5b", "haiku"]`            |
| `sonnet`    | `["openai/qwen2.5-coder:1.5b", "sonnet"]`           |
| `opus`      | `["opus"]` — always Anthropic, no local equivalent. |
| anything    | `[<as-is>]` — pass-through.                         |

```toml
[agent]
default_agent = "claw_smart"

[intelligence]
default_model = "sonnet"        # routed to qwen, then Anthropic on failure
```

Both credentials live in the environment side-by-side; claw selects
the right one per cascade step from the model name prefix:

```bash
export OPENAI_BASE_URL="http://127.0.0.1:11434/v1"   # for qwen via Ollama
export OPENAI_API_KEY="ollama"
export ANTHROPIC_API_KEY="sk-ant-..."                # only spent if qwen fails
```

The winning result's metadata records the cascade trace under
``claw_smart_chain``, ``claw_smart_winner``, and ``claw_smart_attempts``,
so your traces show exactly which step answered each turn.

Override the routing in code:

```python
from openjarvis.agents.claw_smart import ClawSmartAgent

agent = ClawSmartAgent(
    engine=None, model="sonnet",
    model_routes={
        # Force a smarter local model for sonnet
        "sonnet": ["openai/qwen2.5-coder:7b", "sonnet"],
        # Pin opus to claude-haiku-4-5 instead, ignoring any local hop
        "opus": ["haiku"],
    },
)
```

## Multi-model ensemble (`scripts/claw_ensemble.py`)

For mission-critical prompts, [`scripts/claw_ensemble.py`](../../scripts/claw_ensemble.py)
runs two models with a quality gate that filters out hallucinated tool
calls and empty replies. Two modes:

- **`cascade`** (default; best on CPU) — try the primary first; only
  invoke the secondary if the primary's output trips the filter. One
  model fully owns the cores per turn, so warm-cache turns are as fast
  as the primary alone.
- **`race`** (best on GPU / multi-GPU) — dispatch every model
  concurrently and take the first reply that passes the gate.

```bash
export CLAW_BINARY=/path/to/claw
export OPENAI_BASE_URL="http://127.0.0.1:11434/v1"
export OPENAI_API_KEY="ollama"
export CLAW_ENSEMBLE_MODE=cascade        # or "race" if you have a GPU
export CLAW_ENSEMBLE_MODELS="openai/qwen2.5-coder:1.5b,openai/llama3.2:3b"

uv run python scripts/claw_ensemble.py "write a fibonacci function in python"
```

## Troubleshooting

- **`binary_missing`** — the agent could not locate `claw`. Build it,
  install it on `PATH`, or set `CLAW_BINARY`.
- **`401 Invalid bearer token`** from the wrapped subprocess — `claw`
  rejects an `sk-ant-*` key in the `ANTHROPIC_AUTH_TOKEN` slot. Move it to
  `ANTHROPIC_API_KEY`. See `claw-code/USAGE.md` for the credential matrix.
- **Output looks truncated** — increase `timeout` or call `claw doctor`
  manually to confirm provider connectivity before reusing the preset.
