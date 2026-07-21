"""Start real bubblewrap sandboxes to exercise the sandbox-coding-agent launcher."""

import collections.abc
import dataclasses
import grp
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Self

from sandbox_probe import Op, OpKind

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "sandbox-coding-agent"
PROBE_SOURCE = Path(__file__).resolve().parent / "sandbox_probe.py"
INTEGRATION_ENV_VAR = "SANDBOX_CODING_AGENT_INTEGRATION"
INTEGRATION_ENABLED = os.environ.get(INTEGRATION_ENV_VAR) is not None
SKIP_REASON = f"Set {INTEGRATION_ENV_VAR} to start real sandboxes for these tests"
LAUNCH_TIMEOUT_SECONDS = 60.0
BASE_INSTRUCTIONS = "# itest base instructions\n"
AGENT_INSTRUCTIONS = "# itest agent instructions\n"
EXTRA_AGENT_INSTRUCTIONS = "# itest extra agent instructions\n"
TEST_TERM = "xterm-itest"
# the group names the launcher copies from the host (its GROUPS constant)
COPIED_GROUPS = ("kvm", "nobody", "nogroup")
EXECUTABLE_STUB = "#!/bin/sh\nexit 0\n"
# real jj, skipping the read-only wrapper an enclosing agent sandbox puts on PATH
JJ_BIN = shutil.which(
    "jj",
    path=os.pathsep.join(entry for entry in os.get_exec_path() if entry != "/run/bin"),
)
SYS_CPU_ONLINE = Path("/sys/devices/system/cpu/online")


@dataclasses.dataclass(frozen=True, slots=True)
class SandboxFixture:
    """Synthetic host directory tree backing one launcher run."""

    root: Path
    home: Path
    config_home: Path
    cache_home: Path
    data_home: Path
    state_home: Path
    runtime_dir: Path
    project_dir: Path
    probe_dir: Path
    tools_dir: Path

    @classmethod
    def create(cls, root: Path) -> Self:
        """Build the fixture paths rooted at root."""
        return cls(
            root=root,
            home=root / "home",
            config_home=root / "config",
            cache_home=root / "cache",
            data_home=root / "data",
            state_home=root / "state",
            runtime_dir=root / "runtime",
            project_dir=root / "project",
            probe_dir=root / "probe-bin",
            tools_dir=root / "tools",
        )

    def env(self) -> dict[str, str]:
        """Return the base launcher environment for this fixture."""
        return {
            "HOME": str(self.home),
            "XDG_CACHE_HOME": str(self.cache_home),
            "XDG_CONFIG_HOME": str(self.config_home),
            "XDG_DATA_HOME": str(self.data_home),
            "XDG_RUNTIME_DIR": str(self.runtime_dir),
            "XDG_STATE_HOME": str(self.state_home),
            "PATH": str(self.tools_dir),
            "EDITOR": "true",
            "TERM": TEST_TERM,
        }

    def exchange_dir(self) -> Path:
        """Return the exchange directory path the launcher derives for the project."""
        name = "-".join(p.lower() for p in (self.root.name, "project"))
        return self.runtime_dir / "agent" / name

    def sandbox_path(self) -> str:
        """Return the PATH value the launcher sets inside the sandbox."""
        return ":".join(
            (
                "/run/bin",
                str(self.home / ".local/libexec/agents"),
                str(self.home / ".cargo/bin"),
                str(self.home / ".local/bin"),
                "/usr/bin",
            )
        )


