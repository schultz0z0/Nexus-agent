"""``hermes plan`` subcommand parser.

The plan command runs an agent loop in read-only mode to explore a task and
produce a structured plan, then enters a REPL where the user can refine and
(optionally) apply the plan. Inspired by Claude Code's `plan` permission
mode — the agent reasons about what to do without being able to make
changes, and the user keeps full control over side effects.

Plan files are stored in ``$HERMES_HOME/plans/<timestamp>-<slug>.md`` and
gitignored, so they live with the user's data (config + memories + skills)
and not in the project repository.
"""

from __future__ import annotations

from typing import Callable


def build_plan_parser(subparsers, *, cmd_plan: Callable) -> None:
    """Attach the ``plan`` subcommand to ``subparsers``."""
    plan_parser = subparsers.add_parser(
        "plan",
        help="Run Nexus Agent in read-only plan mode and produce an executable plan",
        description=(
            "Spawns an agent loop with write tools disabled. The agent explores "
            "the repo, reads files, runs read-only shell commands, and writes a "
            "structured plan describing the proposed change. You then refine the "
            "plan in a REPL and (optionally) apply it step by step with per-"
            "command approval."
        ),
    )
    plan_parser.add_argument(
        "goal",
        nargs="?",
        default=None,
        help="What you want to plan (e.g. 'add a /deploy skill to the desktop app'). "
        "If omitted, you'll be prompted.",
    )
    plan_parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="After the plan is written, enter the REPL with apply-mode enabled "
        "(otherwise 'apply' is rejected).",
    )
    plan_parser.add_argument(
        "--no-repl",
        action="store_true",
        default=False,
        help="Don't enter the REPL; just write the plan and exit. Useful for "
        "automation / CI.",
    )
    plan_parser.add_argument(
        "--cwd",
        default=None,
        metavar="PATH",
        help="Working directory for the plan (defaults to current dir).",
    )
    plan_parser.add_argument(
        "--model",
        default=None,
        help="Override the model for planning (defaults to your chat model).",
    )
    plan_parser.set_defaults(func=cmd_plan)
