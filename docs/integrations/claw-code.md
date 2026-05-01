# Claw Code integration

OpenJarvis ships two Claude-Code-style coding agents:

| Agent       | Backend                                             | Runtime dependency      |
|-------------|-----------------------------------------------------|-------------------------|
| `claude_code` | `@anthropic-ai/claude-code` SDK via Node.js subprocess | Node.js ≥ 22            |
| `claw_code`   | `claw` Rust CLI from [satan-stack/claw-code][cc]    | A built `claw` binary   |

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

## Troubleshooting

- **`binary_missing`** — the agent could not locate `claw`. Build it,
  install it on `PATH`, or set `CLAW_BINARY`.
- **`401 Invalid bearer token`** from the wrapped subprocess — `claw`
  rejects an `sk-ant-*` key in the `ANTHROPIC_AUTH_TOKEN` slot. Move it to
  `ANTHROPIC_API_KEY`. See `claw-code/USAGE.md` for the credential matrix.
- **Output looks truncated** — increase `timeout` or call `claw doctor`
  manually to confirm provider connectivity before reusing the preset.