@unittest.skipUnless(INTEGRATION_ENABLED, SKIP_REASON)
class SandboxTestCase(unittest.TestCase):
    """Drive real launcher runs against synthetic host environments."""

    def setUp(self) -> None:
        """Create a synthetic home, XDG tree, and project for one launcher run."""
        # rooted outside /tmp so the launcher keeps the exchange directory
        # (its CWD-under-/tmp check); the runtime dir is writable even when
        # home is not.
        root = Path(
            tempfile.mkdtemp(
                prefix="sandbox-coding-agent-itest-",
                dir=os.environ.get("XDG_RUNTIME_DIR") or Path.home(),
            )
        ).resolve()
        assert not root.is_relative_to("/tmp"), (
            f"integration fixture must live outside /tmp, got {root}"
        )
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        self.fixture = SandboxFixture.create(root)
        for directory in (
            self.fixture.home,
            self.fixture.config_home,
            self.fixture.cache_home,
            self.fixture.data_home,
            self.fixture.state_home,
            self.fixture.runtime_dir,
            self.fixture.project_dir,
            self.fixture.probe_dir,
            self.fixture.tools_dir,
        ):
            directory.mkdir()
        (self.fixture.config_home / "agents").mkdir()
        (self.fixture.config_home / "agents/AGENTS.md").write_text(BASE_INSTRUCTIONS)
        (self.fixture.config_home / "claude").mkdir()
        (self.fixture.config_home / "claude/claude.json").write_text("{}")
        git = shutil.which("git")
        assert git is not None
        (self.fixture.tools_dir / "git").symlink_to(git)

    def make_project_jj_repo(self) -> None:
        """Turn the fixture project into a jj repository, with jj reachable from the launcher."""
        assert JJ_BIN is not None
        (self.fixture.tools_dir / "jj").symlink_to(JJ_BIN)
        subprocess.run(
            [JJ_BIN, "git", "init", str(self.fixture.project_dir)],
            env=self.fixture.env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

    def run_launcher(
        self,
        ops: collections.abc.Sequence[Op] = (),
        *,
        agent: str,
        extra_env: dict[str, str] | None = None,
        cwd: Path | None = None,
        exit_code: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        """Install the probe as agent and run the launcher with a JSON plan."""
        probe = self.fixture.probe_dir / agent
        shutil.copy(PROBE_SOURCE, probe)
        probe.chmod(0o755)
        plan = json.dumps(
            {
                "ops": [
                    {"label": op.label, "kind": op.kind.name, "arg": str(op.arg)}
                    for op in ops
                ],
                "exit_code": exit_code,
            }
        )
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), str(probe), plan],
            cwd=cwd or self.fixture.project_dir,
            env=self.fixture.env() | (extra_env or {}),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=LAUNCH_TIMEOUT_SECONDS,
        )

    def run_probe(
        self,
        ops: collections.abc.Sequence[Op],
        *,
        agent: str,
        extra_env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> dict[str, object]:
        """Run the launcher and return the probe's JSON report."""
        result = self.run_launcher(ops, agent=agent, extra_env=extra_env, cwd=cwd)
        if result.returncode != 0:
            raise RuntimeError(
                f"Launcher failed ({result.returncode}): {result.stderr}"
            )
        report = json.loads(result.stdout)
        assert isinstance(report, dict)
        return report

    def report_str(self, report: dict[str, object], key: str) -> str:
        """Return a probe report entry known to be a string."""
        value = report[key]
        assert isinstance(value, str)
        return value


class FilesystemTests(SandboxTestCase):
    """Test the filesystem view inside the sandbox."""

    def test_home_is_tmpfs(self) -> None:
        """Hide host home content and discard writes to the sandbox home."""
        (self.fixture.home / "planted.txt").write_text("host secret")

        report = self.run_probe(
            [
                Op("planted_visible", OpKind.EXISTS, self.fixture.home / "planted.txt"),
                Op("write", OpKind.WRITE, self.fixture.home / "from-sandbox.txt"),
            ],
            agent="claude",
        )

        self.assertEqual(report["planted_visible"], False)
        self.assertEqual(report["write"], "ok")
        self.assertEqual(
            sorted(p.name for p in self.fixture.home.iterdir()), ["planted.txt"]
        )

    def test_tmp_is_empty_and_symlinks_exist(self) -> None:
        """Provide an empty /tmp and the usrmerge and /var/tmp symlinks."""
        report = self.run_probe(
            [
                Op("tmp_entries", OpKind.LISTDIR, "/tmp"),
                Op("bin_link", OpKind.READLINK, "/bin"),
                Op("lib_link", OpKind.READLINK, "/lib"),
                Op("var_tmp_link", OpKind.READLINK, "/var/tmp"),
            ],
            agent="claude",
        )

        self.assertEqual(report["tmp_entries"], [])
        self.assertEqual(report["bin_link"], "/usr/bin")
        self.assertEqual(report["lib_link"], "/usr/lib")
        self.assertEqual(report["var_tmp_link"], "/tmp")

    def test_usr_is_read_only(self) -> None:
        """Reject writes under /usr."""
        report = self.run_probe(
            [Op("write", OpKind.WRITE, "/usr/itest-canary")], agent="claude"
        )

        self.assertEqual(report["write"], "EROFS")

    def test_project_dir_is_read_write(self) -> None:
        """Start in the project directory with host content and write-through."""
        (self.fixture.project_dir / "input.txt").write_text("hello from host")

        report = self.run_probe(
            [
                Op("cwd", OpKind.CWD),
                Op("input", OpKind.READ, self.fixture.project_dir / "input.txt"),
                Op("write", OpKind.WRITE, self.fixture.project_dir / "output.txt"),
            ],
            agent="claude",
        )

        self.assertEqual(report["cwd"], str(self.fixture.project_dir))
        self.assertEqual(report["input"], "hello from host")
        self.assertEqual(report["write"], "ok")
        self.assertEqual(
            (self.fixture.project_dir / "output.txt").read_text(), "canary"
        )

    def test_project_git_dir_is_read_only(self) -> None:
        """Expose the project's .git read-only while the rest stays writable."""
        (self.fixture.project_dir / ".git").mkdir()
        (self.fixture.project_dir / ".git/marker").write_text("git data")

        report = self.run_probe(
            [
                Op("marker", OpKind.READ, self.fixture.project_dir / ".git/marker"),
                Op(
                    "git_write", OpKind.WRITE, self.fixture.project_dir / ".git/blocked"
                ),
                Op("project_write", OpKind.WRITE, self.fixture.project_dir / "ok.txt"),
            ],
            agent="claude",
        )

        self.assertEqual(report["marker"], "git data")
        self.assertEqual(report["git_write"], "EROFS")
        self.assertEqual(report["project_write"], "ok")

    def test_overlayfs_discards_writes(self) -> None:
        """Inherit overlay source content but keep writes out of the host."""
        cargo_home = self.fixture.root / "cargo"
        (cargo_home / "bin").mkdir(parents=True)
        (cargo_home / "bin/marker.txt").write_text("cargo marker")
        venv = self.fixture.project_dir / ".venv"
        venv.mkdir()
        (venv / "marker.txt").write_text("venv marker")

        report = self.run_probe(
            [
                Op(
                    "cargo_marker",
                    OpKind.READ,
                    self.fixture.home / ".cargo/bin/marker.txt",
                ),
                Op("cargo_write", OpKind.WRITE, self.fixture.home / ".cargo/added.txt"),
                Op("venv_marker", OpKind.READ, venv / "marker.txt"),
                Op("venv_write", OpKind.WRITE, venv / "added.txt"),
            ],
            agent="claude",
            extra_env={"CARGO_HOME": str(cargo_home)},
        )

        self.assertEqual(report["cargo_marker"], "cargo marker")
        self.assertEqual(report["cargo_write"], "ok")
        self.assertEqual(report["venv_marker"], "venv marker")
        self.assertEqual(report["venv_write"], "ok")
        self.assertFalse((cargo_home / "added.txt").exists())
        self.assertFalse((venv / "added.txt").exists())

    def test_cargo_target_dir_is_shared_overlay(self) -> None:
        """Expose the host Cargo target directory but keep writes out of it."""
        (self.fixture.project_dir / "Cargo.toml").write_text('[package]\nname = "x"\n')
        target = self.fixture.project_dir / "target"
        target.mkdir()
        (target / "artifact.txt").write_text("host build")

        report = self.run_probe(
            [
                Op("target_entries", OpKind.LISTDIR, target),
                Op("artifact", OpKind.READ, target / "artifact.txt"),
                Op("write", OpKind.WRITE, target / "built.txt"),
            ],
            agent="claude",
        )

        self.assertEqual(report["target_entries"], ["artifact.txt"])
        self.assertEqual(report["artifact"], "host build")
        self.assertEqual(report["write"], "ok")
        self.assertEqual([p.name for p in target.iterdir()], ["artifact.txt"])

    def test_cargo_target_dir_created_when_absent(self) -> None:
        """Create the target directory and overlay it when the host lacks one."""
        (self.fixture.project_dir / "Cargo.toml").write_text('[package]\nname = "x"\n')
        target = self.fixture.project_dir / "target"

        report = self.run_probe(
            [Op("write", OpKind.WRITE, target / "built.txt")],
            agent="claude",
        )

        self.assertEqual(report["write"], "ok")
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])

    def test_dev_kvm_is_exposed(self) -> None:
        """Expose /dev/kvm as a character device."""
        report = self.run_probe(
            [Op("kvm_is_char_device", OpKind.IS_CHAR_DEVICE, "/dev/kvm")],
            agent="claude",
        )

        self.assertEqual(report["kvm_is_char_device"], True)


