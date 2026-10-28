"""Test the agent-worker launcher."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import select
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "agent-worker"
SCRIPT_LOADER = importlib.machinery.SourceFileLoader("agent_worker", str(SCRIPT_PATH))
SPEC = importlib.util.spec_from_loader("agent_worker", SCRIPT_LOADER)
assert SPEC is not None
agent_worker = importlib.util.module_from_spec(SPEC)
sys.modules["agent_worker"] = agent_worker
SCRIPT_LOADER.exec_module(agent_worker)

ALL_EFFORTS = ["none", "low", "medium", "high", "xhigh", "max"]
CATALOG = {
    "openai": {
        "models": {
            "gpt-6-luna": {
                "reasoning_options": [{"type": "effort", "values": ALL_EFFORTS}]
            },
            "gpt-6.1-sol": {
                "reasoning_options": [{"type": "effort", "values": ALL_EFFORTS[1:-1]}]
            },
        }
    }
}
PI_SETTINGS = {"packages": ["npm:pi-web-access"]}
CLAUDE_SKILLS = ("other-skill", "review-judge")
SHARED_SKILLS = ("review-correctness", "review-readability")
PROCESS_TIMEOUT_SECONDS = 10.0


class FakeHomeTestCase(unittest.TestCase):
    """Base for tests run from a project directory, with a home holding harness skills and settings, and stub harnesses alone on PATH."""

    def setUp(self) -> None:
        """Create the home, project and stub directories, and enter the project."""
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = root / "home"
        self.pi_dir = self.home / ".config/pi"
        project = root / "project"
        (project / ".git").mkdir(parents=True)
        for path, content in (
            (self.home / ".cache/hax/catalog.json", CATALOG),
            (self.pi_dir / "agent/settings.json", PI_SETTINGS),
            (self.pi_dir / "agent/npm/node_modules/pi-web-access/package.json", {}),
        ):
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(content))
        for skills_dir, names in (
            (self.home / ".claude/skills", CLAUDE_SKILLS),
            (self.home / ".agents/skills", SHARED_SKILLS),
        ):
            for name in names:
                (skills_dir / name).mkdir(parents=True)
                (skills_dir / name / "SKILL.md").write_text(f"# {name}\n")
        self.bin_dir = self.home / "bin"
        self.bin_dir.mkdir()
        for harness in agent_worker.Harness:
            (self.bin_dir / harness).write_text("#!/bin/sh\n")
            (self.bin_dir / harness).chmod(0o755)
        self.enterContext(
            unittest.mock.patch.dict(
                os.environ,
                {"HOME": str(self.home), "PATH": str(self.bin_dir)},
                clear=True,
            )
        )
        self.sandbox_dir = root / "sandbox"
        self.enterContext(
            unittest.mock.patch.object(agent_worker, "SANDBOX_DIR", self.sandbox_dir)
        )
        self.enterContext(contextlib.chdir(project))

    def assert_refusal(self, command: str, message: str) -> None:
        """Check a command line is refused with a message."""
        with self.assertRaises(SystemExit) as raised:
            launch(command)
        self.assertEqual(str(raised.exception), message)


def launch(command: str) -> agent_worker.Launch:
    """Build the launch of a command line whose prompt is given as arguments."""
    request = agent_worker.parse_request(shlex.split(command))
    return agent_worker.select_launch(request, " ".join(request.prompt))


class TranslationTests(FakeHomeTestCase):
    """Tests of the harness launch a command line translates into."""

    def assert_launch(
        self,
        command: str,
        *,
        argv: str | list[str],
        env: dict[str, str] | None = None,
        stdin: str = "PROMPT",
    ) -> None:
        """Check the launch a command line translates into."""
        expected_argv = shlex.split(argv) if isinstance(argv, str) else argv
        expected = agent_worker.Launch(expected_argv, {} if env is None else env, stdin)
        self.assertEqual(launch(command), expected)

    def test_bare_gpt_runs_raw_hax(self) -> None:
        """Run a bare request for a GPT model as a raw hax."""
        self.assert_launch(
            "--preset bare --model gpt-6-luna --effort none --max-turns 3 --no-session --json -- PROMPT",
            argv="hax --raw --json --no-session --provider=codex --model=gpt-6-luna --effort=none -p",
            env={"HAX_MAX_TURNS": "3"},
        )

    def test_bare_forced_on_pi(self) -> None:
        """Give pi a placeholder system prompt, and an empty appendix unless one is given."""
        for option, appended in (
            ("", "''"),
            ("--append-system-prompt APPENDED", "APPENDED"),
        ):
            with self.subTest(option=option):
                self.assert_launch(
                    f"--preset bare --harness pi --model gpt-6.1-sol --effort none --no-session {option} -- PROMPT",
                    argv=f"pi --model openai-codex/gpt-6.1-sol:off --no-tools --no-extensions --no-skills --no-context-files --no-mcp --system-prompt 'You are a helpful assistant.' --append-system-prompt {appended} --no-session -p",
                )

    def test_full_without_subagents_runs_hax(self) -> None:
        """Remove hax's subagents through its environment."""
        self.assert_launch(
            "--model gpt-6.1-sol --effort high --tools -subagent -- PROMPT",
            argv="hax --provider=codex --model=gpt-6.1-sol --effort=high -p",
            env={"HAX_NO_SUBAGENTS": "1"},
        )

    def test_lean_gpt_runs_pi(self) -> None:
        """Run a lean request for a GPT model on pi, hax taking no shell-only tool list."""
        self.assert_launch(
            "--preset lean --model gpt-6.1-sol --effort low --json -- PROMPT",
            argv="pi --model openai-codex/gpt-6.1-sol:low --tools bash --no-extensions --no-skills --no-context-files --no-mcp --system-prompt 'You are a helpful assistant.' --append-system-prompt '' --mode json -p",
        )

    def test_standard_gpt_runs_hax(self) -> None:
        """Run a standard request for a GPT model on hax."""
        self.assert_launch(
            "--preset standard --model gpt-6.1-sol --effort low -- PROMPT",
            argv="hax --provider=codex --model=gpt-6.1-sol --effort=low -p",
            env={"HAX_NO_TASKS": "1", "HAX_NO_SUBAGENTS": "1", "HAX_NO_SKILLS": "1"},
        )

    def test_standard_claude(self) -> None:
        """Give claude the shell and file tools, keeping its system prompt and instruction files."""
        self.assert_launch(
            "--preset standard --model opus -- PROMPT",
            argv="""claude --model opus --tools Bash,Read,Write,Edit --disable-slash-commands --strict-mcp-config --settings '{"autoMemoryEnabled": false, "includeGitInstructions": false}' --permission-mode bypassPermissions -p""",
        )

    def test_session_id_runs_pi(self) -> None:
        """Run on pi a GPT request naming its session, hax taking no session id."""
        self.assert_launch(
            "--model gpt-6.1-sol --session-id 3f2c5a0e-8d1b-4c6e-9a7f-1b2d3e4f5a6b -- PROMPT",
            argv="pi --model openai-codex/gpt-6.1-sol --no-mcp --session-id 3f2c5a0e-8d1b-4c6e-9a7f-1b2d3e4f5a6b -p",
        )

    def test_bare_gpt_keeping_context_runs_pi(self) -> None:
        """Run on pi a bare GPT request keeping skills, instruction files or a system prompt, raw hax taking none of them."""
        for option in (
            "--instruction-files",
            "--skills review-correctness",
            "--invoke-skill review-correctness",
            "--system-prompt TEXT",
            "--append-system-prompt TEXT",
        ):
            with self.subTest(option=option):
                command = f"--preset bare --model gpt-6-luna {option}"
                self.assertEqual(launch(command).argv[0], "pi")

    def test_hax_constraints_fall_back_to_pi(self) -> None:
        """Run on pi a GPT request removing a tool hax always has, or keeping MCP servers."""
        for option, argv in (
            (
                "--tools -read",
                "pi --model openai-codex/gpt-6-luna --exclude-tools read --no-mcp -p",
            ),
            ("--mcp", "pi --model openai-codex/gpt-6-luna -p"),
        ):
            with self.subTest(option=option):
                self.assertEqual(
                    launch(f"--model gpt-6-luna {option}").argv, shlex.split(argv)
                )

    def test_hax_context_through_environment(self) -> None:
        """Give hax its tool list, skills, instruction files and system prompt through its environment, and the invoked skill by path."""
        self.assert_launch(
            "--model gpt-6-luna --tools shell,read,write,edit --no-skills --no-instruction-files --system-prompt SYSTEM --append-system-prompt APPENDED --invoke-skill review-correctness -- ARGS",
            argv="hax --provider=codex --model=gpt-6-luna -p",
            env={
                "HAX_NO_TASKS": "1",
                "HAX_NO_SUBAGENTS": "1",
                "HAX_NO_SKILLS": "1",
                "HAX_NO_AGENTS_MD": "1",
                "HAX_SYSTEM_PROMPT": "SYSTEM",
                "HAX_SYSTEM_PROMPT_APPEND": "APPENDED",
            },
            stdin=f"Read {self.home / '.agents/skills/review-correctness/SKILL.md'} and follow its instructions. Arguments: ARGS",
        )

    def test_read_only_gpt_skill_runs_pi(self) -> None:
        """Load the invoked skill by path on pi, and invoke it through the prompt."""
        self.assert_launch(
            "--preset read-only --model gpt-6-astra --effort xhigh --invoke-skill review-correctness -- ARGS",
            argv=[
                "pi",
                "--model",
                "openai-codex/gpt-6-astra:xhigh",
                "--tools",
                "read,grep,find,ls",
                "--no-extensions",
                "--no-skills",
                "--skill",
                str(self.home / ".agents/skills/review-correctness"),
                "--no-mcp",
                "-p",
            ],
            stdin="/skill:review-correctness ARGS",
        )

    def test_full_gpt_skill_runs_pi(self) -> None:
        """Load the invoked skill by path on pi, along with the skills pi discovers."""
        self.assert_launch(
            "--harness pi --model gpt-6.1-sol --invoke-skill review-correctness -- ARGS",
            argv=[
                "pi",
                "--model",
                "openai-codex/gpt-6.1-sol",
                "--skill",
                str(self.home / ".agents/skills/review-correctness"),
                "--no-mcp",
                "-p",
            ],
            stdin="/skill:review-correctness ARGS",
        )

    def test_gpt_skill_subset_runs_pi(self) -> None:
        """Run on pi a GPT request keeping a subset of the skills, hax loading all or none."""
        for option in ("--skills review-readability", "--skills -review-correctness"):
            with self.subTest(option=option):
                self.assertEqual(
                    launch(f"--model gpt-6-luna {option}").argv,
                    [
                        "pi",
                        "--model",
                        "openai-codex/gpt-6-luna",
                        "--no-skills",
                        "--skill",
                        str(self.home / ".agents/skills/review-readability"),
                        "--no-mcp",
                        "-p",
                    ],
                )

    def test_skill_missing_from_hax_runs_pi(self) -> None:
        """Run on pi a GPT request invoking a skill only pi finds, by its path in the view of pi's sandbox."""
        skill = self.pi_dir / "agent/skills/pi-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# pi-skill\n")
        self.assert_launch(
            "--model gpt-6-luna --invoke-skill pi-skill -- ARGS",
            argv=[
                "pi",
                "--model",
                "openai-codex/gpt-6-luna",
                "--skill",
                "~/.pi/agent/skills/pi-skill",
                "--no-mcp",
                "-p",
            ],
            stdin="/skill:pi-skill ARGS",
        )

    def test_read_only_claude_skill(self) -> None:
        """Turn on the invoked claude skill and off every other, and invoke it through the prompt."""
        self.assert_launch(
            "--preset read-only --model claude-fable-5-1 --effort xhigh --no-system-prompt --invoke-skill review-judge -- PROMPT",
            argv=[
                "claude",
                "--model",
                "claude-fable-5-1",
                "--effort",
                "xhigh",
                "--tools",
                "Read,Grep,Glob,Skill",
                "--strict-mcp-config",
                "--system-prompt",
                "",
                "--settings",
                (
                    '{"autoMemoryEnabled": false, "includeGitInstructions": false, '
                    '"disableBundledSkills": true, "skillOverrides": '
                    '{"other-skill": "off", "plugin-authoring": "off", "review-judge": "on"}}'
                ),
                "--permission-mode",
                "bypassPermissions",
                "-p",
            ],
            stdin="/review-judge PROMPT",
        )

    def test_claude_skill_adjustments(self) -> None:
        """Turn added claude skills on and removed ones off, leaving the others as they are."""
        self.assert_launch(
            "--model opus --skills +other-skill,-review-judge -- PROMPT",
            argv=[
                "claude",
                "--model",
                "opus",
                "--strict-mcp-config",
                "--settings",
                '{"skillOverrides": {"other-skill": "on", "review-judge": "off"}}',
                "--permission-mode",
                "bypassPermissions",
                "-p",
            ],
        )

    def test_claude_request_options(self) -> None:
        """Pass claude the options a request sets, effort none becoming low."""
        self.assert_launch(
            "--model opus --effort none --mcp --system-prompt SYSTEM --max-turns 3 --no-session --json -- PROMPT",
            argv="claude --model opus --effort low --system-prompt SYSTEM --max-turns 3 --no-session-persistence --output-format stream-json --verbose --permission-mode bypassPermissions -p",
        )

    def test_hax_effort_raised_to_model_level(self) -> None:
        """Raise an effort level hax's catalog lacks for the model to the next one it has."""
        self.assertEqual(
            launch("--preset bare --model gpt-6.1-sol --effort none").argv,
            shlex.split(
                "hax --raw --provider=codex --model=gpt-6.1-sol --effort=low -p"
            ),
        )

    def test_hax_catalog_gap_falls_back_to_pi(self) -> None:
        """Run on pi when hax's catalog cannot give the model the requested effort."""
        self.assertEqual(launch("--model gpt-6-astra --effort low").argv[0], "pi")
        self.assertEqual(launch("--model gpt-6.1-sol --effort max").argv[0], "pi")
        (self.home / ".cache/hax/catalog.json").unlink()
        self.assertEqual(launch("--model gpt-6-luna --effort low").argv[0], "pi")

    def test_missing_harness_skipped(self) -> None:
        """Run on pi when hax is not installed."""
        (self.bin_dir / "hax").unlink()
        self.assertEqual(launch("--preset bare --model gpt-6-luna").argv[0], "pi")

    def test_adjustment_of_preset_tool_list(self) -> None:
        """Add a tool to the list of a preset."""
        self.assertEqual(
            launch("--preset read-only --tools +web --model gpt-6.1-sol").argv,
            shlex.split(
                "pi --model openai-codex/gpt-6.1-sol --tools read,grep,find,ls,web_search,fetch_content,get_search_content,source_check --no-extensions -e ~/.pi/agent/npm/node_modules/pi-web-access --no-skills --no-mcp -p"
            ),
        )

    def test_tool_removals(self) -> None:
        """Remove tools from a harness's own set, along with an addition, and from a preset list."""
        for command, argv in (
            (
                "--model opus --tools -shell",
                "claude --model opus --disallowedTools Bash --strict-mcp-config --permission-mode bypassPermissions -p",
            ),
            (
                "--model gpt-6.1-sol --tools +grep,-shell",
                "pi --model openai-codex/gpt-6.1-sol --tools +grep --exclude-tools bash --no-mcp -p",
            ),
            (
                "--harness pi --model gpt-6.1-sol --tools -web",
                "pi --model openai-codex/gpt-6.1-sol --exclude-tools web_search,fetch_content,get_search_content,source_check --no-mcp -p",
            ),
            (
                "--preset read-only --tools -read --model gpt-6.1-sol",
                "pi --model openai-codex/gpt-6.1-sol --tools grep,find,ls --no-extensions --no-skills --no-mcp -p",
            ),
        ):
            with self.subTest(command=command):
                self.assertEqual(launch(command).argv, shlex.split(argv))

    def test_pi_web_needs_package(self) -> None:
        """Refuse web tools on pi when its settings do not install the package providing them."""
        (self.pi_dir / "agent/settings.json").unlink()
        self.assert_refusal(
            "--preset read-only --tools +web --model gpt-6.1-sol",
            "No harness can satisfy the request:\n"
            "  hax: takes no tool list but shell,read,write,edit, with or without subagent\n"
            "  pi: has no web tool without the pi-web-access extension",
        )

    def test_pi_lacking_tool(self) -> None:
        """Refuse adding a tool pi lacks, and ignore its removal."""
        self.assertEqual(
            launch("--harness pi --model gpt-6.1-sol --tools -subagent").argv,
            shlex.split("pi --model openai-codex/gpt-6.1-sol --no-mcp -p"),
        )
        self.assert_refusal(
            "--harness pi --model gpt-6.1-sol --tools +subagent",
            "No harness can satisfy the request:\n  pi: has no subagent tool",
        )

    def test_addition_to_claude_own_tools(self) -> None:
        """Spell out the tools claude enables on its own when adding one to them."""
        self.assertEqual(
            launch("--model opus --tools +grep").argv,
            [
                "claude",
                "--model",
                "opus",
                "--tools",
                ",".join([*agent_worker.CLAUDE_OWN_TOOLS, "Grep", "Skill"]),
                "--strict-mcp-config",
                "--permission-mode",
                "bypassPermissions",
                "-p",
            ],
        )

    def test_optional_selections(self) -> None:
        """Launch optional names a harness has as additions, and the others as omitted."""
        skill = self.pi_dir / "agent/skills/pi-only"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# pi-only\n")
        for optional, equivalent in (
            ("--model opus --tools ?grep", "--model opus --tools +grep"),
            (
                "--model gpt-6-luna --tools ?web,?subagent",
                "--model gpt-6-luna --tools +subagent",
            ),
            (
                "--harness pi --model gpt-6.1-sol --tools ?grep,?subagent",
                "--harness pi --model gpt-6.1-sol --tools +grep",
            ),
            (
                "--preset read-only --tools ?web --model gpt-6.1-sol",
                "--preset read-only --tools +web --model gpt-6.1-sol",
            ),
            (
                "--preset bare --model gpt-6-luna --tools ?shell",
                "--preset bare --model gpt-6-luna --tools +shell",
            ),
            (
                "--preset bare --model gpt-6-luna --tools ?shell,?read,?write,?edit",
                "--preset bare --model gpt-6-luna --tools +shell,+read,+write,+edit",
            ),
            (
                "--preset bare --model gpt-6-luna --skills ?review-correctness",
                "--preset bare --model gpt-6-luna --skills +review-correctness",
            ),
            (
                "--model opus --skills ?other-skill,?missing-skill",
                "--model opus --skills +other-skill",
            ),
            ("--model gpt-6-luna --skills ?missing-skill", "--model gpt-6-luna"),
            ("--model gpt-6-luna --skills ?pi-only", "--model gpt-6-luna"),
            (
                "--model gpt-6-luna --tools -read --skills ?pi-only",
                "--model gpt-6-luna --tools -read --skills +pi-only",
            ),
            (
                "--preset lean --model gpt-6.1-sol --skills ?review-correctness,?missing-skill",
                "--preset lean --model gpt-6.1-sol --skills +review-correctness",
            ),
        ):
            with self.subTest(optional=optional):
                self.assertEqual(launch(optional), launch(equivalent))

    def test_optional_pi_web_needs_package(self) -> None:
        """Ignore optional web tools on pi when its settings do not install the package providing them."""
        settings = self.pi_dir / "agent/settings.json"
        for package in ("unrelated", "pi-web-access-extra"):
            (self.pi_dir / "agent/npm/node_modules" / package).mkdir()
        for content in (
            None,
            {},
            {"packages": ["npm:unrelated"]},
            {"packages": ["npm:pi-web-access-extra"]},
        ):
            with self.subTest(content=content):
                if content is None:
                    settings.unlink()
                else:
                    settings.write_text(json.dumps(content))
                self.assertEqual(
                    launch("--preset lean --tools ?grep,?web --model gpt-6.1-sol"),
                    launch("--preset lean --tools +grep --model gpt-6.1-sol"),
                )

    def test_pi_web_package_settings(self) -> None:
        """Find the package providing pi's web tools in a pinned or object declaration, and in settings starting with a byte order mark."""
        settings = self.pi_dir / "agent/settings.json"
        for content in (
            json.dumps({"packages": ["npm:pi-web-access@0.37.0"]}),
            json.dumps({"packages": [{"source": "npm:pi-web-access"}]}),
            f"\ufeff{json.dumps(PI_SETTINGS)}",
        ):
            with self.subTest(content=content):
                settings.write_text(content)
                self.assertEqual(
                    launch("--preset read-only --tools ?web --model gpt-6.1-sol"),
                    launch("--preset read-only --tools +web --model gpt-6.1-sol"),
                )

    def test_pi_scoped_npm_package(self) -> None:
        """Name a pi npm package by its scope and name, whether pinned or not."""
        (self.pi_dir / "agent/npm/node_modules/@scope/probe").mkdir(parents=True)
        for source in ("npm:@scope/probe", "npm:@scope/probe@1.2.3"):
            with self.subTest(source=source):
                (self.pi_dir / "agent/settings.json").write_text(
                    json.dumps({"packages": [source]})
                )
                self.assertEqual(
                    launch(
                        "--harness pi --model gpt-6.1-sol --extensions +@scope/probe"
                    ).argv,
                    shlex.split(
                        "pi --model openai-codex/gpt-6.1-sol -e ~/.pi/agent/npm/node_modules/@scope/probe --no-mcp -p"
                    ),
                )

    def test_pi_extension_selection(self) -> None:
        """Load pi extensions by path in pi's view, on top of those pi loads or alone, mcp following the MCP option."""
        extensions = self.pi_dir / "agent/extensions"
        (extensions / "tools").mkdir(parents=True)
        (extensions / "tools/index.ts").write_text("")
        (extensions / "empty").mkdir()
        for name in ("handoff.ts", "notes.md", ".hidden.ts"):
            (extensions / name).write_text("")
        web = "~/.pi/agent/npm/node_modules/pi-web-access"
        for options, args in (
            ("--extensions +handoff", "-e ~/.pi/agent/extensions/handoff.ts --no-mcp"),
            (
                "--extensions -handoff",
                f"--no-extensions -e builtin:codemode -e builtin:llama.cpp -e {web} -e builtin:tool-search -e ~/.pi/agent/extensions/tools --no-mcp",
            ),
            (
                "--preset standard --extensions +tools --mcp",
                "--tools bash,read,write,edit --no-extensions -e ~/.pi/agent/extensions/tools -e builtin:mcp --no-skills",
            ),
            ("--no-extensions", "--no-extensions --no-mcp"),
            (
                "--extensions ?handoff,?missing",
                "-e ~/.pi/agent/extensions/handoff.ts --no-mcp",
            ),
        ):
            with self.subTest(options=options):
                self.assertEqual(
                    launch(f"--harness pi --model gpt-6.1-sol {options}").argv,
                    shlex.split(f"pi --model openai-codex/gpt-6.1-sol {args} -p"),
                )

    def test_pi_web_tools_load_their_extension(self) -> None:
        """Load the extension providing pi's web tools along with them, even when extensions are off or it is removed."""
        web = "-e ~/.pi/agent/npm/node_modules/pi-web-access"
        for options, args in (
            (
                "--preset standard --tools +web",
                f"--tools bash,read,write,edit,web_search,fetch_content,get_search_content,source_check --no-extensions {web} --no-skills",
            ),
            (
                "--tools +web --extensions -pi-web-access",
                f"--tools +web_search,+fetch_content,+get_search_content,+source_check {web}",
            ),
        ):
            with self.subTest(options=options):
                self.assertEqual(
                    launch(f"--harness pi --model gpt-6.1-sol {options}").argv,
                    shlex.split(
                        f"pi --model openai-codex/gpt-6.1-sol {args} --no-mcp -p"
                    ),
                )

    def test_harness_dir_follows_sandbox_mount(self) -> None:
        """Locate a harness's directory where this process sees it, inside a sandbox or not."""
        xdg_config_home = self.home / "xdg"
        for harness in (agent_worker.Harness.CLAUDE, agent_worker.Harness.PI):
            for environ, sandbox_dir, directory in (
                ({}, self.sandbox_dir, self.home / ".config" / harness),
                (
                    {"XDG_CONFIG_HOME": str(xdg_config_home)},
                    self.sandbox_dir,
                    xdg_config_home / harness,
                ),
                (
                    {"XDG_CONFIG_HOME": str(xdg_config_home)},
                    self.home,
                    self.home / f".{harness}",
                ),
            ):
                with (
                    self.subTest(directory=directory),
                    unittest.mock.patch.dict(os.environ, environ),
                    unittest.mock.patch.object(
                        agent_worker, "SANDBOX_DIR", sandbox_dir
                    ),
                ):
                    self.assertEqual(agent_worker.harness_dir(harness), directory)

    def test_pi_extension_selection_refused(self) -> None:
        """Refuse selecting pi extensions it lacks, or alongside settings it cannot follow."""
        for settings, selection, refusal in (
            (PI_SETTINGS, "+missing", "has no extension missing"),
            (PI_SETTINGS, "+missing,-codemode", "has no extension missing"),
            (
                {"extensions": ["-builtin:llama.cpp"]},
                "+missing",
                "cannot select extensions alongside its extensions setting",
            ),
            (
                {"packages": [{"source": "npm:pi-web-access", "extensions": []}]},
                "+missing",
                "cannot select extensions alongside the filtered package npm:pi-web-access",
            ),
            (
                {"packages": ["git:github.com/user/repo"]},
                "+missing",
                "cannot select extensions alongside the non-npm package git:github.com/user/repo",
            ),
            (
                {"packages": ["npm:absent"]},
                "+missing",
                "cannot select extensions alongside the package npm:absent, missing from its npm directory",
            ),
        ):
            with self.subTest(settings=settings, selection=selection):
                (self.pi_dir / "agent/settings.json").write_text(json.dumps(settings))
                self.assert_refusal(
                    f"--harness pi --model gpt-6.1-sol --extensions {selection}",
                    f"No harness can satisfy the request:\n  pi: {refusal}",
                )

    def test_pi_extension_layout_refused(self) -> None:
        """Refuse selecting pi extensions in a layout it cannot follow."""
        bundle = {"pi": {"extensions": ["index.js"], "skills": ["skills"]}}
        for index, (files, refusal) in enumerate(
            (
                (
                    {"extensions/.gitignore": "*.js\n"},
                    "cannot select extensions alongside the .gitignore of its extensions directory",
                ),
                (
                    {"extensions/index.ts": ""},
                    "cannot select extensions alongside the index.ts of its extensions directory",
                ),
                (
                    {"extensions/probe.js": "", "extensions/probe/index.js": ""},
                    "has two extensions named probe",
                ),
                (
                    {"extensions/pi-web-access.ts": ""},
                    "has two extensions named pi-web-access",
                ),
                (
                    {"extensions/bundle/package.json": json.dumps(bundle)},
                    "cannot select extensions alongside the extension bundle carrying skills",
                ),
                (
                    {"npm/node_modules/pi-web-access/skills/web/SKILL.md": ""},
                    "cannot select extensions alongside the extension pi-web-access carrying skills",
                ),
            )
        ):
            config_home = self.home / f"config-{index}"
            for path, content in {
                "settings.json": json.dumps(PI_SETTINGS),
                "npm/node_modules/pi-web-access/package.json": "{}",
                **files,
            }.items():
                (config_home / "pi/agent" / path).parent.mkdir(
                    parents=True, exist_ok=True
                )
                (config_home / "pi/agent" / path).write_text(content)
            with (
                self.subTest(refusal=refusal),
                unittest.mock.patch.dict(
                    os.environ, {"XDG_CONFIG_HOME": str(config_home)}
                ),
            ):
                self.assert_refusal(
                    "--harness pi --model gpt-6.1-sol --extensions -codemode",
                    f"No harness can satisfy the request:\n  pi: {refusal}",
                )

    def test_pi_extension_settings_unread_without_selection(self) -> None:
        """Launch pi alongside extension settings it cannot follow when the request mentions no extension and asks for no web tool."""
        (self.pi_dir / "agent/settings.json").write_text(
            json.dumps({"extensions": ["-builtin:llama.cpp"]})
        )
        for options in ("", "--no-extensions", "--tools -web"):
            with self.subTest(options=options):
                self.assertEqual(
                    launch(f"--harness pi --model gpt-6.1-sol {options}").argv[0], "pi"
                )

    def test_hax_has_no_extensions(self) -> None:
        """Run on pi a GPT request adding or listing extensions, hax having none, and on hax one turning them off, removing or optionally adding one."""
        (self.pi_dir / "agent/extensions").mkdir()
        (self.pi_dir / "agent/extensions/handoff.ts").write_text("")
        for options, harness in (
            ("--extensions +handoff", "pi"),
            ("--extensions handoff", "pi"),
            ("--extensions ?handoff", "hax"),
            ("--extensions -handoff", "hax"),
            ("--no-extensions", "hax"),
        ):
            with self.subTest(options=options):
                self.assertEqual(
                    launch(f"--model gpt-6-luna {options}").argv[0], harness
                )

    def test_claude_plugin_selection(self) -> None:
        """Turn claude plugins on and off through its settings, every installed one when given a list, and refuse one not installed."""
        registry = self.home / ".config/claude/plugins/installed_plugins.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(
            json.dumps({"version": 2, "plugins": {"probe@mp": [], "other@mp": []}})
        )
        for options, enabled in (
            ("--extensions +probe@mp", {"probe@mp": True}),
            ("--extensions +probe@mp,-other@mp", {"probe@mp": True, "other@mp": False}),
            (
                "--preset lean --extensions ?probe@mp,?missing@mp",
                {"probe@mp": True, "other@mp": False},
            ),
            ("--preset lean", {"probe@mp": False, "other@mp": False}),
        ):
            with self.subTest(options=options):
                argv = launch(f"--model opus {options}").argv
                settings = json.loads(argv[argv.index("--settings") + 1])
                self.assertEqual(settings["enabledPlugins"], enabled)
        self.assertNotIn("--settings", launch("--model opus").argv)
        self.assert_refusal(
            "--model opus --extensions +missing@mp",
            "No harness can satisfy the request:\n  claude: has no plugin missing@mp",
        )

    def test_fallback_skill_dir(self) -> None:
        """Look a skill up in the shared skill directory when no harness directory has it."""
        skill = self.home / ".config/agents/skills/fallback-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# fallback-skill\n")
        self.assertEqual(
            launch(
                "--preset standard --skills fallback-skill --model gpt-6.1-sol"
            ).argv,
            [
                "pi",
                "--model",
                "openai-codex/gpt-6.1-sol",
                "--tools",
                "bash,read,write,edit",
                "--no-extensions",
                "--no-skills",
                "--skill",
                str(skill),
                "--no-mcp",
                "-p",
            ],
        )

    def test_project_skills_found_up_to_repository_root(self) -> None:
        """Find the skills of the working directory and its parents up to the repository root, ahead of the home ones."""
        project = Path.cwd()
        for skill in (
            project / ".agents/skills/review-correctness",
            project.parent / ".agents/skills/outside-skill",
        ):
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"# {skill.name}\n")
        (project / "sub").mkdir()
        self.enterContext(contextlib.chdir(project / "sub"))
        skills = agent_worker.find_skills(agent_worker.Harness.PI)
        self.assertEqual(
            (skills["review-correctness"], "outside-skill" in skills),
            (project / ".agents/skills/review-correctness", False),
        )

    def test_caller_arguments_override_preset(self) -> None:
        """Let the caller's arguments win over those a preset implies."""
        self.assert_launch(
            "--preset bare --memory --auth work --model sonnet",
            argv=[
                "claude",
                "--model",
                "sonnet",
                "--tools",
                "",
                "--disable-slash-commands",
                "--strict-mcp-config",
                "--system-prompt",
                "",
                "--settings",
                '{"claudeMdExcludes": ["**"], "includeGitInstructions": false}',
                "--permission-mode",
                "bypassPermissions",
                "-p",
            ],
            env={"SANDBOX_AGENT_CLAUDE_AUTH": "work"},
            stdin="",
        )

    def test_auth_switch_refused_inside_sandbox(self) -> None:
        """Refuse inside a sandbox an auth profile other than the one it mounted."""
        self.sandbox_dir.mkdir()
        with unittest.mock.patch.dict(os.environ, {"SANDBOX_AGENT_CLAUDE_AUTH": "pro"}):
            self.assertEqual(
                launch("--model opus --auth pro").env,
                {"SANDBOX_AGENT_CLAUDE_AUTH": "pro"},
            )
            self.assert_refusal(
                "--model opus --auth work",
                "No harness can satisfy the request:\n"
                "  claude: cannot switch from the auth profile its sandbox mounted, pro",
            )

    def test_prompt_file_read_once(self) -> None:
        """Read a system prompt file once, a pipe giving its content only once."""
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        os.write(write_end, b"SYSTEM")
        os.close(write_end)
        request = agent_worker.parse_request(
            ["--model", "opus", "--system-prompt", f"@/dev/fd/{read_end}"]
        )
        self.assertEqual(request.system_prompt, "SYSTEM")

    def test_malformed_selection_rejected(self) -> None:
        """Reject malformed selections."""
        for option, message in (
            ("--skills --no-tools", "argument --skills: expected one argument"),
            (
                "--extensions --no-tools",
                "argument --extensions: expected one argument",
            ),
            ("--skills ''", "Empty name in --skills"),
            (
                "--skills review-correctness,+other",
                "--skills takes names or +/-/? adjustments, not both",
            ),
            ("--tools unknown", "Unknown tools: unknown"),
            ("--tools ?unknown", "Unknown tools: unknown"),
        ):
            with self.subTest(option=option):
                stderr = io.StringIO()
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(stderr):
                    agent_worker.parse_request(shlex.split(f"--model opus {option}"))
                self.assertIn(message, stderr.getvalue())

    def test_adjustment_precedence(self) -> None:
        """Let removals win over additions and additions over optional ones within an adjustment of a selection, and a later adjustment override an earlier one on the names they share."""
        grep = frozenset({"grep"})
        added = agent_worker.Selection(None, added=grep)
        optional = agent_worker.Selection(None, optional=grep)
        removed = agent_worker.Selection(None, removed=grep)
        for options, expected in (
            ("--tools +grep --tools -grep", removed),
            ("--tools -grep --tools +grep", added),
            ("--tools +grep,-grep", removed),
            ("--tools read --tools +read,-read", agent_worker.Selection(frozenset())),
            ("--tools +grep --tools ?grep", optional),
            ("--tools ?grep --tools +grep", added),
            ("--tools ?grep,+grep", added),
            ("--tools ?grep,-grep", removed),
            (
                "--tools read --tools ?read",
                agent_worker.Selection(frozenset(), optional=frozenset({"read"})),
            ),
            (
                "--tools +grep,-read,?find --tools -web",
                agent_worker.Selection(
                    None,
                    added=grep,
                    optional=frozenset({"find"}),
                    removed=frozenset({"read", "web"}),
                ),
            ),
            (
                "--tools shell --tools ?grep --tools ?find",
                agent_worker.Selection(
                    frozenset({"shell"}), optional=frozenset({"grep", "find"})
                ),
            ),
        ):
            with self.subTest(options=options):
                request = agent_worker.parse_request(
                    shlex.split(f"--model opus {options}")
                )
                self.assertEqual(request.tools, expected)

    def test_unknown_model_rejected(self) -> None:
        """Refuse a model of no known family."""
        self.assert_refusal(
            "--model unsupported-model", "Unknown model family: unsupported-model"
        )

    def test_incompatible_harness_refused(self) -> None:
        """Refuse a forced harness that does not run the model."""
        self.assert_refusal(
            "--model gpt-6-luna --harness claude",
            "Harness claude does not run gpt-6-luna",
        )

    def test_unsatisfiable_request_names_every_refusal(self) -> None:
        """Fail with each candidate harness's reason when none can satisfy the request."""
        self.assert_refusal(
            "--preset lean --max-turns 3 --model gpt-6.1-sol",
            "No harness can satisfy the request:\n"
            "  hax: takes no tool list but shell,read,write,edit, with or without subagent\n"
            "  pi: has no turn cap",
        )


