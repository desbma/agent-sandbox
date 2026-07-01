"""Test sandbox-coding-agent helpers without starting bubblewrap."""

import atexit
import contextlib
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
) -> types.ModuleType:
    """Load the launcher script as a module under a controlled environment.

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
        sys.argv = [str(SCRIPT_PATH), "claude"]
        os.chdir(IMPORT_CWD)
        loader.exec_module(module)
    finally:
        sys.argv = saved_argv
        os.chdir(saved_cwd)
        os.environ.clear()
        os.environ.update(saved_environ)
    return module


launcher = load_launcher()
atexit.register(shutil.rmtree, FIXTURE_ROOT, ignore_errors=True)


class TempDirTestCase(unittest.TestCase):
    """Provide self-cleaning temporary directories and git repositories."""

    def make_temp_dir(self) -> Path:
        """Create a self-cleaning temporary directory."""
        path = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
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


class MountTests(unittest.TestCase):
    """Test the Mount dataclass."""

    def test_target_defaults_to_source(self) -> None:
        """Use the source path as target when no remap is given."""
        mount = launcher.Mount(Path("/src"), None, launcher.MountKind.BIND_RO)

        self.assertEqual(mount.target, Path("/src"))

    def test_target_uses_remap(self) -> None:
        """Use the remapped destination as target when given."""
        mount = launcher.Mount(Path("/src"), Path("/dst"), launcher.MountKind.BIND_RO)

        self.assertEqual(mount.target, Path("/dst"))


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

        lines = launcher.gen_passwd().decode().splitlines()

        self.assertEqual(lines[0], expected)

    def test_lists_only_current_and_group_users(self) -> None:
        """Emit only the current user plus the users named after copied groups."""
        expected_names = [pwd.getpwuid(os.getuid()).pw_name]
        for name in launcher.GROUPS:
            with contextlib.suppress(KeyError):
                expected_names.append(pwd.getpwnam(name).pw_name)

        lines = launcher.gen_passwd().decode().splitlines()

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

        lines = launcher.gen_group().decode().splitlines()

        self.assertEqual(lines[0], expected)

    def test_lists_only_current_and_copied_groups(self) -> None:
        """Emit only the primary group plus the groups copied from the host."""
        expected_names = [grp.getgrgid(os.getgid()).gr_name]
        for name in launcher.GROUPS:
            with contextlib.suppress(KeyError):
                expected_names.append(grp.getgrnam(name).gr_name)

        lines = launcher.gen_group().decode().splitlines()

        self.assertEqual([line.split(":")[0] for line in lines], expected_names)


class GenGlobalAgentsMdTests(unittest.TestCase):
    """Test generation of the agent's global instructions file."""

    def setUp(self) -> None:
        """Ensure the base AGENTS.md location exists and starts absent."""
        self.base_path = FAKE_CONFIG_HOME / "agents/AGENTS.md"
        self.base_path.parent.mkdir(parents=True, exist_ok=True)
        self.addCleanup(self.base_path.unlink, missing_ok=True)
        self.base_path.unlink(missing_ok=True)

    def render(
        self,
        exchange_dir: Path | None = None,
        *,
        has_unjaild: bool = False,
        has_proxy: bool = False,
        dir_mounts: dict[object, list[Path]] | None = None,
    ) -> str:
        """Generate the instructions file content with defaults for all knobs."""
        return launcher.gen_global_agents_md(
            exchange_dir,
            has_unjaild=has_unjaild,
            has_proxy=has_proxy,
            dir_mounts=dir_mounts or {},
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
        with_bullet = self.render(Path("/run/user/1000/exchange"))
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


class ProxySetupTests(unittest.TestCase):
    """Test detection of the auth-injecting proxy."""

    def test_returns_none_without_ca_bundle(self) -> None:
        """Detect no proxy when the CA bundle file is absent."""
        self.assertIsNone(launcher.proxy_setup())

    def test_returns_mount_and_env_with_ca_bundle(self) -> None:
        """Return the CA bundle mount and gh routing env when the proxy runs."""
        bundle = Path(launcher.PROXY_CA_BUNDLE)
        bundle.parent.mkdir(parents=True, exist_ok=True)
        bundle.write_text("FAKE CA")
        self.addCleanup(bundle.unlink)

        proxy = launcher.proxy_setup()

        assert proxy is not None
        mount, env = proxy
        self.assertEqual(mount.src, bundle)
        self.assertIsNone(mount.dst)
        self.assertEqual(mount.kind, launcher.MountKind.BIND_RO)
        self.assertEqual(
            env,
            {
                "GH_TOKEN": "agent-proxy-placeholder",
                "HTTPS_PROXY": "http://127.0.0.1:8085",
                "NO_PROXY": "127.0.0.1,localhost",
                "SSL_CERT_FILE": str(bundle),
            },
        )


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


class GetRepoRootTests(TempDirTestCase):
    """Test repository root discovery."""

    def test_returns_git_root_from_subdir(self) -> None:
        """Find the git repository root from a nested directory."""
        repo = self.make_git_repo()
        subdir = repo / "nested"
        subdir.mkdir()

        with contextlib.chdir(subdir):
            self.assertEqual(launcher.get_repo_root(), repo)

    def test_returns_none_outside_repo(self) -> None:
        """Find no root outside any repository."""
        with contextlib.chdir(self.make_temp_dir()):
            self.assertIsNone(launcher.get_repo_root())


class GetJjDefaultWsRootTests(TempDirTestCase):
    """Test jujutsu default workspace root discovery."""

    def test_returns_none_outside_workspace(self) -> None:
        """Find no workspace root outside any jj repository."""
        with contextlib.chdir(self.make_temp_dir()):
            self.assertIsNone(launcher.get_jj_default_ws_root())

    @unittest.skipUnless(Path("/usr/bin/jj").exists(), "jj is not installed")
    def test_returns_default_workspace_root(self) -> None:
        """Find the default workspace root inside a jj repository."""
        repo = self.make_temp_dir()
        # the absolute path bypasses the sandbox's read-only jj wrapper, which rejects init
        subprocess.run(
            ["/usr/bin/jj", "git", "init", str(repo)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        with contextlib.chdir(repo):
            self.assertEqual(launcher.get_jj_default_ws_root(), repo)


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


class ConfirmCwdTests(TempDirTestCase):
    """Test startup directory confirmation."""

    def test_returns_cwd_outside_repo(self) -> None:
        """Keep the current directory when it is not inside a repository."""
        cwd = self.make_temp_dir()

        with contextlib.chdir(cwd):
            self.assertEqual(launcher.confirm_cwd(), cwd)

    def test_refuses_home(self) -> None:
        """Exit when started directly in the home directory."""
        home = self.make_temp_dir()
        with (
            unittest.mock.patch.dict(os.environ, {"HOME": str(home)}),
            contextlib.chdir(home),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as ctx,
        ):
            launcher.confirm_cwd()

        self.assertEqual(ctx.exception.code, 1)

    def test_prompt_accepted_moves_to_repo_root(self) -> None:
        """Move to the repository root when the user accepts the prompt."""
        repo = self.make_git_repo()
        subdir = repo / "nested"
        subdir.mkdir()

        with (
            contextlib.chdir(subdir),
            unittest.mock.patch("sys.stdin") as stdin,
            unittest.mock.patch("builtins.input", side_effect=["y"]),
        ):
            stdin.isatty.return_value = True
            self.assertEqual(launcher.confirm_cwd(), repo)

    def test_prompt_declined_stays_in_cwd(self) -> None:
        """Stay in the current directory when the prompt is declined."""
        repo = self.make_git_repo()
        subdir = repo / "nested"
        subdir.mkdir()

        with (
            contextlib.chdir(subdir),
            unittest.mock.patch("sys.stdin") as stdin,
        ):
            stdin.isatty.return_value = False
            self.assertEqual(launcher.confirm_cwd(), subdir)


if __name__ == "__main__":
    unittest.main()