class GeneratedFileTests(SandboxTestCase):
    """Test the files the launcher synthesizes inside the sandbox."""

    def test_etc_passwd(self) -> None:
        """Generate a stripped /etc/passwd with the current user forced to bash."""
        report = self.run_probe(
            [
                Op("passwd", OpKind.READ, "/etc/passwd"),
                Op("mode", OpKind.MODE, "/etc/passwd"),
            ],
            agent="claude",
        )

        entry = pwd.getpwuid(os.getuid())
        expected_first = ":".join(
            [
                entry.pw_name,
                entry.pw_passwd,
                str(entry.pw_uid),
                str(entry.pw_gid),
                entry.pw_gecos,
                entry.pw_dir,
                "/usr/bin/bash",
            ]
        )
        expected_names = [entry.pw_name]
        for name in COPIED_GROUPS:
            try:
                expected_names.append(pwd.getpwnam(name).pw_name)
            except KeyError:
                continue
        lines = self.report_str(report, "passwd").splitlines()
        self.assertEqual(lines[0], expected_first)
        self.assertEqual([line.split(":")[0] for line in lines], expected_names)
        self.assertEqual(report["mode"], "0o600")

    def test_etc_group(self) -> None:
        """Generate a stripped /etc/group led by the current primary group."""
        report = self.run_probe(
            [
                Op("group", OpKind.READ, "/etc/group"),
                Op("mode", OpKind.MODE, "/etc/group"),
            ],
            agent="claude",
        )

        entry = grp.getgrgid(os.getgid())
        expected_first = ":".join(
            [
                entry.gr_name,
                entry.gr_passwd or "",
                str(entry.gr_gid),
                ",".join(entry.gr_mem),
            ]
        )
        lines = self.report_str(report, "group").splitlines()
        self.assertEqual(lines[0], expected_first)
        self.assertEqual(report["mode"], "0o600")

    def test_etc_nsswitch(self) -> None:
        """Generate an nsswitch.conf resolving users from files and hosts via DNS."""
        report = self.run_probe(
            [Op("nsswitch", OpKind.READ, "/etc/nsswitch.conf")], agent="claude"
        )

        self.assertEqual(
            report["nsswitch"], "passwd: files\ngroup: files\nhosts: files dns\n"
        )

    @unittest.skipUnless(JJ_BIN is not None, "jj is not installed")
    def test_jj_wrapper(self) -> None:
        """Provision the jj wrapper and advertise it, with the real binary, inside a jj repository."""
        self.make_project_jj_repo()

        report = self.run_probe(
            [
                Op("mode", OpKind.MODE, "/run/bin/jj"),
                Op(
                    "instructions",
                    OpKind.READ,
                    self.fixture.home / ".claude/CLAUDE.md",
                ),
            ],
            agent="claude",
        )

        self.assertEqual(report["mode"], "0o700")
        instructions = self.report_str(report, "instructions")
        self.assertIn("use it for read-only queries in this repository", instructions)
        self.assertIn(f"`{self.fixture.tools_dir / 'jj'}`", instructions)

    @unittest.skipUnless(JJ_BIN is not None, "jj is not installed")
    def test_no_jj_hint_outside_jj_repo(self) -> None:
        """Omit the jj hint outside a jj repository even when jj is available."""
        assert JJ_BIN is not None
        (self.fixture.tools_dir / "jj").symlink_to(JJ_BIN)

        report = self.run_probe(
            [Op("instructions", OpKind.READ, self.fixture.home / ".claude/CLAUDE.md")],
            agent="claude",
        )

        self.assertNotIn(
            "wrapper adding `--ignore-working-copy`",
            self.report_str(report, "instructions"),
        )

    @unittest.skipUnless(
        SYS_CPU_ONLINE.is_file(), "host sysfs CPU topology is unavailable"
    )
    def test_cpu_topology(self) -> None:
        """Share the host CPU counts and the guest vCPU pin map for the microvm launcher."""
        report = self.run_probe(
            [
                Op("topology", OpKind.READ, "/run/sandbox-coding-agent/cpu-topology"),
                Op("mode", OpKind.MODE, "/run/sandbox-coding-agent/cpu-topology"),
            ],
            agent="claude",
        )

        counts_line, pin_line = self.report_str(report, "topology").splitlines()
        counts = re.fullmatch(
            r"sockets=([1-9]\d*),cores=([1-9]\d*),threads=([1-9]\d*)", counts_line
        )
        assert counts is not None
        self.assertRegex(pin_line, r"^pin=\d+(,\d+)*$")
        pin = pin_line.removeprefix("pin=").split(",")
        sockets, cores, threads = (int(group) for group in counts.groups())
        self.assertEqual(len(set(pin)), sockets * cores * threads)
        self.assertEqual(report["mode"], "0o600")