class ProcessTests(FakeHomeTestCase):
    """Tests running the launcher on fake harnesses."""

    def setUp(self) -> None:
        """Make the stub harnesses scripts, finding the base commands on PATH."""
        super().setUp()
        for name, script in (
            ("claude", 'printf "%s\\n" "$@"; cat'),
            ("hax", 'cat; echo noise >&2; exit "$FAKE_STATUS"'),
        ):
            (self.bin_dir / name).write_text(f"#!/bin/sh\n{script}\n")
        self.env = os.environ | {"PATH": f"{self.bin_dir}:{os.defpath}"}

    def run_worker(
        self,
        command: str,
        env: dict[str, str] | None = None,
        stdin: str = "",
        stdin_fd: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run the launcher to completion."""
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *shlex.split(command)],
            input=None if stdin_fd is not None else stdin,
            stdin=stdin_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env | (env or {}),
            text=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )

    def start_worker(self, command: str) -> subprocess.Popen[str]:
        """Start the launcher, its output piped."""
        return subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH), *shlex.split(command)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
            text=True,
        )

    def test_prompt_reaches_exec_harness_stdin(self) -> None:
        """Feed the prompt given as arguments to an exec'd harness on its stdin."""
        result = self.run_worker("--model haiku -- --tools -read --skills +task")
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (
                0,
                "--model\nhaiku\n--strict-mcp-config\n--permission-mode\nbypassPermissions\n-p\n--tools -read --skills +task",
                "",
            ),
        )

    def test_piped_prompt_reaches_exec_harness_stdin(self) -> None:
        """Feed the prompt read on stdin, when no argument gives one, to an exec'd harness on its stdin."""
        prompt = "  first line\n--model second line\n"
        result = self.run_worker("--model haiku", stdin=prompt)
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr),
            (
                0,
                f"--model\nhaiku\n--strict-mcp-config\n--permission-mode\nbypassPermissions\n-p\n{prompt}",
                "",
            ),
        )

    def test_dry_run_prints_command(self) -> None:
        """Print the harness command line, its variables first and its values quoted, instead of running it, and without waiting for a prompt."""
        (self.bin_dir / "hax").write_text("#!/bin/sh\necho ran\n")
        read_end, write_end = os.pipe()
        self.addCleanup(os.close, read_end)
        self.addCleanup(os.close, write_end)
        for command, expected in (
            (
                "--model gpt-6-luna --tools -subagent --system-prompt 'be brief'",
                "HAX_NO_SUBAGENTS=1 HAX_SYSTEM_PROMPT='be brief' hax --provider=codex --model=gpt-6-luna -p\n",
            ),
            (
                "--model haiku --no-system-prompt",
                "claude --model haiku --strict-mcp-config --system-prompt '' --permission-mode bypassPermissions -p\n",
            ),
        ):
            with self.subTest(command=command):
                result = self.run_worker(f"-d {command}", stdin_fd=read_end)
                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr),
                    (0, expected, ""),
                )

    def test_help_preset_order(self) -> None:
        """List the presets in the help as full, standard, read-only, lean, bare."""
        result = self.run_worker("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--preset {full,standard,read-only,lean,bare}", result.stdout)

    def test_hax_stderr_shown_only_on_failure(self) -> None:
        """Hide hax's stderr when it succeeds, and show it with its exit status when it fails."""
        for status, stderr in (("0", ""), ("3", "noise\n")):
            with self.subTest(status=status):
                result = self.run_worker(
                    "--preset bare --model gpt-6-luna -- hello",
                    env={"FAKE_STATUS": status},
                )
                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr),
                    (int(status), "hello", stderr),
                )

    def test_hax_kill_propagated(self) -> None:
        """Die from the signal hax dies from."""
        for signum in (signal.SIGKILL, signal.SIGTERM):
            with self.subTest(signal=signum.name):
                (self.bin_dir / "hax").write_text(f"#!/bin/sh\nkill -{signum} $$\n")
                result = self.run_worker("--preset bare --model gpt-6-luna")
                self.assertEqual((result.returncode, result.stderr), (-signum, ""))

    def test_hax_killed_with_launcher(self) -> None:
        """Kill hax when the launcher dies."""
        (self.bin_dir / "hax").write_text("#!/bin/sh\necho ready\nexec sleep 60\n")
        with self.start_worker("--preset bare --model gpt-6-luna") as worker:
            assert worker.stdout is not None
            self.assertEqual(worker.stdout.readline(), "ready\n")
            worker.kill()
            closed, _, _ = select.select(
                [worker.stdout], [], [], PROCESS_TIMEOUT_SECONDS
            )
            self.assertEqual(closed, [worker.stdout])
            self.assertEqual(worker.stdout.read(), "")

    def test_inherited_hax_variables_dropped(self) -> None:
        """Drop the inherited hax variables a launch controls, and keep the others."""
        (self.bin_dir / "hax").write_text("#!/bin/sh\nenv | grep '^HAX_' | sort\n")
        result = self.run_worker(
            "--model gpt-6-luna --tools -subagent",
            env={"HAX_MAX_TURNS": "3", "HAX_SYSTEM_PROMPT": "parent", "HAX_TRACE": "1"},
        )
        self.assertEqual(
            (result.returncode, result.stdout), (0, "HAX_NO_SUBAGENTS=1\nHAX_TRACE=1\n")
        )

    def test_signal_forwarded_to_hax(self) -> None:
        """Forward a termination signal the launcher gets to hax."""
        (self.bin_dir / "hax").write_text(
            "#!/bin/sh\n"
            "trap 'echo forwarded; exit 0' TERM\n"
            "sleep 0.2\n"
            "echo ready\n"
            "while :; do sleep 0.05; done\n"
        )
        with self.start_worker("--preset bare --model gpt-6-luna") as worker:
            assert worker.stdout is not None
            self.assertEqual(worker.stdout.readline(), "ready\n")
            worker.send_signal(signal.SIGTERM)
            stdout, stderr = worker.communicate(timeout=PROCESS_TIMEOUT_SECONDS)
        self.assertEqual((worker.returncode, stdout, stderr), (0, "forwarded\n", ""))


if __name__ == "__main__":
    unittest.main()
