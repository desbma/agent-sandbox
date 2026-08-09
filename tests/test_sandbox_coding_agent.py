"""Test sandbox-coding-agent helpers without starting bubblewrap."""

import atexit
import collections.abc
import contextlib
import dataclasses
import grp
import importlib.machinery
import importlib.util
import io
import os
import pwd
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
import unittest.mock
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "sandbox-coding-agent"
FIXTURE_ROOT = Path(tempfile.mkdtemp(prefix="sandbox-coding-agent-tests-")).resolve()
FAKE_HOME = FIXTURE_ROOT / "home"
FAKE_CONFIG_HOME = FIXTURE_ROOT / "config"
FAKE_RUNTIME_DIR = FIXTURE_ROOT / "runtime"
IMPORT_CWD = FIXTURE_ROOT / "cwd"


def load_launcher(
    env_overrides: dict[str, str | None] | None = None,
    cwd: Path | None = None,
    agent: str = "claude",
) -> types.ModuleType:
    """Load the launcher script as a module under a controlled environment, directory and agent.

    A None value in env_overrides removes that variable from the environment.
    """
    for directory in (FAKE_HOME, FAKE_CONFIG_HOME, FAKE_RUNTIME_DIR, IMPORT_CWD):
        directory.mkdir(parents=True, exist_ok=True)
    loader = importlib.machinery.SourceFileLoader(
        "sandbox_coding_agent", str(SCRIPT_PATH)
    )
    spec = importlib.util.spec_from_loader("sandbox_coding_agent", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["sandbox_coding_agent"] = module
    saved_argv = sys.argv
    saved_cwd = Path.cwd()
    saved_environ = os.environ.copy()
    env: dict[str, str | None] = {
        "HOME": str(FAKE_HOME),
        "XDG_CACHE_HOME": str(FIXTURE_ROOT / "cache"),
        "XDG_CONFIG_HOME": str(FAKE_CONFIG_HOME),
        "XDG_DATA_HOME": str(FIXTURE_ROOT / "data"),
        "XDG_RUNTIME_DIR": str(FAKE_RUNTIME_DIR),
        "XDG_STATE_HOME": str(FIXTURE_ROOT / "state"),
        "CARGO_HOME": str(FIXTURE_ROOT / "cargo"),
        "RUSTUP_HOME": str(FIXTURE_ROOT / "rustup"),
        "EDITOR": "true",
    }
    env.update(env_overrides or {})
    try:
        for key, value in env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        sys.argv = [str(SCRIPT_PATH), agent]
        os.chdir(cwd if cwd is not None else IMPORT_CWD)
        loader.exec_module(module)
    finally:
        sys.argv = saved_argv
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_environ)
    return module


launcher = load_launcher()
atexit.register(shutil.rmtree, FIXTURE_ROOT, ignore_errors=True)

# sandbox facts with every optional element absent, base for the instructions rendering tests
EMPTY_SANDBOX_FACTS = launcher.SandboxFacts(
    exchange_dir=None,
    dir_mounts={},
    has_unjaild=False,
    has_proxy=False,
    jj_bin=None,
)