class EnvironmentTests(SandboxTestCase):
    """Test environment clearing, setting, and forwarding."""

    def test_env_set_cleared_and_forwarded(self) -> None:
        """Clear host env, set the sandbox env, and forward the allowed vars."""
        report = self.run_probe(
            [
                Op("PS1", OpKind.ENV, "PS1"),
                Op("UV_LINK_MODE", OpKind.ENV, "UV_LINK_MODE"),
                Op("PATH", OpKind.ENV, "PATH"),
                Op("TERM", OpKind.ENV, "TERM"),
                Op("EDITOR", OpKind.ENV, "EDITOR"),
                Op("HOME", OpKind.ENV, "HOME"),
                Op("CLAUDE_VISIBLE", OpKind.ENV, "CLAUDE_VISIBLE"),
                Op("secret", OpKind.ENV, "SECRET_CANARY"),
                Op("gh_token", OpKind.ENV, "GH_TOKEN"),
            ],
            agent="claude",
            extra_env={"SECRET_CANARY": "leak", "CLAUDE_VISIBLE": "yes"},
        )

        self.assertEqual(report["PS1"], "claude-sandbox$ ")
        self.assertEqual(report["UV_LINK_MODE"], "copy")
        self.assertEqual(report["PATH"], self.fixture.sandbox_path())
        self.assertEqual(report["TERM"], TEST_TERM)
        self.assertEqual(report["EDITOR"], "true")
        self.assertEqual(report["HOME"], str(self.fixture.home))
        self.assertEqual(report["CLAUDE_VISIBLE"], "yes")
        self.assertIsNone(report["secret"])
        self.assertIsNone(report["gh_token"])

    def test_agent_prefix_forwarding_follows_agent_name(self) -> None:
        """Forward only the env vars prefixed with the launched agent's name."""
        (self.fixture.config_home / "codex").mkdir()
        (self.fixture.config_home / "codex/config.toml").write_text('model = "test"\n')

        report = self.run_probe(
            [
                Op("ps1", OpKind.ENV, "PS1"),
                Op("codex_visible", OpKind.ENV, "CODEX_VISIBLE"),
                Op("claude_visible", OpKind.ENV, "CLAUDE_VISIBLE"),
            ],
            agent="codex",
            extra_env={"CODEX_VISIBLE": "yes", "CLAUDE_VISIBLE": "yes"},
        )

        self.assertEqual(report["ps1"], "codex-sandbox$ ")
        self.assertEqual(report["codex_visible"], "yes")
        self.assertIsNone(report["claude_visible"])

    def test_proxy_routing_when_ca_bundle_present(self) -> None:
        """Route gh through the auth proxy when its CA bundle exists."""
        bundle = self.fixture.runtime_dir / "agent-proxy/ca-bundle.crt"
        bundle.parent.mkdir()
        bundle.write_text("FAKE CA")

        report = self.run_probe(
            [
                Op("GH_TOKEN", OpKind.ENV, "GH_TOKEN"),
                Op("HTTPS_PROXY", OpKind.ENV, "HTTPS_PROXY"),
                Op("NO_PROXY", OpKind.ENV, "NO_PROXY"),
                Op("SSL_CERT_FILE", OpKind.ENV, "SSL_CERT_FILE"),
                Op("bundle", OpKind.READ, bundle),
                Op("bundle_write", OpKind.WRITE, bundle),
                Op("claude_md", OpKind.READ, self.fixture.home / ".claude/CLAUDE.md"),
            ],
            agent="claude",
        )

        self.assertEqual(report["GH_TOKEN"], "agent-proxy-placeholder")
        self.assertEqual(report["HTTPS_PROXY"], "http://127.0.0.1:8085")
        self.assertEqual(report["NO_PROXY"], "127.0.0.1,localhost")
        self.assertEqual(report["SSL_CERT_FILE"], str(bundle))
        self.assertEqual(report["bundle"], "FAKE CA")
        self.assertEqual(report["bundle_write"], "EROFS")
        self.assertIn(
            "`gh` is available and authenticated",
            self.report_str(report, "claude_md"),
        )


