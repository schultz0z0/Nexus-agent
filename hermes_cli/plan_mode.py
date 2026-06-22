"""Core logic for ``hermes plan``.

Three responsibilities:

1. **Read-only tool gate** — given the agent's tool registry, produce a
   filtered view that blocks any tool with side effects (write, edit,
   delete, network POST, code execution that touches the filesystem, etc.)
   while passing through read tools (read_file, search_files, terminal
   commands on the read-only allowlist, vision, web search/fetch).

2. **Plan agent loop** — drive a regular chat loop with the gated tool
   set, asking the agent to produce a plan in a specific markdown schema
   (Goal, Context, Proposed changes, Files to modify, Commands to run,
   Risks, Acceptance criteria). The agent's final assistant message is
   parsed and saved to ``$HERMES_HOME/plans/<id>.md``.

3. **Plan REPL** — once the plan is on disk, enter a small read-eval-print
   loop where the user can ``show``, ``refine <note>`` (re-invokes the
   agent in read-only mode to edit the plan in place), ``apply``
   (executes the ``Commands to run`` section step by step with per-command
   approval), ``save``, ``discard``, or ``exit``.

The read-only allowlist is conservative — when in doubt, block. Network
reads (web search, web fetch GET) are allowed because they are observable
side effects the user has already authorised through provider config;
mutating HTTP (POST/PUT/PATCH/DELETE) is blocked.

This module is intentionally framework-agnostic: it does not import the
agent loop driver directly. The handler in ``hermes_cli/main.py``
wires it up to the real agent loop. That makes the module easy to test
in isolation and keeps the read-only policy auditable in one place.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

# ---------------------------------------------------------------------------
# Read-only tool policy
# ---------------------------------------------------------------------------

# Tools that the plan-mode agent can call. Anything not in this list is
# blocked. We enumerate by tool name, not by side-effect class, because
# the agent's tool registry is name-based and the policy is part of the
# user-facing guarantee.
#
# Conservative additions to the Hermes Agent's existing tool set are
# allowed; anything that touches the filesystem, runs arbitrary code,
# mutates the network, or modifies process state is rejected.
PLAN_ALLOWED_TOOLS = frozenset({
    # File reading
    "read_file",
    "search_files",
    "file_search",
    "list_files",
    "glob",
    # Read-only shell helpers
    "terminal",
    # Vision / images (read-only)
    "vision",
    "image_gen_readonly",  # future-safe
    # Web reads
    "web_search",
    "web",
    "web_fetch",
    # Code reading — running a script is not allowed, but viewing its
    # output via the read_file path is. We expose a synthetic "show_file"
    # alias that maps to read_file in the dispatch layer.
    "show_file",
    # Skill loading (read-only metadata; the skill itself is invoked by
    # the agent loop and we gate that separately)
    "skills_list",
    "skill_read",
    # Session introspection (read-only)
    "session_search",
    "memory_read",
    # Plan-specific helpers
    "plan_save",
    "plan_finalize",
})


# Shell commands that the ``terminal`` tool is allowed to run in plan
# mode. Anything not matched here is rejected. This is a conservative
# allowlist: file mutation (``rm``, ``mv``, ``cp`` to non-/tmp, ``tee``),
# network mutation (``curl -X POST``, ``npm install``, ``git push``,
# ``git commit``), and any ``sudo``/system-modifying call are blocked.
PLAN_ALLOWED_SHELL_PATTERNS = (
    # Filesystem reads
    r"^\s*(ls|stat|file|readlink|wc|head|tail|cat|less|more)\b",
    r"^\s*find\b(?!.*-delete|.*-exec)",
    r"^\s*tree\b",
    r"^\s*du\b(?!.*--delete)",
    r"^\s*df\b",
    # Search
    r"^\s*grep\b",
    r"^\s*rg\b(?!.*--delete)",
    r"^\s*ag\b",
    r"^\s*ack\b",
    r"^\s*fd\b",
    # Git reads (no writes, no network)
    r"^\s*git\s+(status|log|diff|show|branch|tag|remote|rev-parse|ls-files|ls-tree|blame|shortlog|describe|config --get)\b",
    # Network reads (no POST/PUT/PATCH/DELETE; no installs)
    r"^\s*curl\s+[^|;&]*\s",
    r"^\s*wget\s+[^|;&]*\s",
    r"^\s*http(?:s)?\b",
    # Process reads
    r"^\s*ps\b",
    r"^\s*lsof\b",
    r"^\s*netstat\b",
    r"^\s*ss\b",
    r"^\s*uname\b",
    r"^\s*whoami\b",
    r"^\s*pwd\b",
    r"^\s*date\b",
    r"^\s*env\b",
    r"^\s*printenv\b",
    r"^\s*which\b",
    r"^\s*type\b",
)

PLAN_BLOCKED_SHELL_PATTERNS = (
    r"\brm\b",
    r"\bmv\b",
    r"\bcp\b",
    r"\btee\b",
    r"\bsudo\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bmkdir\b(?!\s+-p\s+/(?:tmp|var/tmp))",
    r"\btouch\b",
    r"\b(>|>>|>>?)\s*\S",  # shell redirection
    r"\bgit\s+(commit|push|pull|fetch|merge|rebase|reset|clean|cherry-pick|revert|stash|am|apply)\b",
    r"\bnpm\s+(install|i|add|remove|rm|uninstall|update|publish|login)\b",
    r"\byarn\s+(add|install|remove|upgrade)\b",
    r"\bpnpm\s+(add|install|remove|update)\b",
    r"\bpip\s+install\b",
    r"\buv\s+(add|pip\s+install|sync)\b",
    r"\bcurl\s+-X\s+(POST|PUT|PATCH|DELETE)\b",
    r"\bwget\s+--post\b",
    r"\bdd\b",
    r"\bmkfs\.",
    r"\brm\s+-rf\s+/(?:\s|$)",  # belt-and-suspenders for `rm -rf /`
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bkill\s+-9\s+/",
)


def is_tool_allowed(tool_name: str) -> bool:
    """Return True if the named tool is allowed in plan mode."""
    return tool_name in PLAN_ALLOWED_TOOLS


def is_shell_command_allowed(command: str) -> tuple[bool, str]:
    """Decide whether a shell command may run in plan mode.

    Returns ``(True, "")`` when allowed, or ``(False, reason)`` with a
    human-readable reason when blocked. The check is fail-closed: any
    command that does not match an allow pattern is rejected.
    """
    if not command or not command.strip():
        return False, "empty command"

    # Block patterns are evaluated first — they always win.
    for pattern in PLAN_BLOCKED_SHELL_PATTERNS:
        if re.search(pattern, command):
            return False, f"matches blocked pattern: {pattern}"

    # Allow patterns are checked next.
    for pattern in PLAN_ALLOWED_SHELL_PATTERNS:
        if re.search(pattern, command):
            return True, ""

    return False, "command not in read-only allowlist"


# ---------------------------------------------------------------------------
# Plan schema and parsing
# ---------------------------------------------------------------------------

PLAN_TEMPLATE = """\
# Plan: {title}

