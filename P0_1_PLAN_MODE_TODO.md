# P0.1 — `hermes plan` — TODO: wire the chat_loop driver

**Status:** P0.1 (read-only policy + REPL + parser + 22 tests) is **merged** in
`1de6f3810`. The chat_loop driver is **not** wired. This file describes what
remains.

## What's in place today

- `hermes_cli/plan_mode.py` — read-only tool allowlist (`PLAN_ALLOWED_TOOLS`),
  read-only shell-command policy (`is_shell_command_allowed`), `Plan` schema
  and `render()` / `parse_plan()` round-trip, `repl()` with `show` / `refine` /
  `apply` / `save` / `discard` / `path` / `help` / `exit`.
- `hermes_cli/subcommands/plan.py` — argparse glue (`hermes plan [--apply]
  [--no-repl] [--cwd PATH] [--model MODEL] [goal]`).
- `hermes_cli/main.py` — the `plan` subcommand is registered.
- `tests/test_plan_mode.py` — 22 unittest tests covering the tool policy,
  shell policy, plan render/parse round-trip, and every REPL command. All
  pass in 80ms without an LLM.
- `hermes plan --help` works.
- `hermes plan "x"` (no chat_loop) prints
  `No chat_loop was provided — this build of plan mode cannot drive the
  agent loop yet. Pass chat_loop=... in production.` and exits 1.

## What still needs to be built

### Step 1 — implement a `chat_loop` driver

A new function that drives the agent loop once and returns the final
assistant message plus the tool-call trace. Signature:

```python
def chat_loop(
    messages: list[dict],
    *,
    tools: list[str],
    model: str | None,
    cwd: Path,
    log: Callable[[str], None] | None = None,
    on_tool_call: Callable[[str, dict], dict] | None = None,
) -> dict:
    """Run the agent loop with the given messages, return final state.

    Returns
    -------
    {
        "final_text": str,                # last assistant message
        "tool_calls": list[dict],         # every tool call made
        "messages": list[dict],           # full conversation after the run
        "stopped_reason": str,            # 'plan_save', 'max_turns', 'error'
    }
    """
```

The driver must:

1. Load `config.yaml` and pick the provider/model from `model` (or the
   user's default).
2. Build a session scoped to `cwd` (or a temp session, since plan mode
   shouldn't pollute the user's history — but that's a design call).
3. Resolve the named tool list (`tools`) to the agent's internal tool
   registry.
4. Override the `run_shell_command` tool to apply the read-only
   `is_shell_command_allowed` policy before forwarding to the real
   tool implementation.
5. Reject (return a tool error rather than raise) any call to a tool
   that isn't in `PLAN_ALLOWED_TOOLS`.
6. Loop until either: the agent emits a `plan_save` tool call, the
   conversation exceeds a sane turn limit (e.g. 20 turns), or an error
   occurs.
7. Return the final state.

### Step 2 — wire `chat_loop` into `run_plan`

`run_plan` in `hermes_cli/plan_mode.py` already takes `chat_loop` as a
parameter. The job is to:

1. Resolve the user's config (model, provider, cwd) before calling
   `chat_loop`.
2. Make sure the planner is told to call `plan_save` exactly once, and
   the prompt enforces that.
3. Save the final text to a plan file via the existing
   `new_plan_path` + `parse_plan` flow.
4. Enter the REPL (already implemented).

The wiring point is `hermes_cli/main.py`'s `cmd_plan` (currently a
thin wrapper around `plan_mode.cmd_plan`). Replace the import with a
factory that injects the real driver.

### Step 3 — gate `plan_save` and `plan_finalize` on the planner

The current `PLAN_ALLOWED_TOOLS` lists `plan_save` and `plan_finalize`
as allowed. The real driver needs to recognise those as synthetic
tools (not in the existing registry) and either:

  * (preferred) implement them as in-process handlers that write to
    `new_plan_path(goal)` / append a finalize footer, and return
    success without round-tripping to the model; OR
  * define them as real tool wrappers in `hermes_cli/plan_tools.py`
    and register them on the fly when entering plan mode.

### Step 4 — test end-to-end

`tests/test_plan_mode.py` already covers everything that doesn't need
an LLM. Add an end-to-end test that:

1. Mocks a one-shot LLM (return a canned plan when prompted with the
   plan schema).
2. Calls `run_plan(...)` with a stub `chat_loop` that uses the mock.
3. Asserts the plan file was written, the REPL was entered, and the
   plan parses round-trip.

This is straightforward because `run_plan` already takes `chat_loop`
as a parameter — the test is just an instance of the production
injection pattern.

## Why we did NOT wire this in P0.1

1. **Scope creep risk.** The agent loop is the single most
   load-bearing piece of Hermes. Patching it without an integration
   test environment (i.e. a configured provider with quota) is a
   recipe for a broken `main`. The REPL and tool policy are the
   user-visible surface; they're worth reviewing in isolation before
   we touch the driver.
2. **The real driver is not a clean function.** `_call()` is module-
   private, `_init_agent()` is heavily context-dependent (session
   state, profile, tools, MCP, cron, gateway hooks), and
   `cmd_chat` is a long-lived REPL — not a "run once, return
   result". A proper integration is a refactor of the agent
   harness, not a wrapper. That belongs in a focused follow-up.
3. **No provider in the test environment.** End-to-end testing of
   the planner requires an actual LLM to evaluate the prompt; the
   venv in this checkout has no provider configured. The integration
   would be un-testable from the current host.

## How to pick this back up

Read `hermes_cli/cmd_chat` in `hermes_cli/main.py` (line ~2206) to
understand the agent harness, then write a `chat_loop` driver in
`hermes_cli/agent_driver.py` that exposes a clean function signature
(above) and reuse it from both `cmd_chat` and `cmd_plan`. The
`plan_mode.run_plan` already takes `chat_loop` as a parameter — the
wiring is purely additive.

When this is done, the `No chat_loop was provided` error in
`hermes plan "x"` goes away and the user gets a real plan.