class InstructionsTests(SandboxTestCase):
    """Test the dynamically generated global instructions file."""

    def test_claude_instructions_content(self) -> None:
        """Generate CLAUDE.md from the base file plus the live mount layout."""
        report = self.run_probe(
            [
                Op("claude_md", OpKind.READ, self.fixture.home / ".claude/CLAUDE.md"),
                Op("mode", OpKind.MODE, self.fixture.home / ".claude/CLAUDE.md"),
            ],
            agent="claude",
        )

        content = self.report_str(report, "claude_md")
        self.assertTrue(
            content.startswith(
                f"{BASE_INSTRUCTIONS.strip()}\n\n## Sandbox environment\n"
            )
        )
        self.assertIn("You are running in a `bwrap` based sandbox.", content)
        lines = content.splitlines()
        tmpfs_line = next(line for line in lines if "tmpfs filesystems" in line)
        self.assertIn(f"`{self.fixture.home}`", tmpfs_line)
        self.assertIn(f"`{self.fixture.runtime_dir}`", tmpfs_line)
        ro_line = next(line for line in lines if "read-only bind mounts" in line)
        self.assertIn("`/usr`", ro_line)
        rw_line = next(line for line in lines if "normal bind mounts" in line)
        self.assertIn(f"`{self.fixture.project_dir}`", rw_line)
        self.assertIn(f"`{self.fixture.exchange_dir()}`", rw_line)
        self.assertIn(f"place them under `{self.fixture.exchange_dir()}`", content)
        self.assertNotIn("overlayfs filesystems", content)
        self.assertNotIn("xdg-open", content)
        self.assertNotIn("`gh`", content)
        self.assertEqual(report["mode"], "0o600")

    def test_instructions_include_agent_file(self) -> None:
        """Inline the agent's own instructions file between the base and the sandbox section."""
        (self.fixture.config_home / "agents/AGENTS.claude.md").write_text(
            AGENT_INSTRUCTIONS
        )

        report = self.run_probe(
            [Op("claude_md", OpKind.READ, self.fixture.home / ".claude/CLAUDE.md")],
            agent="claude",
        )

        content = self.report_str(report, "claude_md")
        self.assertTrue(
            content.startswith(
                f"{BASE_INSTRUCTIONS.strip()}\n\n{AGENT_INSTRUCTIONS.strip()}\n\n"
                "## Sandbox environment\n"
            )
        )

    def test_instructions_of_extra_agent_use_its_own_file(self) -> None:
        """Give an always-provisioned extra agent its own instructions file, not the launched one's."""
        pi = self.fixture.home / ".local/libexec/pi/pi"
        pi.parent.mkdir(parents=True)
        pi.write_text(EXECUTABLE_STUB)
        pi.chmod(0o755)
        (self.fixture.config_home / "agents/AGENTS.claude.md").write_text(
            AGENT_INSTRUCTIONS
        )
        (self.fixture.config_home / "agents/AGENTS.pi.md").write_text(
            EXTRA_AGENT_INSTRUCTIONS
        )

        report = self.run_probe(
            [
                Op("claude_md", OpKind.READ, self.fixture.home / ".claude/CLAUDE.md"),
                Op("pi_md", OpKind.READ, self.fixture.home / ".pi/agent/AGENTS.md"),
            ],
            agent="claude",
        )

        claude_md = self.report_str(report, "claude_md")
        pi_md = self.report_str(report, "pi_md")
        self.assertIn(AGENT_INSTRUCTIONS.strip(), claude_md)
        self.assertNotIn(EXTRA_AGENT_INSTRUCTIONS.strip(), claude_md)
        self.assertIn(EXTRA_AGENT_INSTRUCTIONS.strip(), pi_md)
        self.assertNotIn(AGENT_INSTRUCTIONS.strip(), pi_md)

    def test_instructions_without_base_file(self) -> None:
        """Generate only the sandbox section when the user has no base file."""
        (self.fixture.config_home / "agents/AGENTS.md").unlink()

        report = self.run_probe(
            [Op("claude_md", OpKind.READ, self.fixture.home / ".claude/CLAUDE.md")],
            agent="claude",
        )

        content = self.report_str(report, "claude_md")
        self.assertTrue(content.startswith("## Sandbox environment\n"))

    def test_unknown_agent_gets_no_instructions(self) -> None:
        """Start an agent without a known instructions path and generate none."""
        report = self.run_probe(
            [
                Op(
                    "claude_md_exists",
                    OpKind.EXISTS,
                    self.fixture.home / ".claude/CLAUDE.md",
                ),
                Op("ps1", OpKind.ENV, "PS1"),
            ],
            agent="myagent",
        )

        self.assertEqual(report["claude_md_exists"], False)
        self.assertEqual(report["ps1"], "myagent-sandbox$ ")

    def test_exchange_dir_round_trips(self) -> None:
        """Create the exchange directory and reflect sandbox writes on the host."""
        exchange = self.fixture.exchange_dir()

        report = self.run_probe(
            [
                Op("is_dir", OpKind.ISDIR, exchange),
                Op("write", OpKind.WRITE, exchange / "note.txt"),
            ],
            agent="claude",
        )

        self.assertEqual(report["is_dir"], True)
        self.assertEqual(report["write"], "ok")
        self.assertEqual((exchange / "note.txt").read_text(), "canary")

    def test_no_exchange_dir_for_project_under_tmp(self) -> None:
        """Skip the exchange directory when the project lives under /tmp."""
        project = Path(tempfile.mkdtemp(dir="/tmp")).resolve()
        self.addCleanup(shutil.rmtree, project, ignore_errors=True)

        report = self.run_probe(
            [
                Op("cwd", OpKind.CWD),
                Op("runtime_entries", OpKind.LISTDIR, self.fixture.runtime_dir),
                Op("claude_md", OpKind.READ, self.fixture.home / ".claude/CLAUDE.md"),
            ],
            agent="claude",
            cwd=project,
        )

        self.assertEqual(report["cwd"], str(project))
        self.assertEqual(report["runtime_entries"], [])
        self.assertNotIn("exchange", self.report_str(report, "claude_md"))
        self.assertEqual(list(self.fixture.runtime_dir.iterdir()), [])

    def test_unjail_tools_expose_xdg_open(self) -> None:
        """Expose xdg-open in the sandbox when the unjail tools are on PATH."""
        for name in ("unjail-xdg-open", "unjail"):
            tool = self.fixture.tools_dir / name
            tool.write_text(EXECUTABLE_STUB)
            tool.chmod(0o755)
        unjaild_socket = self.fixture.runtime_dir / "unjaild/xdg-open"
        unjaild_socket.parent.mkdir()
        unjaild_socket.touch()

        report = self.run_probe(
            [
                Op("xdg_open", OpKind.ACCESS_X, "/run/bin/xdg-open"),
                Op("unjail", OpKind.ACCESS_X, "/run/bin/unjail"),
                Op("socket_exists", OpKind.EXISTS, unjaild_socket),
                Op("claude_md", OpKind.READ, self.fixture.home / ".claude/CLAUDE.md"),
            ],
            agent="claude",
        )

        self.assertEqual(report["xdg_open"], True)
        self.assertEqual(report["unjail"], True)
        self.assertEqual(report["socket_exists"], True)
        self.assertIn("`xdg-open` is available", self.report_str(report, "claude_md"))