> Generated by `hermes plan` on {timestamp}.
> Plan ID: `{plan_id}`

## Goal

{goal}

## Context

{context}

## Proposed changes

{proposed_changes}

## Files to modify

{files_to_modify}

## Commands to run

```bash
{commands}
```

## Risks

{risks}

## Acceptance criteria

{acceptance_criteria}
"""


@dataclass
class Plan:
    """In-memory representation of a plan file."""

    plan_id: str
    title: str
    goal: str
    context: str = ""
    proposed_changes: str = ""
    files_to_modify: str = ""
    commands: str = ""
    risks: str = ""
    acceptance_criteria: str = ""
    path: Optional[Path] = None
    created_at: float = field(default_factory=time.time)

    def render(self) -> str:
        """Render the plan to markdown matching the canonical template."""
        return PLAN_TEMPLATE.format(
            title=self.title,
            timestamp=time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(self.created_at)),
            plan_id=self.plan_id,
            goal=self.goal or "(not specified)",
            context=self.context or "(to be filled by the planner)",
            proposed_changes=self.proposed_changes or "(to be filled by the planner)",
            files_to_modify=self.files_to_modify or "(to be filled by the planner)",
            commands=self.commands or "# (no shell commands needed)",
            risks=self.risks or "(to be filled by the planner)",
            acceptance_criteria=self.acceptance_criteria or "(to be filled by the planner)",
        )

    def save(self) -> Path:
        """Write the plan to disk and return the path."""
        if self.path is None:
            raise RuntimeError("Plan has no path; set Plan.path before calling save()")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.render(), encoding="utf-8")
        return self.path


# Parse a markdown plan back into a Plan. The parser is intentionally
# lenient — sections may be missing (filled with placeholders) and the
# commands block is captured as a single string so we can run it
# command-by-command later.
SECTION_HEADERS = {
    "context": "## Context",
    "proposed_changes": "## Proposed changes",
    "files_to_modify": "## Files to modify",
    "commands": "## Commands to run",
    "risks": "## Risks",
    "acceptance_criteria": "## Acceptance criteria",
}


def parse_plan(path: Path) -> Plan:
    """Parse a plan markdown file into a Plan object."""
    text = path.read_text(encoding="utf-8")
    # Plan ID is the first ``> Plan ID: `` line in the header.
    plan_id_match = re.search(r"Plan ID:\s*`([^`]+)`", text)
    plan_id = plan_id_match.group(1) if plan_id_match else path.stem
    # Title is the first H1.
    title_match = re.search(r"^#\s+Plan:\s*(.+)$", text, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else path.stem
    # Goal is the first paragraph after the "## Goal" header.
    goal_match = re.search(
        r"## Goal\s*\n\s*\n?(.+?)(?:\n\s*\n|\n## )", text, re.DOTALL
    )
    goal = goal_match.group(1).strip() if goal_match else ""

    plan = Plan(
        plan_id=plan_id,
        title=title,
        goal=goal,
        path=path,
        created_at=path.stat().st_mtime,
    )
    # Parse remaining sections by walking the document.
    for field_name, header in SECTION_HEADERS.items():
        pattern = re.escape(header) + r"\s*\n(.*?)(?=\n## |\Z)"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            value = match.group(1).strip()
            # The "Commands to run" section is fenced as a bash code block;
            # strip the ```bash / ``` fence so the round-trip preserves only
            # the user-authored shell lines.
            if field_name == "commands":
                value = re.sub(
                    r"^```(?:bash|sh|shell)?\s*\n(.*?)\n```\s*$",
                    r"\1",
                    value,
                    flags=re.DOTALL,
                )
            setattr(plan, field_name, value)
    return plan


# ---------------------------------------------------------------------------
# Plan location helpers
# ---------------------------------------------------------------------------


def plans_dir(home: Optional[Path] = None) -> Path:
    """Return the directory where plan files are stored.

    Honours ``$HERMES_HOME`` (defaults to ``~/.hermes``) so plans live
    in the same place as sessions, memories, and skills. The directory
    is created on first write.
    """
    if home is None:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    return home / "plans"


def slugify(text: str, max_len: int = 40) -> str:
    """Turn an arbitrary goal into a filesystem-safe slug."""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", text.lower()).strip("-")
    return (slug or "plan")[:max_len]


# Per-process counter to disambiguate plans created within the same wall-
# clock second (Windows time.time() resolution is ~15ms, which is fine for
# humans but flaky for tests and rapid automation).
_new_plan_counter = 0


def new_plan_path(goal: str, home: Optional[Path] = None) -> Path:
    """Create a unique plan path for a new plan."""
    global _new_plan_counter
    _new_plan_counter += 1
    ts = time.strftime("%Y%m%d-%H%M%S")
    suffix = f"{_new_plan_counter:03d}"
    return plans_dir(home) / f"{ts}-{suffix}-{slugify(goal)}.md"


# ---------------------------------------------------------------------------
# Plan REPL
# ---------------------------------------------------------------------------

HELP_TEXT = """\
Plan REPL commands:
  show                  Show the current plan
  refine <note>         Ask the planner agent to revise the plan with <note>
  apply                 Execute the plan's "Commands to run" (per-command approval)
  save                  Re-save the plan to disk (after manual edits)
  discard               Delete the plan file and exit
  path                  Print the plan file path
  help                  Show this help
  exit / quit / q        Exit without applying
