"""Smoke tests for ``hermes_cli.plan_mode``.

The tests focus on the parts that don't need a real agent loop:
- the read-only tool policy (which tools are allowed, which shell
  commands are allowed/blocked)
- the plan parser and renderer (round-trip a Plan object through
  ``render`` + ``parse_plan`` and assert key sections survive)
- the REPL (drive a fake ``input_fn`` through ``show``, ``refine``,
  ``save``, ``apply``, ``discard``, ``exit`` and assert correct
  side-effects and return values)

What this test does NOT do:
- drive the real ``chat_loop`` driver. The agent loop is wired up
  in production by ``main.py``; we keep the test hermetic so the
  suite runs in <1s without a configured provider.

Run with:
    cd $HERMES_AGENT_REPO
    ./venv/Scripts/python.exe -m pytest tests/test_plan_mode.py -v
    # or
    ./venv/Scripts/python.exe tests/test_plan_mode.py
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

# Make the repo root importable when running this file directly.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from hermes_cli.plan_mode import (  # noqa: E402
    PLAN_ALLOWED_TOOLS,
    Plan,
    is_shell_command_allowed,
    is_tool_allowed,
    new_plan_path,
    parse_plan,
    plans_dir,
    repl,
    slugify,
)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Read-only tool policy
# ---------------------------------------------------------------------------


class TestToolPolicy(unittest.TestCase):
    def test_read_tools_are_allowed(self):
        for tool in ("read_file", "search_files", "web_fetch", "vision"):
            self.assertTrue(is_tool_allowed(tool), f"{tool} should be allowed")

    def test_write_tools_are_blocked(self):
        for tool in ("write_file", "edit_file", "delete_file", "rm"):
            self.assertFalse(is_tool_allowed(tool), f"{tool} should be blocked")

    def test_shell_read_commands_allowed(self):
        for cmd in (
            "ls -la",
            "cat README.md",
            "grep -r 'TODO' src/",
            "git status",
            "git log --oneline -10",
            "find . -name '*.py' -not -path './node_modules/*'",
            "pwd",
            "ps aux | grep hermes",
        ):
            allowed, reason = is_shell_command_allowed(cmd)
            self.assertTrue(allowed, f"expected allowed: {cmd!r} (got reason: {reason!r})")

    def test_shell_destructive_commands_blocked(self):
        for cmd in (
            "rm -rf /",
            "rm foo.txt",
            "mv a b",
            "echo x > file",
            "sudo apt install evil",
            "git push origin main",
            "git commit -m 'oops'",
            "npm install foo",
            "pip install evil",
            "curl -X POST https://example.com",
            "shutdown -h now",
        ):
            allowed, reason = is_shell_command_allowed(cmd)
            self.assertFalse(allowed, f"expected blocked: {cmd!r}")

    def test_unknown_shell_command_blocked_fail_closed(self):
        """Anything not explicitly allowed is rejected (fail-closed)."""
        allowed, reason = is_shell_command_allowed("some_random_tool --foo")
        self.assertFalse(allowed)
        self.assertIn("allowlist", reason)

    def test_allowed_tools_set_is_not_empty(self):
        self.assertGreater(len(PLAN_ALLOWED_TOOLS), 0)

    def test_write_tools_not_in_allowed_set(self):
        """Defence-in-depth: the allowlist must not contain any obvious write tool."""
        banned_in_allowlist = PLAN_ALLOWED_TOOLS & {
            "write_file", "edit_file", "delete_file", "rm",
            "bash", "shell", "exec", "code_execution",
        }
        self.assertFalse(
            banned_in_allowlist,
            f"forbidden tools in allowlist: {banned_in_allowlist}",
        )


# ---------------------------------------------------------------------------
# Slug + plan path
# ---------------------------------------------------------------------------


class TestSlugAndPath(unittest.TestCase):
    def test_slugify_basic(self):
        self.assertEqual(
            slugify("Add /deploy skill to desktop app"),
            "add-deploy-skill-to-desktop-app",
        )

    def test_slugify_strips_unsafe_chars(self):
        self.assertEqual(slugify("hello world! @#$%"), "hello-world")

    def test_slugify_fallback(self):
        self.assertEqual(slugify("///"), "plan")

    def test_new_plan_path_creates_unique(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            import hermes_cli.plan_mode as pm
            original = pm.plans_dir
            pm.plans_dir = lambda home=None: Path(td)
            try:
                p1 = new_plan_path("Add auth middleware")
                p2 = new_plan_path("Add auth middleware")
                self.assertNotEqual(p1, p2)
                self.assertEqual(p1.parent, Path(td))
            finally:
                pm.plans_dir = original


# ---------------------------------------------------------------------------
# Plan render + parse round-trip
# ---------------------------------------------------------------------------


def _sample_plan(**overrides) -> Plan:
    defaults = dict(
        plan_id="20260622-120000-add-auth",
        title="Add auth middleware",
        goal="Add JWT-based auth middleware to the API.",
        context="Currently all endpoints are public.",
        proposed_changes="1. Add auth middleware\n2. Wire it into the FastAPI app",
        files_to_modify="- `src/api/middleware.py`\n- `src/api/app.py`",
        commands='echo "hello"\nls -la',
        risks="Risk: breaking existing endpoints.",
        acceptance_criteria="- All endpoints require valid JWT\n- Tests pass",
    )
    defaults.update(overrides)
    return Plan(**defaults)


class TestPlanRender(unittest.TestCase):
    def test_plan_render_includes_all_sections(self):
        p = _sample_plan()
        text = p.render()
        for header in (
            "# Plan: Add auth middleware",
            "## Goal",
            "## Context",
            "## Proposed changes",
            "## Files to modify",
            "## Commands to run",
            "## Risks",
            "## Acceptance criteria",
        ):
            self.assertIn(header, text, f"missing section: {header}")

    def test_plan_round_trip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = _sample_plan()
            p.path = Path(td) / "round-trip.md"
            p.save()
            parsed = parse_plan(p.path)
            self.assertEqual(parsed.title, p.title)
            self.assertEqual(parsed.goal, p.goal)
            self.assertEqual(parsed.proposed_changes, p.proposed_changes)
            self.assertEqual(parsed.commands, p.commands)
            self.assertEqual(parsed.risks, p.risks)
            self.assertEqual(parsed.acceptance_criteria, p.acceptance_criteria)

    def test_plan_render_with_minimal_fields(self):
        """Sections left empty should render as placeholders, not crash."""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Plan(plan_id="x", title="Empty plan", goal="do a thing")
            p.path = Path(td) / "empty.md"
            p.save()
            text = p.path.read_text(encoding="utf-8")
            self.assertIn("# Plan: Empty plan", text)
            self.assertIn("## Context", text)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------


class TestRepl(unittest.TestCase):
    def _plan(self, tmp_path, name="repl.md", **kwargs):
        p = _sample_plan(**kwargs)
        p.path = tmp_path / name
        p.save()
        return p

    def test_repl_show_prints_plan(self):
        import tempfile, contextlib
        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()) as out:
            tmp = Path(td)
            p = self._plan(tmp)
            inputs = iter(["show", "exit"])
            repl(p, cwd=tmp, input_fn=lambda _: next(inputs))
            self.assertIn("# Plan: Add auth middleware", out.getvalue())
            self.assertIn("JWT", out.getvalue())

    def test_repl_path_prints_file(self):
        import tempfile, contextlib
        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()) as out:
            tmp = Path(td)
            p = self._plan(tmp, "path.md")
            inputs = iter(["path", "exit"])
            repl(p, cwd=tmp, input_fn=lambda _: next(inputs))
            self.assertIn(str(p.path), out.getvalue())

    def test_repl_apply_disabled_by_default(self):
        import tempfile, contextlib
        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()) as out:
            tmp = Path(td)
            p = self._plan(tmp, "no-apply.md")
            inputs = iter(["apply", "exit"])
            repl(p, cwd=tmp, apply_enabled=False, input_fn=lambda _: next(inputs))
            self.assertIn("disabled", out.getvalue().lower())

    def test_repl_discard_deletes_and_returns(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = self._plan(tmp, "discard.md")
            self.assertTrue(p.path.exists())
            inputs = iter(["discard"])
            result = repl(p, cwd=tmp, input_fn=lambda _: next(inputs))
            self.assertEqual(result, "discard")
            self.assertFalse(p.path.exists())

    def test_repl_exit_returns_exit(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = self._plan(tmp, "exit.md")
            inputs = iter(["exit"])
            result = repl(p, cwd=tmp, input_fn=lambda _: next(inputs))
            self.assertEqual(result, "exit")

    def test_repl_refine_uses_callback(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = self._plan(tmp, "refine.md")
            refined = _sample_plan(proposed_changes="Refined change set: 1. do X 2. do Y")

            def fake_refine(plan, note):
                self.assertEqual(note, "be more specific")
                return refined

            inputs = iter(["refine be more specific", "exit"])
            repl(p, cwd=tmp, refine_callback=fake_refine, input_fn=lambda _: next(inputs))
            on_disk = parse_plan(p.path)
            self.assertIn("Refined change set", on_disk.proposed_changes)

    def test_repl_apply_runs_allowed_commands(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            p = self._plan(tmp, "apply.md", commands="pwd")
            inputs = iter(["apply", "exit"])
            result = repl(
                p,
                cwd=tmp,
                ask=lambda prompt: True,  # always say yes
                input_fn=lambda _: next(inputs),
            )
            self.assertEqual(result, "exit")

    def test_repl_apply_blocks_disallowed_commands(self):
        import tempfile, contextlib
        with tempfile.TemporaryDirectory() as td, contextlib.redirect_stdout(io.StringIO()) as out:
            tmp = Path(td)
            p = self._plan(tmp, "blocked.md", commands="rm -rf /")
            yes_ask = lambda prompt: True
            inputs = iter(["apply", "exit"])
            repl(
                p,
                cwd=tmp,
                ask=yes_ask,
                input_fn=lambda _: next(inputs),
            )
            self.assertIn("blocked", out.getvalue().lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