class AgentSpecificTests(SandboxTestCase):
    """Test per-agent mounts, generated configs, and symlinks."""

    def test_claude_config_round_trips(self) -> None:
        """Expose the claude config read-write at its sandbox locations."""
        report = self.run_probe(
            [
                Op("claude_json", OpKind.READ, self.fixture.home / ".claude.json"),
                Op("write", OpKind.WRITE, self.fixture.home / ".claude/state.txt"),
            ],
            agent="claude",
        )

        self.assertEqual(report["claude_json"], "{}")
        self.assertEqual(report["write"], "ok")
        self.assertEqual(
            (self.fixture.config_home / "claude/state.txt").read_text(), "canary"
        )

    def test_claude_md_symlink_created_for_agents_md(self) -> None:
        """Symlink CLAUDE.md to an existing AGENTS.md in the project."""
        (self.fixture.project_dir / "AGENTS.md").write_text("project instructions")

        report = self.run_probe(
            [
                Op("is_link", OpKind.ISLINK, self.fixture.project_dir / "CLAUDE.md"),
                Op("target", OpKind.READLINK, self.fixture.project_dir / "CLAUDE.md"),
                Op("content", OpKind.READ, self.fixture.project_dir / "CLAUDE.md"),
            ],
            agent="claude",
        )

        self.assertEqual(report["is_link"], True)
        self.assertEqual(report["target"], str(self.fixture.project_dir / "AGENTS.md"))
        self.assertEqual(report["content"], "project instructions")
        # the symlink is created on the bind-mounted project, so it persists
        self.assertTrue((self.fixture.project_dir / "CLAUDE.md").is_symlink())

    def test_no_claude_md_symlink_without_agents_md(self) -> None:
        """Create no CLAUDE.md symlink when the project has no AGENTS.md."""
        report = self.run_probe(
            [
                Op(
                    "claude_md_exists",
                    OpKind.LEXISTS,
                    self.fixture.project_dir / "CLAUDE.md",
                )
            ],
            agent="claude",
        )

        self.assertEqual(report["claude_md_exists"], False)

    def test_codex_trusts_launch_dir_keeping_user_config_writable(self) -> None:
        """Trust the launch directory from the system layer, leaving the user config writable."""
        (self.fixture.config_home / "codex").mkdir()
        (self.fixture.config_home / "codex/config.toml").write_text('model = "test"\n')

        report = self.run_probe(
            [
                Op("config", OpKind.READ, self.fixture.home / ".codex/config.toml"),
                Op(
                    "config_write",
                    OpKind.WRITE,
                    self.fixture.home / ".codex/config.toml",
                ),
                Op("system_config", OpKind.READ, Path("/etc/codex/config.toml")),
                Op(
                    "agents_md_exists",
                    OpKind.EXISTS,
                    self.fixture.home / ".codex/AGENTS.md",
                ),
            ],
            agent="codex",
        )

        self.assertEqual(report["config"], 'model = "test"\n')
        self.assertEqual(report["config_write"], "ok")
        self.assertEqual(
            report["system_config"],
            f'[projects."{self.fixture.project_dir}"]\ntrust_level = "trusted"\n',
        )
        self.assertEqual(report["agents_md_exists"], True)
        self.assertEqual(
            (self.fixture.config_home / "codex/config.toml").read_text(), "canary"
        )

    def test_codex_creates_user_config_on_first_write(self) -> None:
        """Persist codex's user config to the host when it does not exist yet."""
        report = self.run_probe(
            [Op("write", OpKind.WRITE, self.fixture.home / ".codex/config.toml")],
            agent="codex",
        )

        self.assertEqual(report["write"], "ok")
        self.assertEqual(
            (self.fixture.config_home / "codex/config.toml").read_text(), "canary"
        )

    def test_pi_gets_npmrc_and_instructions(self) -> None:
        """Generate the npm prefix config and instructions for pi."""
        report = self.run_probe(
            [
                Op("npmrc", OpKind.READ, self.fixture.home / ".npmrc"),
                Op(
                    "agents_md_exists",
                    OpKind.EXISTS,
                    self.fixture.home / ".pi/agent/AGENTS.md",
                ),
            ],
            agent="pi",
        )

        self.assertEqual(report["npmrc"], f"prefix={self.fixture.home}/.pi/agent/npm\n")
        self.assertEqual(report["agents_md_exists"], True)

    def test_amp_dirs_round_trip(self) -> None:
        """Expose the amp state and config dirs read-write at their locations."""
        report = self.run_probe(
            [
                Op("state_write", OpKind.WRITE, self.fixture.home / ".amp/state.txt"),
                Op(
                    "config_write",
                    OpKind.WRITE,
                    self.fixture.home / ".config/amp/conf.txt",
                ),
                Op(
                    "agents_md",
                    OpKind.READ,
                    self.fixture.home / ".config/amp/AGENTS.md",
                ),
            ],
            agent="amp",
        )

        self.assertEqual(report["state_write"], "ok")
        self.assertEqual(report["config_write"], "ok")
        self.assertIn("## Sandbox environment", self.report_str(report, "agents_md"))
        self.assertEqual(
            (self.fixture.cache_home / "amp/state.txt").read_text(), "canary"
        )
        self.assertEqual(
            (self.fixture.config_home / "amp/conf.txt").read_text(), "canary"
        )
        # the generated content lives in a memfd; only the empty file bwrap
        # created as mount point leaks to the bind-mounted host config dir
        self.assertEqual((self.fixture.config_home / "amp/AGENTS.md").read_text(), "")

    def test_launch_inside_skill_dir_stays_writable(self) -> None:
        """Keep the launch dir writable when it is the agent's read-only skill dir."""
        (self.fixture.config_home / "agents/skills").mkdir(parents=True)
        cwd = self.fixture.home / ".config/agents/skills"
        cwd.mkdir(parents=True)

        report = self.run_probe(
            [Op("write", OpKind.WRITE, cwd / "new-skill.txt")],
            agent="amp",
            cwd=cwd,
        )

        self.assertEqual(report["write"], "ok")
        self.assertEqual((cwd / "new-skill.txt").read_text(), "canary")

    def test_first_launch_creates_missing_agent_dirs(self) -> None:
        """Create missing read-write mount sources on the host at first launch."""
        shutil.rmtree(self.fixture.config_home / "claude")

        self.assertEqual(self.run_launcher(agent="claude").returncode, 0)

        self.assertTrue((self.fixture.config_home / "claude").is_dir())
        self.assertTrue((self.fixture.config_home / "claude/claude.json").exists())
        self.assertTrue((self.fixture.cache_home / "agent-microvm").is_dir())