class TempDirTestCase(unittest.TestCase):
    """Provide self-cleaning temporary directories and git repositories."""

    def make_temp_dir(self) -> Path:
        """Create a self-cleaning temporary directory."""
        path = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def make_persistent_temp_dir(self) -> Path:
        """Create a self-cleaning temporary directory outside /tmp, where scratch mode is off."""
        path = Path(
            tempfile.mkdtemp(dir=os.environ.get("XDG_RUNTIME_DIR") or Path.home())
        ).resolve()
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def make_executable(self, path: Path) -> Path:
        """Create an executable file at path and return it."""
        path.write_text("")
        path.chmod(0o755)
        return path

    def make_git_repo(self) -> Path:
        """Create a self-cleaning temporary git repository."""
        repo = self.make_temp_dir()
        subprocess.run(
            ["git", "-C", str(repo), "init", "--quiet"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return repo

    def make_jj_repo(self, root: Path) -> Path:
        """Initialize a jj repository at root, creating parents, and return it."""
        assert launcher.JJ_BIN is not None
        root.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [launcher.JJ_BIN, "git", "init", str(root)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return root

    def add_jj_workspace(self, repo: Path, dest: Path) -> Path:
        """Add a jj workspace of repo at dest and return it."""
        assert launcher.JJ_BIN is not None
        subprocess.run(
            [launcher.JJ_BIN, "workspace", "add", str(dest)],
            cwd=str(repo),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return dest


class MountTests(unittest.TestCase):
    """Test the Mount dataclass."""

    def test_target_defaults_to_source(self) -> None:
        """Use the source path as target when no remap is given."""
        mount = launcher.Mount(Path("/src"), launcher.MountKind.BIND_RO)

        self.assertEqual(mount.target, Path("/src"))

    def test_target_uses_remap(self) -> None:
        """Use the remapped destination as target when given."""
        mount = launcher.Mount(Path("/src"), launcher.MountKind.BIND_RO, Path("/dst"))

        self.assertEqual(mount.target, Path("/dst"))

    def test_bwrap_args_bind_ro(self) -> None:
        """Bind the source read-only at the remapped target."""
        mount = launcher.Mount(Path("/src"), launcher.MountKind.BIND_RO, Path("/dst"))

        self.assertEqual(mount.bwrap_args, ["--ro-bind", "/src", "/dst"])

    def test_bwrap_args_bind_rw(self) -> None:
        """Bind the source read-write at its own path when not remapped."""
        mount = launcher.Mount(Path("/src"), launcher.MountKind.BIND_RW)

        self.assertEqual(mount.bwrap_args, ["--bind", "/src", "/src"])

    def test_bwrap_args_tmpfs(self) -> None:
        """Mount a private tmpfs over the target."""
        mount = launcher.Mount(Path("/src"), launcher.MountKind.TMPFS)

        self.assertEqual(mount.bwrap_args, ["--perms", "0700", "--tmpfs", "/src"])

    def test_bwrap_args_overlayfs(self) -> None:
        """Overlay a throwaway upper layer over the source at the target."""
        mount = launcher.Mount(Path("/src"), launcher.MountKind.OVERLAYFS, Path("/dst"))

        self.assertEqual(
            mount.bwrap_args,
            ["--dir", "/dst", "--overlay-src", "/src", "--tmp-overlay", "/dst"],
        )


class MountKindTests(unittest.TestCase):
    """Test the mount kind specifications."""

    def test_source_pre_checks(self) -> None:
        """Check the host source of every kind consuming one, creating it for read-write binds."""
        self.assertEqual(
            {kind: kind.spec.source_pre_check for kind in launcher.MountKind},
            {
                launcher.MountKind.TMPFS: launcher.SourcePreCheck.NONE,
                launcher.MountKind.OVERLAYFS: launcher.SourcePreCheck.DROP_IF_MISSING,
                launcher.MountKind.BIND_RO: launcher.SourcePreCheck.DROP_IF_MISSING,
                launcher.MountKind.BIND_RW: launcher.SourcePreCheck.MKDIR_IF_MISSING,
            },
        )

    def test_descriptions_are_ordered_for_display(self) -> None:
        """Describe the kinds from the most to the least isolated from the host."""
        self.assertEqual(
            [kind.spec.agent_description.split(" ")[0] for kind in launcher.MountKind],
            ["tmpfs", "overlayfs", "read-only", "normal"],
        )


class ResolveMountsTests(unittest.TestCase):
    """Test mount deduplication, ordering, and launch-dir precedence."""

    def test_orders_parents_before_children(self) -> None:
        """Sort mounts so a parent target precedes any nested one."""
        child = launcher.Mount(Path("/a/b/c"), launcher.MountKind.BIND_RO)
        parent = launcher.Mount(Path("/a"), launcher.MountKind.BIND_RO)

        resolved = launcher.resolve_mounts([child, parent], Path("/cwd"))

        self.assertEqual(resolved, [parent, child])

    def test_drops_exact_duplicates(self) -> None:
        """Collapse identical mounts to a single entry."""
        mount = launcher.Mount(Path("/a"), launcher.MountKind.BIND_RO)

        resolved = launcher.resolve_mounts([mount, mount], Path("/cwd"))

        self.assertEqual(resolved, [mount])

    def test_launch_dir_stays_writable_over_read_only_collision(self) -> None:
        """Drop a read-only mount that resolves onto the writable launch directory."""
        cwd = Path("/home/user/.config/agents/skills")
        writable = launcher.Mount(cwd, launcher.MountKind.BIND_RW)
        skills = launcher.Mount(
            Path("/xdg/agents/skills"), launcher.MountKind.BIND_RO, cwd
        )

        resolved = launcher.resolve_mounts([writable, skills], cwd)

        self.assertEqual([m for m in resolved if m.target == cwd], [writable])


class HomeToolsTests(unittest.TestCase):
    """Test HOME_TOOLS construction from the environment."""

    def test_editor_unset(self) -> None:
        """Build HOME_TOOLS without an editor entry when EDITOR is unset."""
        module = load_launcher({"EDITOR": None})

        self.assertNotIn(None, module.HOME_TOOLS)

    def test_editor_set(self) -> None:
        """Include the EDITOR command in HOME_TOOLS when set."""
        module = load_launcher({"EDITOR": "myeditor"})

        self.assertIn("myeditor", module.HOME_TOOLS)


class AgentSpecsTests(unittest.TestCase):
    """Test agent-specific sandbox provisioning."""

    def test_skill_dirs_are_read_only(self) -> None:
        """Expose the shared skill directory read-only for every supported agent."""
        skill_dir = FAKE_CONFIG_HOME / "agents/skills"

        skill_mounts = [
            mount
            for spec in launcher.AGENTS.values()
            for mount in spec.mounts
            if mount.src == skill_dir
        ]

        self.assertEqual(len(skill_mounts), len(launcher.AGENTS))
        self.assertTrue(
            all(mount.kind is launcher.MountKind.BIND_RO for mount in skill_mounts)
        )


class SessionTmpfsMountsTests(unittest.TestCase):
    """Test the tmpfs mounts hiding agent session history in scratch mode."""

    def test_session_dirs_of_known_agents(self) -> None:
        """Point every supported agent at the directory holding its session history."""
        self.assertEqual(
            {name: spec.session_dir for name, spec in launcher.AGENTS.items()},
            {
                "amp": FAKE_HOME / ".local/share/amp/threads",
                "claude": FAKE_HOME / ".claude/projects",
                "codex": FAKE_HOME / ".codex/sessions",
                "pi": FAKE_HOME / ".pi/agent/sessions",
            },
        )

    def test_covers_each_session_dir(self) -> None:
        """Mount a tmpfs over the session dir of every given agent."""
        specs = [launcher.AGENTS["claude"], launcher.AGENTS["codex"]]

        mounts = launcher.session_tmpfs_mounts(specs)

        self.assertEqual(
            mounts,
            [
                launcher.Mount(
                    FAKE_HOME / ".claude/projects", launcher.MountKind.TMPFS
                ),
                launcher.Mount(FAKE_HOME / ".codex/sessions", launcher.MountKind.TMPFS),
            ],
        )

    def test_skips_agent_without_session_dir(self) -> None:
        """Leave out an agent that declares no session dir."""
        self.assertEqual(launcher.session_tmpfs_mounts([launcher.AgentSpec()]), [])


class AuthProfileMountsTests(TempDirTestCase):
    """Test the credentials mounts selecting an agent's auth profile."""

    def setUp(self) -> None:
        """Build an agent spec whose credentials live in a temporary config directory."""
        config_dir = self.make_temp_dir()
        self.credentials = config_dir / ".credentials.json"
        self.profile = config_dir / ".credentials-pro.json"
        self.sandbox_path = Path("/home/user/.claude/.credentials.json")
        self.spec = launcher.AgentSpec(
            credentials=launcher.Mount(
                self.credentials, launcher.MountKind.BIND_RW, self.sandbox_path
            )
        )

    def resolve(
        self, agents: dict[str, launcher.AgentSpec], **profiles: str
    ) -> list[launcher.Mount]:
        """Resolve the auth profile mounts of agents, with only the given profiles selected."""
        env = {
            f"SANDBOX_AGENT_{name.upper()}_AUTH": profile
            for name, profile in profiles.items()
        }
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            return launcher.auth_profile_mounts(agents)

    def resolve_report(
        self, agents: dict[str, launcher.AgentSpec], **profiles: str
    ) -> str:
        """Resolve the auth profile mounts of agents, returning what the launcher reported."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.resolve(agents, **profiles)
        return stderr.getvalue()

    def claude_mount(self, src: Path) -> list[launcher.Mount]:
        """Return the single mount exposing src at the claude credentials path."""
        return [launcher.Mount(src, launcher.MountKind.BIND_RW, self.sandbox_path)]

    def test_credentials_of_known_agents(self) -> None:
        """Point every agent supporting auth profiles at its credentials file."""
        self.assertEqual(
            {name: spec.credentials for name, spec in launcher.AGENTS.items()},
            {
                "amp": None,
                "claude": launcher.Mount(
                    FAKE_CONFIG_HOME / "claude/.credentials.json",
                    launcher.MountKind.BIND_RW,
                    FAKE_HOME / ".claude/.credentials.json",
                ),
                "codex": launcher.Mount(
                    FAKE_CONFIG_HOME / "codex/auth.json",
                    launcher.MountKind.BIND_RW,
                    FAKE_HOME / ".codex/auth.json",
                ),
                "pi": launcher.Mount(
                    FAKE_CONFIG_HOME / "pi/agent/auth.json",
                    launcher.MountKind.BIND_RW,
                    FAKE_HOME / ".pi/agent/auth.json",
                ),
            },
        )

    def test_no_mount_without_selected_profile(self) -> None:
        """Leave the agent's own credentials in place when no profile is selected."""
        self.credentials.write_text('{"token": "default"}')

        self.assertEqual(self.resolve({"claude": self.spec}), [])
        self.assertFalse(self.profile.exists())

    def test_seeds_new_profile_beside_default_credentials(self) -> None:
        """Create empty credentials for a new profile, never copying the default ones."""
        self.credentials.write_text('{"token": "default"}')

        mounts = self.resolve({"claude": self.spec}, claude="pro")

        self.assertEqual(mounts, self.claude_mount(self.profile))
        self.assertEqual(self.profile.read_text(), "{}")
        self.assertEqual(self.profile.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.credentials.read_text(), '{"token": "default"}')

    def test_keeps_existing_profile_credentials(self) -> None:
        """Select the credentials of an already used profile without modifying them."""
        self.credentials.write_text('{"token": "default"}')
        self.profile.write_text('{"token": "pro"}')

        mounts = self.resolve({"claude": self.spec}, claude="pro")

        self.assertEqual(mounts, self.claude_mount(self.profile))
        self.assertEqual(self.profile.read_text(), '{"token": "pro"}')

    def test_tolerates_a_concurrent_first_launch(self) -> None:
        """Keep the credentials a launcher racing on the same new profile seeded first."""
        self.credentials.write_text('{"token": "default"}')
        self.profile.write_text('{"token": "pro"}')

        # the racing launcher wins between this one's absence check and its own create
        with unittest.mock.patch.object(Path, "exists", return_value=False):
            mounts = self.resolve({"claude": self.spec}, claude="pro")

        self.assertEqual(mounts, self.claude_mount(self.profile))
        self.assertEqual(self.profile.read_text(), '{"token": "pro"}')

    def test_seeds_new_profile_without_default_credentials(self) -> None:
        """Create empty credentials for a new profile of an agent that is not logged in."""
        mounts = self.resolve({"claude": self.spec}, claude="pro")

        self.assertEqual(mounts, self.claude_mount(self.profile))
        self.assertEqual(self.profile.read_text(), "{}")
        self.assertEqual(self.profile.stat().st_mode & 0o777, 0o600)

    def test_creates_missing_config_dir(self) -> None:
        """Create the config directory of an agent that was never launched."""
        credentials = self.make_temp_dir() / "codex/auth.json"
        spec = launcher.AgentSpec(
            credentials=launcher.Mount(credentials, launcher.MountKind.BIND_RW)
        )

        self.resolve({"codex": spec}, codex="pro")

        self.assertEqual((credentials.parent / "auth-pro.json").read_text(), "{}")

    def test_switches_each_agent_separately(self) -> None:
        """Switch only the agents whose profile env var is set."""
        self.credentials.write_text('{"token": "default"}')
        other = self.make_temp_dir() / "auth.json"
        other.write_text('{"token": "pi"}')
        pi_spec = launcher.AgentSpec(
            credentials=launcher.Mount(other, launcher.MountKind.BIND_RW)
        )

        mounts = self.resolve({"claude": self.spec, "pi": pi_spec}, pi="perso")

        self.assertEqual(
            mounts,
            [
                launcher.Mount(
                    other.parent / "auth-perso.json", launcher.MountKind.BIND_RW
                )
            ],
        )

    def test_rejects_agent_without_credentials(self) -> None:
        """Fail when a profile is selected for an agent that keeps no credentials file."""
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            self.resolve({"amp": launcher.AgentSpec()}, amp="pro")

    def test_login_commands_of_known_agents(self) -> None:
        """Name a login command for the agents that refuse to start on empty credentials."""
        self.assertEqual(
            {name: spec.auth_profile_login for name, spec in launcher.AGENTS.items()},
            {"amp": None, "claude": None, "codex": "codex login", "pi": None},
        )

    def test_reports_a_profile_that_was_never_logged_into(self) -> None:
        """Point at the agent's login command while the profile credentials stay empty."""
        spec = dataclasses.replace(self.spec, auth_profile_login="codex login")

        report = self.resolve_report({"codex": spec}, codex="pro")

        # a login command missing the selector would overwrite the default credentials
        self.assertIn("`SANDBOX_AGENT_CODEX_AUTH=pro codex login`", report)
        # the seeding launch is not the only one to report it, the profile stays unusable until
        # the login writes credentials
        self.assertEqual(self.resolve_report({"codex": spec}, codex="pro"), report)

    def test_reports_nothing_once_the_profile_is_logged_in(self) -> None:
        """Stay quiet once the profile credentials hold a login."""
        spec = dataclasses.replace(self.spec, auth_profile_login="codex login")
        self.profile.write_text('{"token": "pro"}')

        self.assertEqual(self.resolve_report({"codex": spec}, codex="pro"), "")

    def test_reports_nothing_for_an_agent_starting_logged_out(self) -> None:
        """Stay quiet for an agent whose own login flow handles empty credentials."""
        self.assertEqual(self.resolve_report({"claude": self.spec}, claude="pro"), "")


class MemfdDataTests(unittest.TestCase):
    """Test memfd creation."""

    def test_round_trips_data_from_start(self) -> None:
        """Read back the written data from offset zero."""
        fd = launcher.memfd_data(b"payload")
        self.addCleanup(os.close, fd)

        self.assertEqual(os.read(fd, 1024), b"payload")


class GenPasswdTests(unittest.TestCase):
    """Test /etc/passwd generation."""

    def test_current_user_entry_forces_bash(self) -> None:
        """Emit the current user first with the shell forced to bash."""
        entry = pwd.getpwuid(os.getuid())
        expected = ":".join(
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

        lines = launcher.gen_passwd([]).decode().splitlines()

        self.assertEqual(lines[0], expected)

    def test_lists_only_current_and_group_users(self) -> None:
        """Emit only the current user plus the users named after copied groups."""
        expected_names = [pwd.getpwuid(os.getuid()).pw_name]
        for name in launcher.GROUPS:
            with contextlib.suppress(KeyError):
                expected_names.append(pwd.getpwnam(name).pw_name)

        lines = launcher.gen_passwd(launcher.GROUPS).decode().splitlines()

        self.assertEqual([line.split(":")[0] for line in lines], expected_names)


class GenGroupTests(unittest.TestCase):
    """Test /etc/group generation."""

    def test_current_group_entry(self) -> None:
        """Emit the current primary group first with its members."""
        entry = grp.getgrgid(os.getgid())
        expected = ":".join(
            [
                entry.gr_name,
                entry.gr_passwd or "",
                str(entry.gr_gid),
                ",".join(entry.gr_mem),
            ]
        )

        lines = launcher.gen_group([]).decode().splitlines()

        self.assertEqual(lines[0], expected)

    def test_lists_only_current_and_copied_groups(self) -> None:
        """Emit only the primary group plus the groups copied from the host."""
        expected_names = [grp.getgrgid(os.getgid()).gr_name]
        for name in launcher.GROUPS:
            with contextlib.suppress(KeyError):
                expected_names.append(grp.getgrnam(name).gr_name)

        lines = launcher.gen_group(launcher.GROUPS).decode().splitlines()

        self.assertEqual([line.split(":")[0] for line in lines], expected_names)


class GenGlobalAgentsMdTests(unittest.TestCase):
    """Test generation of the agent's global instructions file."""

    def setUp(self) -> None:
        """Ensure the base and per-agent AGENTS.md locations exist and start absent."""
        self.base_path = FAKE_CONFIG_HOME / "agents/AGENTS.md"
        self.agent_path = FAKE_CONFIG_HOME / "agents/AGENTS.claude.md"
        for path in (self.base_path, self.agent_path):
            path.parent.mkdir(parents=True, exist_ok=True)
            self.addCleanup(path.unlink, missing_ok=True)
            path.unlink(missing_ok=True)

    def render(self, agent: str = "claude", **facts: object) -> str:
        """Generate the instructions file content, overriding the given sandbox facts only."""
        return launcher.gen_global_agents_md(
            agent, dataclasses.replace(EMPTY_SANDBOX_FACTS, **facts)
        ).decode()

    def test_appends_section_to_base_file(self) -> None:
        """Append the sandbox section after the user's base instructions."""
        self.base_path.write_text("# my base\n")

        content = self.render()

        self.assertTrue(content.startswith("# my base\n\n## Sandbox environment\n"))

    def test_without_base_file(self) -> None:
        """Emit only the sandbox section when the base file is missing."""
        content = self.render()

        self.assertTrue(content.startswith("## Sandbox environment\n"))
        self.assertTrue(content.endswith("\n"))

    def test_agent_file_between_base_and_section(self) -> None:
        """Insert the per-agent file after the base instructions, before the sandbox section."""
        self.base_path.write_text("# my base\n")
        self.agent_path.write_text("# claude only\n")

        content = self.render()

        self.assertTrue(
            content.startswith("# my base\n\n# claude only\n\n## Sandbox environment\n")
        )

    def test_agent_file_without_base_file(self) -> None:
        """Emit the per-agent file before the sandbox section when the base is missing."""
        self.agent_path.write_text("# claude only\n")

        content = self.render()

        self.assertTrue(content.startswith("# claude only\n\n## Sandbox environment\n"))

    def test_agent_file_of_another_agent_ignored(self) -> None:
        """Read the per-agent file of the rendered agent only."""
        self.agent_path.write_text("# claude only\n")

        content = self.render(agent="codex")

        self.assertNotIn("claude only", content)

    def test_filesystem_bullet_lists_mounts_by_kind(self) -> None:
        """List directory mounts grouped by kind, in display order."""
        dir_mounts: dict[object, list[Path]] = {
            launcher.MountKind.BIND_RW: [Path("/work")],
            launcher.MountKind.TMPFS: [Path("/tmp"), Path("/run")],
        }

        content = self.render(dir_mounts=dir_mounts)

        tmpfs_pos = content.index("tmpfs filesystems")
        bind_rw_pos = content.index("normal bind mounts")
        self.assertLess(tmpfs_pos, bind_rw_pos)
        self.assertIn("`/tmp`, `/run`", content)
        self.assertIn("`/work`", content)
        self.assertNotIn("overlayfs", content)
        self.assertNotIn("read-only bind mounts", content)

    def test_exchange_dir_bullet(self) -> None:
        """Mention the exchange directory only when one exists."""
        with_bullet = self.render(exchange_dir=Path("/run/user/1000/exchange"))
        without_bullet = self.render()

        self.assertIn("place them under `/run/user/1000/exchange`", with_bullet)
        self.assertNotIn("exchange", without_bullet)

    def test_unjaild_bullet(self) -> None:
        """Mention xdg-open only when the unjail tools are available."""
        with_bullet = self.render(has_unjaild=True)
        without_bullet = self.render()

        self.assertIn("`xdg-open` is available", with_bullet)
        self.assertNotIn("xdg-open", without_bullet)

    def test_proxy_bullet(self) -> None:
        """Mention gh only when the auth proxy is running."""
        with_bullet = self.render(has_proxy=True)
        without_bullet = self.render()

        self.assertIn("`gh` is available and authenticated", with_bullet)
        self.assertNotIn("`gh`", without_bullet)

    def test_jj_wrapper_bullet(self) -> None:
        """Mention the jj wrapper and the real binary only inside a jj workspace."""
        with_bullet = self.render(jj_bin="/opt/bin/jj")
        without_bullet = self.render()

        self.assertIn(f"`{launcher.SANDBOX_BIN_DIR / 'jj'}`", with_bullet)
        self.assertIn("`/opt/bin/jj`", with_bullet)
        self.assertNotIn("jj", without_bullet)


class ResolveExtraAgentsTests(TempDirTestCase):
    """Test which always-provisioned agents are added beside the launched one."""

    @contextlib.contextmanager
    def agents(
        self, provision_always: tuple[Path, ...]
    ) -> collections.abc.Iterator[None]:
        """Replace the agent table with a claude and a pi provisioned from the given paths."""
        table = {
            "claude": launcher.AgentSpec(agents_md=Path("/claude/CLAUDE.md")),
            "pi": launcher.AgentSpec(
                agents_md=Path("/pi/AGENTS.md"), provision_always=provision_always
            ),
        }
        with unittest.mock.patch.object(launcher, "AGENTS", table):
            yield

    def test_includes_installed_extra(self) -> None:
        """Map an installed extra agent to its resolved binary."""
        pi_bin = self.make_executable(self.make_temp_dir() / "pi")
        with self.agents((pi_bin,)):
            self.assertEqual(launcher.resolve_extra_agents("claude"), {"pi": pi_bin})

    def test_prefers_earliest_installed_binary(self) -> None:
        """Resolve an extra agent to the first of its executable binaries."""
        base = self.make_temp_dir()
        first = self.make_executable(base / "first")
        second = self.make_executable(base / "second")
        with self.agents((first, second)):
            self.assertEqual(launcher.resolve_extra_agents("claude"), {"pi": first})

    def test_skips_non_executable_binary(self) -> None:
        """Skip a binary that exists but is not executable."""
        base = self.make_temp_dir()
        plain = base / "plain"
        plain.write_text("")
        pi_bin = self.make_executable(base / "pi")
        with self.agents((plain, pi_bin)):
            self.assertEqual(launcher.resolve_extra_agents("claude"), {"pi": pi_bin})

    def test_excludes_uninstalled_extra(self) -> None:
        """Drop an extra agent whose binary is absent."""
        base = self.make_temp_dir()
        with self.agents((base / "pi",)):
            self.assertEqual(launcher.resolve_extra_agents("claude"), {})

    def test_excludes_agent_never_provisioned_beside_others(self) -> None:
        """Drop an agent with no always-provisioned binary path."""
        pi_bin = self.make_executable(self.make_temp_dir() / "pi")
        with self.agents((pi_bin,)):
            self.assertEqual(launcher.resolve_extra_agents("pi"), {})

    def test_excludes_entry_agent(self) -> None:
        """Skip the launched agent even when it is an always-provisioned one."""
        pi_bin = self.make_executable(self.make_temp_dir() / "pi")
        with self.agents((pi_bin,)):
            self.assertNotIn("pi", launcher.resolve_extra_agents("pi"))


class AgentFilesTests(unittest.TestCase):
    """Test the per-agent generated config files."""

    def test_codex_config_trusts_the_launch_directory(self) -> None:
        """Trust the launch directory through codex's read-only system config layer."""
        files = launcher.AGENTS["codex"].files
        content = files[Path("/etc/codex/config.toml")].data.decode()

        self.assertEqual(
            content,
            f'[projects."{IMPORT_CWD}"]\ntrust_level = "trusted"\n',
        )

    def test_pi_npmrc_points_at_its_state_dir(self) -> None:
        """Prefix pi's npm modules with its own state directory."""
        content = launcher.AGENTS["pi"].files[FAKE_HOME / ".npmrc"].data.decode()

        self.assertEqual(content, f"prefix={FAKE_HOME / '.pi/agent/npm'}\n")


class ClaudeMemoryEnvTests(TempDirTestCase):
    """Test the Claude memory override keyed on the jj default workspace."""

    def test_no_override_without_jj_default_workspace(self) -> None:
        """Leave the memory path alone outside a jj repository."""
        self.assertEqual(launcher.AGENTS["claude"].env, {})

    @unittest.skipUnless(launcher.JJ_BIN is not None, "jj is not installed")
    def test_override_keyed_on_default_workspace(self) -> None:
        """Point a secondary workspace's memory dir at the default workspace's project slug."""
        base = self.make_temp_dir()
        repo = self.make_jj_repo(base / "default")
        workspace = self.add_jj_workspace(repo, base / "workspace")

        module = load_launcher(cwd=workspace)

        slug = launcher.claude_project_slug(repo)
        self.assertEqual(
            module.AGENTS["claude"].env,
            {
                "CLAUDE_COWORK_MEMORY_PATH_OVERRIDE": str(
                    FAKE_HOME / ".claude/projects" / slug / "memory"
                )
            },
        )


class JjWrapperFileTests(TempDirTestCase):
    """Test provisioning of the read-only jj wrapper."""

    def test_absent_outside_jj_repo(self) -> None:
        """Provision no jj wrapper when the launch directory is outside a jj repository."""
        self.assertNotIn(launcher.SANDBOX_BIN_DIR / "jj", launcher.FILES)

    @unittest.skipUnless(launcher.JJ_BIN is not None, "jj is not installed")
    def test_provisioned_inside_jj_repo(self) -> None:
        """Provision the jj wrapper when the launch directory is inside a jj repository."""
        repo = self.make_jj_repo(self.make_temp_dir() / "repo")

        module = load_launcher(cwd=repo)

        self.assertIn(module.SANDBOX_BIN_DIR / "jj", module.FILES)

    def test_invokes_real_jj_with_ignore_working_copy(self) -> None:
        """Inject the read-only flag while preserving arguments for the next jj on PATH."""
        base = self.make_temp_dir()
        wrapper_dir = base / "wrapper"
        real_dir = base / "real"
        wrapper_dir.mkdir()
        real_dir.mkdir()
        wrapper = wrapper_dir / "jj"
        wrapper.write_bytes(launcher.JJ_WRAPPER.data)
        wrapper.chmod(0o755)
        real = real_dir / "jj"
        real.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        real.chmod(0o755)

        result = subprocess.run(
            [wrapper, "log", "-r", "@"],
            env={
                "PATH": os.pathsep.join((str(wrapper_dir), str(real_dir), os.defpath))
            },
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertEqual(
            result.stdout.splitlines(), ["--ignore-working-copy", "log", "-r", "@"]
        )


class ProxyTests(unittest.TestCase):
    """Test detection of the auth-injecting proxy."""

    def test_inactive_without_ca_bundle(self) -> None:
        """Detect no proxy when the CA bundle file is absent."""
        self.assertFalse(launcher.PROXY_ACTIVE)
        self.assertEqual(launcher.PROXY_MOUNTS, [])
        self.assertEqual(launcher.PROXY_ENV, {})

    def test_mount_and_env_with_ca_bundle(self) -> None:
        """Expose the CA bundle and route gh through the proxy when it runs."""
        bundle = Path(launcher.PROXY_CA_BUNDLE)
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_text("FAKE CA")
        self.addCleanup(bundle.unlink)

        module = load_launcher()

        self.assertEqual(
            module.PROXY_MOUNTS,
            [module.Mount(bundle, module.MountKind.BIND_RO)],
        )
        self.assertEqual(
            module.PROXY_ENV,
            {
                "GH_TOKEN": "agent-proxy-placeholder",
                "HTTPS_PROXY": "http://127.0.0.1:8085",
                "NO_PROXY": "127.0.0.1,localhost",
                "SSL_CERT_FILE": str(bundle),
            },
        )


class ToolMountsTests(TempDirTestCase):
    """Test the mounts exposing host tools."""

    def mounts_for(self, tools: list[str], path: Path) -> list[object]:
        """Resolve the tool mounts for the given tool names against a single PATH dir."""
        with (
            unittest.mock.patch.object(launcher, "HOME_TOOLS", tools),
            unittest.mock.patch.dict(os.environ, {"PATH": str(path)}),
        ):
            return launcher.tool_mounts()

    def test_exposes_tool_outside_usr(self) -> None:
        """Bind a tool resolved outside /usr read-only at its own path."""
        base = self.make_temp_dir()
        tool = self.make_executable(base / "mytool")

        self.assertEqual(
            self.mounts_for(["mytool"], base),
            [launcher.Mount(tool, launcher.MountKind.BIND_RO)],
        )

    def test_accepts_absolute_tool_path(self) -> None:
        """Bind a tool given as an absolute path, as EDITOR may be."""
        base = self.make_temp_dir()
        tool = self.make_executable(base / "mytool")

        self.assertEqual(
            self.mounts_for([str(tool)], self.make_temp_dir()),
            [launcher.Mount(tool, launcher.MountKind.BIND_RO)],
        )

    def test_skips_tool_under_usr(self) -> None:
        """Skip a tool already exposed by the read-only /usr bind."""
        self.assertEqual(self.mounts_for(["env"], Path("/usr/bin")), [])

    def test_skips_missing_tool(self) -> None:
        """Skip a tool that is not installed."""
        self.assertEqual(self.mounts_for(["mytool"], self.make_temp_dir()), [])


class CargoTargetMountsTests(TempDirTestCase):
    """Test the cargo target directory overlay."""

    def mounts_for(self, cwd: Path) -> list[object]:
        """Resolve the cargo target mounts for a launch directory."""
        with unittest.mock.patch.object(launcher, "CWD", cwd):
            return launcher.cargo_target_mounts()

    def test_overlays_target_dir_of_a_crate(self) -> None:
        """Overlay the target dir, creating it, when the launch dir is a crate."""
        cwd = self.make_temp_dir()
        (cwd / "Cargo.toml").write_text("")

        mounts = self.mounts_for(cwd)

        self.assertEqual(
            mounts, [launcher.Mount(cwd / "target", launcher.MountKind.OVERLAYFS)]
        )
        self.assertTrue((cwd / "target").is_dir())

    def test_no_mount_outside_a_crate(self) -> None:
        """Leave the launch dir alone when it holds no manifest."""
        cwd = self.make_temp_dir()

        self.assertEqual(self.mounts_for(cwd), [])
        self.assertFalse((cwd / "target").exists())


class AgentBinaryMountsTests(TempDirTestCase):
    """Test the mounts exposing the provisioned agents' binaries."""

    def test_binds_binaries_and_wrappers(self) -> None:
        """Bind the launched agent's binary, the extras' binaries, and every wrapper."""
        base = self.make_temp_dir()
        claude_bin = self.make_executable(base / "claude")
        pi_bin = self.make_executable(base / "pi")

        with unittest.mock.patch.object(sys, "argv", ["launcher", str(claude_bin)]):
            mounts = launcher.agent_binary_mounts(["claude", "pi"], {"pi": pi_bin})

        self.assertEqual(
            mounts,
            [
                launcher.Mount(
                    FAKE_HOME / ".local/bin/claude", launcher.MountKind.BIND_RO
                ),
                launcher.Mount(FAKE_HOME / ".local/bin/pi", launcher.MountKind.BIND_RO),
                launcher.Mount(claude_bin, launcher.MountKind.BIND_RO),
                launcher.Mount(pi_bin, launcher.MountKind.BIND_RO),
            ],
        )

    def test_skips_binary_under_usr(self) -> None:
        """Skip an agent binary already exposed by the read-only /usr bind."""
        with unittest.mock.patch.object(sys, "argv", ["launcher", "/usr/bin/env"]):
            mounts = launcher.agent_binary_mounts(["env"], {})

        self.assertEqual(
            mounts,
            [launcher.Mount(FAKE_HOME / ".local/bin/env", launcher.MountKind.BIND_RO)],
        )


class VcsDirsTests(TempDirTestCase):
    """Test which VCS directories are exposed in the sandbox."""

    def test_launch_dir_dirs_outside_a_known_repo(self) -> None:
        """Expose the launch dir's own VCS dirs when no repository is detected."""
        cwd = self.make_temp_dir()

        self.assertEqual(launcher.vcs_dirs(cwd, None), [cwd / ".git", cwd / ".jj"])

    def test_repo_root_dirs(self) -> None:
        """Expose the VCS dirs of the launch dir when it is the repository root."""
        cwd = self.make_temp_dir()
        info = launcher.RepoInfo(cwd, None, None)

        self.assertEqual(launcher.vcs_dirs(cwd, info), [cwd / ".git", cwd / ".jj"])

    def test_no_dirs_below_the_repo_root(self) -> None:
        """Expose no VCS dir when the launch dir sits below the repository root."""
        repo = self.make_temp_dir()
        subdir = repo / "nested"
        info = launcher.RepoInfo(repo, repo, (repo,))

        self.assertEqual(launcher.vcs_dirs(subdir, info), [])

    def test_adds_jj_default_workspace_dirs(self) -> None:
        """Expose the default workspace's VCS dirs when launched from another workspace."""
        base = self.make_temp_dir()
        default = base / "main"
        ws = base / "ws1"
        info = launcher.RepoInfo(ws, default, (default, ws))

        self.assertEqual(
            launcher.vcs_dirs(ws, info),
            [ws / ".git", ws / ".jj", default / ".git", default / ".jj"],
        )

    def test_default_workspace_dirs_are_not_duplicated(self) -> None:
        """Expose the VCS dirs once when launched from the default workspace itself."""
        default = self.make_temp_dir()
        info = launcher.RepoInfo(default, default, (default,))

        self.assertEqual(
            launcher.vcs_dirs(default, info),
            [default / ".git", default / ".jj"],
        )


class ExchangeRootTests(TempDirTestCase):
    """Test the identity directory keying the exchange dir."""

    def test_cwd_outside_a_repo(self) -> None:
        """Key on the launch dir when it is not in a repository."""
        cwd = self.make_temp_dir()

        self.assertEqual(launcher.exchange_root(cwd, None), cwd)

    def test_git_repo_root(self) -> None:
        """Key on the repository root in a git repository."""
        repo = self.make_temp_dir()
        subdir = repo / "nested"

        info = launcher.RepoInfo(repo, None, None)

        self.assertEqual(launcher.exchange_root(subdir, info), repo)

    def test_jj_workspaces_share_a_root(self) -> None:
        """Key every workspace of a jj repository on their shared directory."""
        base = self.make_temp_dir()
        roots = [base / "proj/main", base / "proj/ws1"]
        for root in roots:
            root.mkdir(parents=True)
        info = launcher.RepoInfo(roots[1], roots[0], tuple(roots))

        self.assertEqual(launcher.exchange_root(roots[1], info), base / "proj")


class ParseCpuListTests(unittest.TestCase):
    """Test sysfs CPU range list expansion."""

    def test_expands_ranges_and_singletons(self) -> None:
        """Expand mixed ranges and single indices into a flat list."""
        self.assertEqual(
            launcher.parse_cpu_list("0-3,8,10-11"), [0, 1, 2, 3, 8, 10, 11]
        )

    def test_single_cpu(self) -> None:
        """Expand a lone index into a one-element list."""
        self.assertEqual(launcher.parse_cpu_list("0"), [0])


class GenCpuTopologyTests(TempDirTestCase):
    """Test host CPU topology extraction from sysfs."""

    def write_sysfs(self, layout: dict[int, tuple[int, int]]) -> Path:
        """Build a fake /sys/devices/system/cpu tree from a cpu -> (package, core) map."""
        cpu_root = self.make_temp_dir()
        online = ",".join(str(cpu) for cpu in sorted(layout))
        (cpu_root / "online").write_text(f"{online}\n")
        for cpu, (package, core) in layout.items():
            topology = cpu_root / f"cpu{cpu}" / "topology"
            topology.mkdir(parents=True)
            (topology / "physical_package_id").write_text(f"{package}\n")
            (topology / "core_id").write_text(f"{core}\n")
        return cpu_root

    def topology_for(self, layout: dict[int, tuple[int, int]]) -> bytes | None:
        """Run gen_cpu_topology against a fake sysfs tree built from the layout."""
        cpu_root = self.write_sysfs(layout)
        with unittest.mock.patch.object(launcher, "SYS_CPU_DIR", cpu_root):
            return launcher.gen_cpu_topology()

    def test_single_socket_with_smt(self) -> None:
        """Report one socket, two cores, two threads for a hyperthreaded quad."""
        layout = {0: (0, 0), 1: (0, 1), 2: (0, 0), 3: (0, 1)}

        self.assertEqual(
            self.topology_for(layout), b"sockets=1,cores=2,threads=2\npin=0,2,1,3"
        )

    def test_dual_socket_with_smt(self) -> None:
        """Report two sockets, two cores, two threads across two packages."""
        layout = {
            0: (0, 0),
            1: (0, 1),
            2: (0, 0),
            3: (0, 1),
            4: (1, 0),
            5: (1, 1),
            6: (1, 0),
            7: (1, 1),
        }

        self.assertEqual(
            self.topology_for(layout),
            b"sockets=2,cores=2,threads=2\npin=0,2,1,3,4,6,5,7",
        )

    def test_pin_groups_strided_hyperthread_siblings(self) -> None:
        """Order the pin map so each core's strided siblings become adjacent vCPUs."""
        layout = {cpu: (0, cpu % 4) for cpu in range(8)}

        self.assertEqual(
            self.topology_for(layout),
            b"sockets=1,cores=4,threads=2\npin=0,4,1,5,2,6,3,7",
        )

    def test_hybrid_host_exposes_one_thread_per_physical_core(self) -> None:
        """Drop hyperthreads on a heterogeneous host, pinning one CPU per core."""
        layout = {0: (0, 0), 4: (0, 0), 1: (0, 1), 5: (0, 1), 2: (0, 2), 3: (0, 3)}

        self.assertEqual(
            self.topology_for(layout),
            b"sockets=1,cores=4,threads=1\npin=0,1,2,3",
        )

    def test_returns_none_without_sysfs(self) -> None:
        """Report no topology when the online CPU file is absent."""
        with unittest.mock.patch.object(launcher, "SYS_CPU_DIR", self.make_temp_dir()):
            self.assertIsNone(launcher.gen_cpu_topology())


class PromptBoolTests(unittest.TestCase):
    """Test interactive boolean prompting."""

    def prompt(self, answers: list[str | EOFError], *, default: bool = False) -> bool:
        """Prompt on a fake tty feeding the given input answers."""
        with (
            unittest.mock.patch("sys.stdin") as stdin,
            unittest.mock.patch("builtins.input", side_effect=answers),
        ):
            stdin.isatty.return_value = True
            return launcher.prompt_bool("continue?", default=default)

    def test_non_tty_returns_default(self) -> None:
        """Return the default without prompting when stdin is not a tty."""
        with unittest.mock.patch("sys.stdin") as stdin:
            stdin.isatty.return_value = False
            self.assertFalse(launcher.prompt_bool("continue?"))
            self.assertTrue(launcher.prompt_bool("continue?", default=True))

    def test_explicit_answers(self) -> None:
        """Map y and n answers to True and False, ignoring case and spacing."""
        self.assertTrue(self.prompt(["y"]))
        self.assertTrue(self.prompt([" Y "]))
        self.assertFalse(self.prompt(["n"], default=True))

    def test_empty_answer_returns_default(self) -> None:
        """Return the default on an empty answer."""
        self.assertFalse(self.prompt([""]))
        self.assertTrue(self.prompt([""], default=True))

    def test_invalid_answer_reprompts(self) -> None:
        """Prompt again until a recognized answer is given."""
        self.assertTrue(self.prompt(["bogus", "y"]))

    def test_eof_returns_default(self) -> None:
        """Return the default when input is exhausted."""
        self.assertTrue(self.prompt([EOFError()], default=True))


class FatalErrorTests(unittest.TestCase):
    """Test fatal error reporting."""

    def test_writes_message_and_exits(self) -> None:
        """Write the prefixed message to stderr and exit with status 1."""
        stderr = io.StringIO()
        with (
            self.assertRaises(SystemExit) as ctx,
            contextlib.redirect_stderr(stderr),
        ):
            launcher.fatal_error("boom")

        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(stderr.getvalue(), "sandbox-coding-agent: boom\n")


class ResolveJjTests(TempDirTestCase):
    """Test resolving the real jj binary past the sandbox wrapper."""

    def make_jj_stub(self, directory: Path) -> Path:
        """Create an executable jj stub in directory and return the directory."""
        directory.mkdir(parents=True, exist_ok=True)
        stub = directory / "jj"
        stub.write_text("#!/bin/sh\n")
        stub.chmod(0o755)
        return directory

    def test_skips_wrapper_dir(self) -> None:
        """Skip the wrapper's jj and return the next one on PATH."""
        base = self.make_temp_dir()
        wrapper_dir = self.make_jj_stub(base / "run/bin")
        real_dir = self.make_jj_stub(base / "usr/bin")
        with (
            unittest.mock.patch.object(launcher, "SANDBOX_BIN_DIR", wrapper_dir),
            unittest.mock.patch.dict(
                os.environ,
                {"PATH": os.pathsep.join([str(wrapper_dir), str(real_dir)])},
            ),
        ):
            self.assertEqual(launcher.resolve_jj(), str(real_dir / "jj"))

    def test_returns_none_without_jj(self) -> None:
        """Return None when no jj is on PATH."""
        with unittest.mock.patch.dict(os.environ, {"PATH": str(self.make_temp_dir())}):
            self.assertIsNone(launcher.resolve_jj())

    def test_uses_default_path_when_path_is_unset(self) -> None:
        """Search the system default path for jj when PATH is absent."""
        real_dir = self.make_jj_stub(self.make_temp_dir() / "default-bin")

        with (
            unittest.mock.patch.object(os, "defpath", str(real_dir)),
            unittest.mock.patch.dict(os.environ, {}, clear=True),
        ):
            self.assertEqual(launcher.resolve_jj(), str(real_dir / "jj"))


class RunCaptureTests(unittest.TestCase):
    """Test command output capture."""

    def test_returns_stdout(self) -> None:
        """Return the standard output of a successful command."""
        self.assertEqual(launcher.run_capture(["echo", "out"]), "out\n")

    def test_returns_none_on_failure(self) -> None:
        """Return None when the command exits non-zero."""
        self.assertIsNone(launcher.run_capture(["false"]))

    def test_returns_none_when_command_is_missing(self) -> None:
        """Return None when the command cannot be run at all."""
        self.assertIsNone(launcher.run_capture(["/nonexistent/command"]))


class GitToplevelTests(TempDirTestCase):
    """Test git repository toplevel discovery."""

    def test_returns_toplevel_from_subdir(self) -> None:
        """Find the git toplevel from a nested directory."""
        repo = self.make_git_repo()
        subdir = repo / "nested"
        subdir.mkdir()

        with contextlib.chdir(subdir):
            self.assertEqual(launcher.git_toplevel(), repo)

    def test_returns_none_outside_repo(self) -> None:
        """Find no toplevel outside any git repository."""
        with contextlib.chdir(self.make_temp_dir()):
            self.assertIsNone(launcher.git_toplevel())


class JjWorkspacesTests(TempDirTestCase):
    """Test enumeration of a jj repository's workspaces."""

    def test_returns_none_outside_repo(self) -> None:
        """Find no workspaces outside any jj repository."""
        with contextlib.chdir(self.make_temp_dir()):
            self.assertIsNone(launcher.jj_workspaces())

    @unittest.skipUnless(launcher.JJ_BIN is not None, "jj is not installed")
    def test_returns_default_workspace(self) -> None:
        """Return the sole default workspace of a single-workspace repository."""
        repo = self.make_jj_repo(self.make_temp_dir() / "repo")
        with contextlib.chdir(repo):
            self.assertEqual(launcher.jj_workspaces(), {"default": repo})

    @unittest.skipUnless(launcher.JJ_BIN is not None, "jj is not installed")
    def test_returns_all_named_workspaces(self) -> None:
        """Return every workspace name and root from any workspace of the repository."""
        base = self.make_temp_dir()
        repo = self.make_jj_repo(base / "main")
        ws1 = self.add_jj_workspace(repo, base / "ws1")

        for cwd in (repo, ws1):
            with contextlib.chdir(cwd):
                self.assertEqual(
                    launcher.jj_workspaces(),
                    {"default": repo, "ws1": ws1},
                )


class QueryRepoInfoTests(TempDirTestCase):
    """Test single-query VCS layout resolution."""

    def test_returns_none_outside_repo(self) -> None:
        """Resolve no layout outside any repository."""
        cwd = self.make_temp_dir()
        with contextlib.chdir(cwd):
            self.assertIsNone(launcher.query_repo_info(cwd))

    def test_git_repo_sets_root_only(self) -> None:
        """Resolve a git repository to its toplevel with no jj fields."""
        repo = self.make_git_repo()
        subdir = repo / "nested"
        subdir.mkdir()

        with contextlib.chdir(subdir):
            info = launcher.query_repo_info(subdir)

        self.assertEqual(info, launcher.RepoInfo(repo, None, None))

    @unittest.skipUnless(launcher.JJ_BIN is not None, "jj is not installed")
    def test_jj_single_workspace(self) -> None:
        """Resolve a single-workspace jj repo to itself as root, default and sole workspace."""
        repo = self.make_jj_repo(self.make_temp_dir() / "repo")
        with contextlib.chdir(repo):
            info = launcher.query_repo_info(repo)

        self.assertEqual(info, launcher.RepoInfo(repo, repo, (repo,)))

    @unittest.skipUnless(launcher.JJ_BIN is not None, "jj is not installed")
    def test_jj_multi_workspace_keys_current_and_default(self) -> None:
        """Key root on the current workspace while listing all roots and the default."""
        base = self.make_temp_dir()
        repo = self.make_jj_repo(base / "main")
        ws1 = self.add_jj_workspace(repo, base / "ws1")

        with contextlib.chdir(ws1):
            info = launcher.query_repo_info(ws1)

        assert info is not None
        self.assertEqual(info.root, ws1)
        self.assertEqual(info.jj_default_ws_root, repo)
        self.assertEqual(set(info.jj_workspace_roots or []), {repo, ws1})


class SharedWorkspaceRootTests(TempDirTestCase):
    """Test the exchange root shared across a repository's workspaces."""

    def build(self, rel_roots: list[str]) -> tuple[Path, list[Path]]:
        """Create the given workspace directories under a fresh base."""
        base = self.make_temp_dir()
        roots = [base / rel for rel in rel_roots]
        for root in roots:
            root.mkdir(parents=True, exist_ok=True)
        return base, roots

    def test_single_workspace_returns_itself(self) -> None:
        """Key on the sole workspace root when there is only one."""
        base, roots = self.build(["proj"])

        self.assertEqual(launcher.shared_workspace_root(roots[0], roots), base / "proj")

    def test_nested_generic_default_collapses_to_project(self) -> None:
        """Drop a generic default workspace name, keying on the grouping dir."""
        base, roots = self.build(["proj/master", "proj/ws1", "proj/ws2"])

        self.assertEqual(
            launcher.shared_workspace_root(base / "proj/ws1", roots), base / "proj"
        )

    def test_nested_shared_letter_prefix_backs_up_to_project(self) -> None:
        """Back up to the grouping dir when the common prefix cuts mid-component."""
        base, roots = self.build(["proj/master", "proj/main"])

        self.assertEqual(
            launcher.shared_workspace_root(base / "proj/main", roots), base / "proj"
        )

    def test_flat_bare_default_keeps_project(self) -> None:
        """Keep the bare project dir when flat siblings share it as a prefix."""
        base, roots = self.build(["proj", "proj-ws1", "proj-ws2"])

        self.assertEqual(
            launcher.shared_workspace_root(base / "proj-ws1", roots), base / "proj"
        )

    def test_flat_and_nested_share_across_workspaces(self) -> None:
        """Resolve every workspace of a repo to the same root."""
        base, roots = self.build(["proj", "proj-ws1", "proj-ws2"])

        resolved = {launcher.shared_workspace_root(root, roots) for root in roots}

        self.assertEqual(resolved, {base / "proj"})

    def test_flat_non_bare_default_collapses_to_parent(self) -> None:
        """Collapse to the container when no bare project dir anchors the prefix."""
        base, roots = self.build(["proj-main", "proj-ws1", "proj-ws2"])

        self.assertEqual(launcher.shared_workspace_root(base / "proj-ws1", roots), base)

    def test_cwd_in_workspace_subdir(self) -> None:
        """Resolve the shared root from a directory below a workspace root."""
        base, roots = self.build(["proj/master", "proj/ws1"])
        subdir = base / "proj/ws1/nested"
        subdir.mkdir()

        self.assertEqual(launcher.shared_workspace_root(subdir, roots), base / "proj")

    def test_scattered_workspaces_fall_back_to_cwd(self) -> None:
        """Give up sharing when the common prefix climbs far above the workspaces."""
        base, roots = self.build(["group/proj/master", "elsewhere/exp"])
        cwd = base / "group/proj/master"

        self.assertEqual(launcher.shared_workspace_root(cwd, roots), cwd)

    def test_guardrail_shares_one_level_above(self) -> None:
        """Share when the common prefix is exactly one level above the workspace."""
        base, roots = self.build(["a/b/ws1", "a/b/ws2"])

        self.assertEqual(
            launcher.shared_workspace_root(base / "a/b/ws1", roots), base / "a/b"
        )

    def test_guardrail_drops_two_levels_above(self) -> None:
        """Give up sharing when the common prefix is two levels above the workspace."""
        base, roots = self.build(["a/b/ws", "a/c/ws"])
        cwd = base / "a/b/ws"

        self.assertEqual(launcher.shared_workspace_root(cwd, roots), cwd)


class ScratchModeTests(TempDirTestCase):
    """Test detection of a throwaway launch directory."""

    def test_enabled_under_tmp(self) -> None:
        """Enter scratch mode when the launch dir is under /tmp."""
        module = load_launcher(cwd=self.make_temp_dir())

        self.assertTrue(module.SCRATCH_MODE)

    def test_disabled_outside_tmp(self) -> None:
        """Stay out of scratch mode for a launch dir outside /tmp."""
        module = load_launcher(cwd=self.make_persistent_temp_dir())

        self.assertFalse(module.SCRATCH_MODE)


class ExchangeDirPathTests(unittest.TestCase):
    """Test construction of the runtime exchange dir path."""

    def test_builds_name_from_last_two_parts(self) -> None:
        """Name the dir after the identity's last two components."""
        path = launcher.exchange_dir_path(
            Path("/run/user/1000"), Path("/home/x/Projets/proj")
        )

        self.assertEqual(path, Path("/run/user/1000/agent/projets-proj"))

    def test_lowercases_components(self) -> None:
        """Lowercase the identity components in the dir name."""
        path = launcher.exchange_dir_path(
            Path("/run/user/1000"), Path("/home/x/Projets/AgentSandbox")
        )

        self.assertEqual(path, Path("/run/user/1000/agent/projets-agentsandbox"))


class ClaudePathHashTests(unittest.TestCase):
    """Test the base36 path hash mirroring Claude Code's slug disambiguation."""

    def test_known_hashes(self) -> None:
        """Reproduce hashes computed by Claude Code's own algorithm."""
        self.assertEqual(
            launcher.claude_path_hash("/home/user/My Project (v2)!"), "gzll6s"
        )
        self.assertEqual(
            launcher.claude_path_hash("/srv/" + "nested/" * 40 + "repo"), "fn0i87"
        )

    def test_empty_path_hashes_to_zero(self) -> None:
        """Hash the empty path to the single zero digit."""
        self.assertEqual(launcher.claude_path_hash(""), "0")


class ClaudeProjectSlugTests(unittest.TestCase):
    """Test the per-project slug encoding mirroring Claude Code's LE()."""

    def test_replaces_non_alphanumeric_with_dash(self) -> None:
        """Map every non-alphanumeric character to a dash."""
        self.assertEqual(
            launcher.claude_project_slug(Path("/home/user/projects/my-repo")),
            "-home-user-projects-my-repo",
        )
        self.assertEqual(
            launcher.claude_project_slug(Path("/home/user/My Project (v2)!")),
            "-home-user-My-Project--v2--",
        )

    def test_long_path_truncated_with_hash_suffix(self) -> None:
        """Truncate slugs over the length cap and append the path hash."""
        slug = launcher.claude_project_slug(Path("/srv/" + "nested/" * 40 + "repo"))

        self.assertEqual(len(slug), launcher.CLAUDE_SLUG_MAX_LEN + len("-fn0i87"))
        self.assertTrue(slug.startswith("-srv-nested-nested-"))
        self.assertTrue(slug.endswith("-fn0i87"))


class ClaudeMdSymlinksTests(TempDirTestCase):
    """Test the CLAUDE.md link pairs derived from the project's AGENTS.md files."""

    def make_agents_md(self, root: Path, relative: str) -> Path:
        """Create an AGENTS.md in a subdirectory of root, creating parents, and return it."""
        agents_md = root / relative / "AGENTS.md"
        agents_md.parent.mkdir(parents=True, exist_ok=True)
        agents_md.write_text("instructions")
        return agents_md

    def test_agents_md_through_max_depth(self) -> None:
        """Pair every AGENTS.md down to the maximum depth, and none below it."""
        cwd = self.make_temp_dir()
        paired = [
            self.make_agents_md(cwd, "."),
            self.make_agents_md(cwd, "docs"),
            self.make_agents_md(cwd, "src/lib"),
        ]
        self.make_agents_md(cwd, "src/lib/inner")

        self.assertEqual(
            dict(launcher.claude_md_symlinks(cwd)),
            {agents_md: agents_md.with_name("CLAUDE.md") for agents_md in paired},
        )

    def test_no_agents_md(self) -> None:
        """Pair nothing when the project holds no AGENTS.md."""
        cwd = self.make_temp_dir()
        (cwd / "docs").mkdir()

        self.assertEqual(launcher.claude_md_symlinks(cwd), ())

    def test_agents_md_path_types(self) -> None:
        """Pair a symlinked AGENTS.md file, and no path that is not a file."""
        cwd = self.make_temp_dir()
        (cwd / "instructions.md").write_text("instructions")
        linked = cwd / "linked/AGENTS.md"
        linked.parent.mkdir()
        linked.symlink_to("../instructions.md")
        (cwd / "directory/AGENTS.md").mkdir(parents=True)

        self.assertEqual(
            launcher.claude_md_symlinks(cwd),
            ((linked, linked.with_name("CLAUDE.md")),),
        )

    def test_existing_claude_md(self) -> None:
        """Skip only the directory already holding a CLAUDE.md, down to a dangling symlink."""
        cwd = self.make_temp_dir()
        self.make_agents_md(cwd, "docs")
        nested = self.make_agents_md(cwd, "docs/api")
        (cwd / "docs/CLAUDE.md").symlink_to(cwd / "docs/gone.md")

        self.assertEqual(
            launcher.claude_md_symlinks(cwd),
            ((nested, nested.with_name("CLAUDE.md")),),
        )

    def test_symlinked_dirs(self) -> None:
        """Skip symlinked subdirectories, whose links bwrap cannot create."""
        cwd = self.make_temp_dir()
        launch_dir_md = self.make_agents_md(cwd, ".")
        outside = self.make_agents_md(self.make_temp_dir(), "shared")
        (cwd / "linked").symlink_to(outside.parent)
        (cwd / "self").symlink_to(cwd)

        self.assertEqual(
            launcher.claude_md_symlinks(cwd), ((launch_dir_md, cwd / "CLAUDE.md"),)
        )

    def test_spec_uses_launch_dir(self) -> None:
        """Derive the claude spec symlinks from the launch directory."""
        cwd = self.make_temp_dir()
        agents_md = self.make_agents_md(cwd, "docs")

        module = load_launcher(cwd=cwd)

        self.assertEqual(
            module.AGENTS["claude"].symlinks, ((agents_md, cwd / "docs/CLAUDE.md"),)
        )

    def test_spec_of_another_entry_agent(self) -> None:
        """Leave the claude spec symlinks empty when another agent is launched."""
        cwd = self.make_temp_dir()
        self.make_agents_md(cwd, ".")

        module = load_launcher(cwd=cwd, agent="codex")

        self.assertEqual(module.AGENTS["claude"].symlinks, ())


class ConfirmCwdTests(TempDirTestCase):
    """Test startup directory confirmation."""

    def test_returns_cwd_outside_repo(self) -> None:
        """Keep the current directory when it is not inside a repository."""
        cwd = self.make_temp_dir()

        with contextlib.chdir(cwd):
            self.assertEqual(launcher.confirm_cwd(None), cwd)

    def test_refuses_home(self) -> None:
        """Exit when started directly in the home directory."""
        home = self.make_temp_dir()
        with (
            unittest.mock.patch.dict(os.environ, {"HOME": str(home)}),
            contextlib.chdir(home),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as ctx,
        ):
            launcher.confirm_cwd(None)

        self.assertEqual(ctx.exception.code, 1)

    def test_prompt_accepted_moves_to_repo_root(self) -> None:
        """Move to the repository root when the user accepts the prompt."""
        repo = self.make_temp_dir()
        subdir = repo / "nested"
        subdir.mkdir()

        with (
            contextlib.chdir(subdir),
            unittest.mock.patch("sys.stdin") as stdin,
            unittest.mock.patch("builtins.input", side_effect=["y"]),
        ):
            stdin.isatty.return_value = True
            self.assertEqual(
                launcher.confirm_cwd(launcher.RepoInfo(repo, None, None)), repo
            )

    def test_prompt_declined_stays_in_cwd(self) -> None:
        """Stay in the current directory when the prompt is declined."""
        repo = self.make_temp_dir()
        subdir = repo / "nested"
        subdir.mkdir()

        with (
            contextlib.chdir(subdir),
            unittest.mock.patch("sys.stdin") as stdin,
        ):
            stdin.isatty.return_value = False
            self.assertEqual(
                launcher.confirm_cwd(launcher.RepoInfo(repo, None, None)), subdir
            )


if __name__ == "__main__":
    unittest.main()