"""


def _print_plan(plan: Plan) -> None:
    """Pretty-print the plan to the terminal."""
    path = plan.path if plan.path else "(unsaved)"
    print()
    print(f"─── Plan: {plan.title} ─────────────────────────────")
    print(f"    id:      {plan.plan_id}")
    print(f"    file:    {path}")
    print()
    print(plan.render())
    print("─────────────────────────────────────────────────────")


def _shell_command_apply(
    command: str,
    *,
    cwd: Path,
    ask: Callable[[str], bool],
    log: Callable[[str], None],
) -> bool:
    """Execute one shell command with per-command approval. Returns True if it ran."""
    allowed, reason = is_shell_command_allowed(command)
    if not allowed:
        log(f"  ✗ blocked: {command}")
        log(f"    reason: {reason}")
        return False
    print(f"\n$ {command}")
    if not ask(f"Run this command? [y/N/e=edit] "):
        print("  → skipped")
        return False
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=False,
            text=True,
        )
        return result.returncode == 0
    except KeyboardInterrupt:
        print("  → interrupted")
        return False


def _apply_plan(
    plan: Plan,
    *,
    cwd: Path,
    ask: Callable[[str], bool],
    log: Callable[[str], None],
) -> None:
    """Run the plan's commands section step by step."""
    commands = [
        line.strip()
        for line in plan.commands.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not commands:
        log("No commands in the plan. Add some in the 'Commands to run' section first.")
        return
    log(f"Applying {len(commands)} command(s) from {plan.path}:")
    failures = 0
    for i, cmd in enumerate(commands, 1):
        log(f"\n[{i}/{len(commands)}]")
        if not _shell_command_apply(cmd, cwd=cwd, ask=ask, log=log):
            failures += 1
            if not ask(f"Command failed/skipped. Continue with the rest? [y/N] "):
                log("Apply aborted by user.")
                return
    log(f"\nDone. {len(commands) - failures}/{len(commands)} command(s) ran successfully.")


def repl(
    plan: Plan,
    *,
    cwd: Path,
    apply_enabled: bool = True,
    ask: Optional[Callable[[str], bool]] = None,
    log: Optional[Callable[[str], None]] = None,
    refine_callback: Optional[Callable[[Plan, str], Plan]] = None,
    input_fn: Callable[[str], str] = input,
) -> str:
    """Run the plan REPL.

    Returns the user's last meaningful action ("exit", "discard", "apply",
    "save") so the caller can branch on it. ``ask`` and ``refine_callback``
    are injected so the REPL can be driven by the real agent loop in
    production and by deterministic stubs in tests.
    """
    ask = ask or (lambda prompt: input_fn(prompt).strip().lower() in {"y", "yes"})
    log = log or (lambda line: print(line))

    if plan.path is None or not plan.path.exists():
        plan.path.parent.mkdir(parents=True, exist_ok=True)
        plan.save()
        log(f"Saved new plan to {plan.path}")

    _print_plan(plan)
    log(HELP_TEXT)

    while True:
        try:
            raw = input_fn("plan> ").strip()
        except (EOFError, KeyboardInterrupt):
            log("\nBye.")
            return "exit"
        if not raw:
            continue
        cmd, _, rest = raw.partition(" ")
        cmd = cmd.lower()
        rest = rest.strip()
        if cmd in {"exit", "quit", "q"}:
            return "exit"
        if cmd == "help":
            log(HELP_TEXT)
            continue
        if cmd == "show":
            _print_plan(plan)
            continue
        if cmd == "path":
            log(str(plan.path))
            continue
        if cmd == "save":
            plan.save()
            log(f"Saved {plan.path}")
            continue
        if cmd == "discard":
            if plan.path and plan.path.exists():
                plan.path.unlink()
                log(f"Deleted {plan.path}")
            return "discard"
        if cmd == "refine":
            if not rest:
                log("Usage: refine <note>")
                continue
            if refine_callback is None:
                log("Refine is not available in this environment.")
                continue
            log(f"Refining plan with note: {rest!r}")
            refined = refine_callback(plan, rest)
            # Carry the path forward so ``save`` lands on the same file.
            refined.path = plan.path
            refined.created_at = plan.created_at
            plan = refined
            plan.save()
            _print_plan(plan)
            continue
        if cmd == "apply":
            if not apply_enabled:
                log("Apply is disabled in this run. Re-run with --apply to enable.")
                continue
            _apply_plan(plan, cwd=cwd, ask=ask, log=log)
            continue
        log(f"Unknown command: {cmd!r}. Type 'help' for the list.")


# ---------------------------------------------------------------------------
# Plan agent prompt
# ---------------------------------------------------------------------------

PLAN_AGENT_SYSTEM_PROMPT = """\
You are Nexus Agent running in **plan mode**. You are helping the user
design a change before any code is written.

# Read-only contract

You may ONLY use the following tools. Any attempt to call a tool not on
this list will be rejected by the runtime:

- read_file, search_files, file_search, list_files, glob
- terminal (with a strict read-only allowlist — no write, no install,
  no shell redirection, no network mutation)
- vision
- web_search, web, web_fetch (HTTP GET only — no POST/PUT/PATCH/DELETE)
- skills_list, skill_read
- session_search, memory_read
- plan_save, plan_finalize

# Workflow

1. Read the user's goal. If it is ambiguous, ask one clarifying question.
2. Explore the repository / codebase / docs to gather context. Use
   `search_files` and `read_file` aggressively. Run `git log`, `git diff`,
   `ls`, `find`, `grep` freely. Read the relevant files end-to-end.
3. When you have enough context, write a single plan document in the
   schema below and call `plan_save` to persist it. Do NOT make any
   other tool calls after `plan_save` — the plan is your final output.

# Plan schema (markdown)

```markdown
# Plan: <short title>

## Goal
<one-paragraph restatement of the user's goal>

## Context
<what you found while exploring — the current state of the relevant
files, any prior art, the user's existing patterns>

## Proposed changes
<numbered list of changes, in execution order. Each item should be
small, testable, and reversible>

## Files to modify
<bulleted list of file paths, grouped by change>

## Commands to run
```bash
# write only the commands the user will need to run, in order.
# keep them small and verifiable.
```

## Risks
<bulleted list of failure modes, each with a mitigation>

## Acceptance criteria
<bulleted list of checks the user can run to confirm the plan worked>
```

# Style rules

- Be specific. File paths, function names, exact commands.
- Prefer the smallest change that solves the problem. Do not refactor
  unrelated code.
- If the goal is dangerous (deletes data, sends messages, modifies
  production), say so explicitly in Risks and Acceptance criteria.
- If the goal is unclear after one round of clarification, save a
  plan that says "goal is ambiguous, please clarify" in the Goal
  section rather than guessing.

Begin.
"""


def build_plan_agent_prompt(goal: str, cwd: Path) -> str:
    """Construct the user message that kicks off the plan agent loop."""
    return (
        f"# Goal\n\n{goal}\n\n"
        f"# Working directory\n\n`{cwd}`\n\n"
        "# Instructions\n\n"
        "Explore the repository, then write a plan in the schema above "
        "and call `plan_save` exactly once. Do not modify any files."
    )


# ---------------------------------------------------------------------------
# Plan loop glue (called from main.py)
# ---------------------------------------------------------------------------


def run_plan(
    goal: str,
    *,
    cwd: Path,
    apply_enabled: bool = True,
    no_repl: bool = False,
    model: Optional[str] = None,
    chat_loop: Optional[Callable[..., dict]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> int:
    """Top-level entrypoint used by the ``hermes plan`` subcommand.

    ``chat_loop`` is the agent-loop driver; it must accept
    ``messages``, ``tools``, ``model`` and return the final assistant
    message plus the tool-call trace. We pass in the gated tool set
    (read-only) and the plan-mode system prompt.
    """
    log = log or (lambda line: print(line))
    log(f"Plan mode: {goal!r} in {cwd}")

    if chat_loop is None:
        log(
            "No chat_loop was provided — this build of plan mode cannot "
            "drive the agent loop yet. Pass chat_loop=... in production."
        )
        return 1

    plan_path = new_plan_path(goal)
    plan_path.parent.mkdir(parents=True, exist_ok=True)

    gated_tools = list(PLAN_ALLOWED_TOOLS) + [
        "plan_save",  # always present in plan mode
    ]
    result = chat_loop(
        messages=[
            {"role": "system", "content": PLAN_AGENT_SYSTEM_PROMPT},
            {"role": "user", "content": build_plan_agent_prompt(goal, cwd)},
        ],
        tools=gated_tools,
        model=model,
    )
    final_text = result.get("final_text", "")
    if not final_text:
        log("Planner did not produce a plan. Aborting.")
        return 1
    plan_path.write_text(final_text, encoding="utf-8")
    log(f"Plan saved to {plan_path}")
    plan = parse_plan(plan_path)

    if no_repl:
        log("REPL disabled (--no-repl). Plan is on disk; exiting.")
        return 0

    repl(
        plan,
        cwd=cwd,
        apply_enabled=apply_enabled,
        log=log,
    )
    return 0


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def cmd_plan(args: argparse.Namespace) -> int:
    """Subcommand entry point for ``hermes plan``.

    The actual agent-loop driver is injected by ``main.py`` (which
    imports the production chat loop and threads it through
    ``run_plan``). When invoked from tests we can pass a stub
    ``chat_loop`` to keep the test hermetic.
    """
    goal = args.goal
    if not goal:
        try:
            goal = input("plan> What do you want to plan? ").strip()
        except (EOFError, KeyboardInterrupt):
            print("No goal provided. Aborting.")
            return 1
    if not goal:
        print("No goal provided. Aborting.")
        return 1
    cwd = Path(args.cwd).resolve() if args.cwd else Path.cwd()
    return run_plan(
        goal,
        cwd=cwd,
        apply_enabled=args.apply,
        no_repl=args.no_repl,
        model=args.model,
    )


__all__ = [
    "PLAN_ALLOWED_TOOLS",
    "PLAN_ALLOWED_SHELL_PATTERNS",
    "PLAN_BLOCKED_SHELL_PATTERNS",
    "is_tool_allowed",
    "is_shell_command_allowed",
    "Plan",
    "parse_plan",
    "plans_dir",
    "new_plan_path",
    "repl",
    "run_plan",
    "cmd_plan",
    "PLAN_AGENT_SYSTEM_PROMPT",
    "build_plan_agent_prompt",
]