class ScratchModeTests(SandboxTestCase):
    """Test the throwaway sandbox built for a launch directory under /tmp."""

    def setUp(self) -> None:
        """Add a project under /tmp and a host session history for the claude agent."""
        super().setUp()
        self.tmp_project = Path(tempfile.mkdtemp(dir="/tmp")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp_project, ignore_errors=True)
        self.host_sessions = self.fixture.config_home / "claude/projects"
        self.sandbox_sessions = self.fixture.home / ".claude/projects"

    def session_dirs(self) -> dict[str, tuple[Path, Path]]:
        """Map each supported agent to its host and sandbox session directories."""
        return {
            "amp": (
                self.fixture.data_home / "amp/threads",
                self.fixture.home / ".local/share/amp/threads",
            ),
            "claude": (self.host_sessions, self.sandbox_sessions),
            "codex": (
                self.fixture.config_home / "codex/sessions",
                self.fixture.home / ".codex/sessions",
            ),
            "pi": (
                self.fixture.config_home / "pi/agent/sessions",
                self.fixture.home / ".pi/agent/sessions",
            ),
        }

    def plant_host_session(self, host_dir: Path) -> None:
        """Write a session history entry in the host directory the agent reads from."""
        # an earlier launch may have left the dir behind as an empty tmpfs mount point
        host_dir.mkdir(parents=True, exist_ok=True)
        (host_dir / "old.jsonl").write_text("host session")

    def session_ops(self, sandbox_dir: Path) -> list[Op]:
        """Return the ops listing the agent's session dir and writing an entry to it."""
        return [
            Op("entries", OpKind.LISTDIR, sandbox_dir),
            Op("write", OpKind.WRITE, sandbox_dir / "new.jsonl"),
        ]

    def test_session_dir_hides_host_history_and_discards_writes(self) -> None:
        """Empty the agent's session dir and keep its writes off the host."""
        for agent, (host_dir, sandbox_dir) in self.session_dirs().items():
            with self.subTest(agent=agent):
                self.plant_host_session(host_dir)

                report = self.run_probe(
                    self.session_ops(sandbox_dir), agent=agent, cwd=self.tmp_project
                )

                self.assertEqual(report["entries"], [])
                self.assertEqual(report["write"], "ok")
                self.assertEqual([p.name for p in host_dir.iterdir()], ["old.jsonl"])

    def test_session_dir_mounted_when_absent_from_host(self) -> None:
        """Mount the session tmpfs even when the host has no session dir yet."""
        report = self.run_probe(
            self.session_ops(self.sandbox_sessions),
            agent="claude",
            cwd=self.tmp_project,
        )

        self.assertEqual(report["entries"], [])
        self.assertEqual(report["write"], "ok")
        # bwrap creates the mount point in the bind-mounted host config dir; it stays
        # empty, as the sandbox writes land on the tmpfs mounted over it
        self.assertTrue(self.host_sessions.is_dir())
        self.assertEqual(list(self.host_sessions.iterdir()), [])

    def test_extra_agent_session_dir_isolated(self) -> None:
        """Hide the session history of an always-provisioned extra agent too."""
        pi = self.fixture.home / ".local/libexec/pi/pi"
        pi.parent.mkdir(parents=True)
        pi.write_text(EXECUTABLE_STUB)
        pi.chmod(0o755)
        host_dir, sandbox_dir = self.session_dirs()["pi"]
        self.plant_host_session(host_dir)

        report = self.run_probe(
            self.session_ops(sandbox_dir), agent="claude", cwd=self.tmp_project
        )

        self.assertEqual(report["entries"], [])
        self.assertEqual(report["write"], "ok")
        self.assertEqual([p.name for p in host_dir.iterdir()], ["old.jsonl"])

    def test_session_dir_advertised_as_tmpfs(self) -> None:
        """List the session dir among the tmpfs mounts of the generated instructions."""
        report = self.run_probe(
            [Op("claude_md", OpKind.READ, self.fixture.home / ".claude/CLAUDE.md")],
            agent="claude",
            cwd=self.tmp_project,
        )

        lines = self.report_str(report, "claude_md").splitlines()
        tmpfs_line = next(line for line in lines if "tmpfs filesystems" in line)
        self.assertIn(f"`{self.sandbox_sessions}`", tmpfs_line)

    def test_project_dir_stays_host_backed(self) -> None:
        """Keep the launch dir readable and its writes on the host under the /tmp tmpfs."""
        (self.tmp_project / "input.txt").write_text("host input")

        report = self.run_probe(
            [
                Op("input", OpKind.READ, self.tmp_project / "input.txt"),
                Op("write", OpKind.WRITE, self.tmp_project / "output.txt"),
            ],
            agent="claude",
            cwd=self.tmp_project,
        )

        self.assertEqual(report["input"], "host input")
        self.assertEqual(report["write"], "ok")
        self.assertEqual((self.tmp_project / "output.txt").read_text(), "canary")

    def test_session_dir_persists_outside_tmp(self) -> None:
        """Keep the session dir bound to the host for a project outside /tmp."""
        self.plant_host_session(self.host_sessions)

        report = self.run_probe(self.session_ops(self.sandbox_sessions), agent="claude")

        self.assertEqual(report["entries"], ["old.jsonl"])
        self.assertEqual(report["write"], "ok")
        self.assertEqual(
            sorted(p.name for p in self.host_sessions.iterdir()),
            ["new.jsonl", "old.jsonl"],
        )


