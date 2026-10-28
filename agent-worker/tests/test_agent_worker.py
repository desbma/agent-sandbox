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
        project = root / "project"
        (project / ".git").mkdir(parents=True)
        for path, content in (
            (self.home / ".cache/hax/catalog.json", CATALOG),
            (self.home / ".pi/agent/settings.json", PI_SETTINGS),
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
                    argv=f"pi --model openai-codex/gpt-6.1-sol:off --no-tools --no-skills --no-context-files --no-mcp --system-prompt 'You are a helpful assistant.' --append-system-prompt {appended} --no-session -p",
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
            argv="pi --model openai-codex/gpt-6.1-sol:low --tools bash --no-skills --no-mcp --mode json -p",
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
        """Run on pi a GPT request invoking a skill only pi finds."""
        skill = self.home / ".pi/agent/skills/pi-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# pi-skill\n")
        self.assert_launch(
            "--model gpt-6-luna --invoke-skill pi-skill -- ARGS",
            argv=[
                "pi",
                "--model",
                "openai-codex/gpt-6-luna",
                "--skill",
                str(skill),
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
                "pi --model openai-codex/gpt-6.1-sol --tools read,grep,find,ls,web_search,fetch_content,get_search_content,source_check --no-skills --no-mcp -p"
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
                "pi --model openai-codex/gpt-6.1-sol --tools grep,find,ls --no-skills --no-mcp -p",
            ),
        ):
            with self.subTest(command=command):
                self.assertEqual(launch(command).argv, shlex.split(argv))

    def test_pi_web_needs_package(self) -> None:
        """Refuse web tools on pi when its settings do not install the package providing them."""
        settings = self.home / ".pi/agent/settings.json"
        settings.unlink()
        with self.assertRaises(SystemExit) as raised:
            launch("--preset read-only --tools +web --model gpt-6.1-sol")
        self.assertEqual(
            str(raised.exception),
            "No harness can satisfy the request:\n"
            "  hax: takes no tool list but shell,read,write,edit, with or without subagent\n"
            f"  pi: has no web tool without the npm:pi-web-access package in {settings}",
        )

    def test_pi_lacking_tool(self) -> None:
        """Refuse adding a tool pi lacks, and ignore its removal."""
        self.assertEqual(
            launch("--harness pi --model gpt-6.1-sol --tools -subagent").argv,
            shlex.split("pi --model openai-codex/gpt-6.1-sol --no-mcp -p"),
        )
        with self.assertRaises(SystemExit) as raised:
            launch("--harness pi --model gpt-6.1-sol --tools +subagent")
        self.assertEqual(
            str(raised.exception),
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

    def test_fallback_skill_dir(self) -> None:
        """Look a skill up in the shared skill directory when no harness directory has it."""
        skill = self.home / ".config/agents/skills/fallback-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# fallback-skill\n")
        self.assertEqual(
            launch("--preset lean --skills fallback-skill --model gpt-6.1-sol").argv,
            [
                "pi",
                "--model",
                "openai-codex/gpt-6.1-sol",
                "--tools",
                "bash",
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
            with self.assertRaises(SystemExit) as raised:
                launch("--model opus --auth work")
        self.assertEqual(
            str(raised.exception),
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
        """Reject malformed tool and skill selections."""
        for option, message in (
            ("--skills --no-tools", "argument --skills: expected one argument"),
            ("--skills ''", "Empty name in --skills"),
            (
                "--skills review-correctness,+other",
                "--skills takes names or +/- adjustments, not both",
            ),
            ("--tools unknown", "Unknown tools: unknown"),
        ):
            with self.subTest(option=option):
                stderr = io.StringIO()
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(stderr):
                    agent_worker.parse_request(shlex.split(f"--model opus {option}"))
                self.assertIn(message, stderr.getvalue())

    def test_adjustment_precedence(self) -> None:
        """Let removals win within an adjustment of a selection, and a later adjustment undo an earlier one."""
        for options, expected in (
            (
                "--tools +grep --tools -grep",
                agent_worker.Selection(None, removed=frozenset({"grep"})),
            ),
            (
                "--tools -grep --tools +grep",
                agent_worker.Selection(None, added=frozenset({"grep"})),
            ),
            (
                "--tools +grep,-grep",
                agent_worker.Selection(None, removed=frozenset({"grep"})),
            ),
            (
                "--tools read --tools +read,-read",
                agent_worker.Selection(frozenset()),
            ),
        ):
            with self.subTest(options=options):
                request = agent_worker.parse_request(
                    shlex.split(f"--model opus {options}")
                )
                self.assertEqual(request.tools, expected)

    def test_unknown_model_rejected(self) -> None:
        """Refuse a model of no known family."""
        with self.assertRaises(SystemExit) as raised:
            launch("--model unsupported-model")
        self.assertEqual(
            str(raised.exception), "Unknown model family: unsupported-model"
        )

    def test_incompatible_harness_refused(self) -> None:
        """Refuse a forced harness that does not run the model."""
        with self.assertRaises(SystemExit) as raised:
            launch("--model gpt-6-luna --harness claude")
        self.assertEqual(
            str(raised.exception), "Harness claude does not run gpt-6-luna"
        )

    def test_unsatisfiable_request_names_every_refusal(self) -> None:
        """Fail with each candidate harness's reason when none can satisfy the request."""
        with self.assertRaises(SystemExit) as raised:
            launch("--preset lean --max-turns 3 --model gpt-6.1-sol")
        self.assertEqual(
            str(raised.exception),
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

    def test_shell_overlap_warned(self) -> None:
        """Warn about the requested tools the shell already covers."""
        result = self.run_worker("--model opus --preset lean --tools +read,+edit,+grep")
        self.assertEqual(
            (result.returncode, result.stderr),
            (
                0,
                "Warning: the shell already covers grep, read, and models are better trained to do it through the shell\n",
            ),
        )

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