class LaunchPolicyTests(SandboxTestCase):
    """Test startup checks and launcher process behavior."""

    def test_refuses_to_start_in_home(self) -> None:
        """Exit with an error when started in the home directory."""
        result = self.run_launcher(agent="claude", cwd=self.fixture.home)

        self.assertEqual(result.returncode, 1)
        self.assertIn("Refusing to start in", result.stderr)

    def test_repo_root_prompt_defaults_to_cwd(self) -> None:
        """Stay in a repo subdirectory when stdin cannot answer the prompt."""
        subprocess.run(
            ["git", "-C", str(self.fixture.project_dir), "init", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        subdir = self.fixture.project_dir / "nested"
        subdir.mkdir()

        report = self.run_probe([Op("cwd", OpKind.CWD)], agent="claude", cwd=subdir)

        self.assertEqual(report["cwd"], str(subdir))

    def test_exit_code_propagates(self) -> None:
        """Propagate the agent's exit status back to the launcher caller."""
        result = self.run_launcher(agent="claude", exit_code=7)

        self.assertEqual(result.returncode, 7)

    def test_home_tools_and_editor_are_mounted(self) -> None:
        """Bind PATH-resolved home tools and an absolute EDITOR into the sandbox."""
        for name in ("uv", "fake-editor"):
            tool = self.fixture.tools_dir / name
            tool.write_text(EXECUTABLE_STUB)
            tool.chmod(0o755)
        editor = self.fixture.tools_dir / "fake-editor"

        report = self.run_probe(
            [
                Op("uv", OpKind.ACCESS_X, self.fixture.tools_dir / "uv"),
                Op("editor", OpKind.ACCESS_X, editor),
            ],
            agent="claude",
            extra_env={"EDITOR": str(editor)},
        )

        self.assertEqual(report["uv"], True)
        self.assertEqual(report["editor"], True)

    def test_debug_mode_prints_bwrap_command(self) -> None:
        """Print the assembled bwrap command on stderr in debug mode."""
        result = self.run_launcher(
            agent="claude", extra_env={"DEBUG_SANDBOX_AGENT": "1"}
        )

        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stderr.startswith("bwrap --unshare-all --share-net"))
        self.assertIn("--clearenv", result.stderr)
        self.assertIn("--die-with-parent", result.stderr)


if __name__ == "__main__":
    unittest.main()
