"""Boot real microVMs to exercise networking, package install, and shares."""

import concurrent.futures
import importlib.machinery
import importlib.util
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from collections.abc import Sequence
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "agent-microvm"
SCRIPT_LOADER = importlib.machinery.SourceFileLoader("agent_microvm", str(SCRIPT_PATH))
SPEC = importlib.util.spec_from_loader("agent_microvm", SCRIPT_LOADER)
assert SPEC is not None
agent_microvm = importlib.util.module_from_spec(SPEC)
sys.modules["agent_microvm"] = agent_microvm
SCRIPT_LOADER.exec_module(agent_microvm)

INTEGRATION_ENV_VAR = "AGENT_MICROVM_INTEGRATION"
INTEGRATION_ENABLED = os.environ.get(INTEGRATION_ENV_VAR) is not None
SKIP_REASON = f"Set {INTEGRATION_ENV_VAR} to boot a real VM for these tests"
GITHUB_HOSTED_RUNNER = os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
# The warm-up boot builds all guest artifacts from scratch on a cold cache
# (downloads, rootfs assembly, squashfs); later per-command boots reuse them.
BUILD_TIMEOUT_SECONDS = 300.0
GUEST_RUN_TIMEOUT_SECONDS = 10.0
PEER_WAIT_TIMEOUT_SECONDS = 30.0
PEER_WAIT_INTERVAL_SECONDS = 0.2
APK_TEST_PACKAGE = "figlet"
PING_TARGET = "1.1.1.1"
DNS_TEST_HOSTNAME = "one.one.one.one"


def run_guest(
    command: Sequence[str],
    cwd: Path | None = None,
    timeout: float = GUEST_RUN_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Boot a throwaway VM, run command in the guest, and return its result.

    Guest stdout arrives on the returned stdout; the launcher's own progress
    logs stay on stderr.
    """
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), *command],
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )


def peer_wait_script(marker_path: Path, target_path: Path) -> str:
    """Return a shell script announcing a marker file then awaiting the peer's."""
    wait = (
        f"until [ -f {shlex.quote(str(target_path))} ]; "
        f"do sleep {PEER_WAIT_INTERVAL_SECONDS}; done"
    )
    return (
        f"echo ready > {shlex.quote(str(marker_path))}\n"
        f"timeout {int(PEER_WAIT_TIMEOUT_SECONDS)} sh -c {shlex.quote(wait)}\n"
    )


@unittest.skipUnless(INTEGRATION_ENABLED, SKIP_REASON)
class MicrovmIntegrationTests(unittest.TestCase):
    """Drive real VM boots through the agent-microvm launcher."""

    @classmethod
    def setUpClass(cls) -> None:
        """Build artifacts and confirm a trivial boot before the test bodies."""
        result = run_guest(("true",), timeout=BUILD_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise RuntimeError(f"Warm-up boot failed: {result.stderr}")

    def test_boots_and_reports_alpine_version(self) -> None:
        """Boot the guest and read its Alpine release, matching the pinned branch."""
        result = run_guest(("cat", "/etc/alpine-release"))

        self.assertEqual(result.returncode, 0)
        release = agent_microvm.ALPINE_BRANCH.removeprefix("v")
        self.assertTrue(result.stdout.strip().startswith(f"{release}."))

    def test_exit_code_propagates(self) -> None:
        """Propagate the guest command's exit status back to the launcher."""
        result = run_guest(("sh", "-c", "exit 7"))

        self.assertEqual(result.returncode, 7)

    def test_passwordless_sudo_is_root(self) -> None:
        """Run a command through passwordless sudo as uid 0."""
        result = run_guest(("sudo", "id", "-u"))

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), "0")

    @unittest.skipIf(
        GITHUB_HOSTED_RUNNER, "GitHub-hosted runners drop inbound ICMP echo replies"
    )
    def test_unprivileged_user_can_ping(self) -> None:
        """Ping a public address as the unprivileged user without a raw socket."""
        result = run_guest(("ping", "-c1", "-W3", PING_TARGET))

        self.assertEqual(result.returncode, 0)

    def test_guest_resolves_dns(self) -> None:
        """Resolve a hostname in the guest through the passt DNS forward."""
        result = run_guest(
            (
                "python3",
                "-c",
                f"import socket; print(socket.gethostbyname({DNS_TEST_HOSTNAME!r}))",
            )
        )

        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.strip())

    def test_apk_add_installs_and_runs_package(self) -> None:
        """Install a package at runtime with apk and run its binary."""
        script = f"sudo apk add --no-progress {APK_TEST_PACKAGE} >/dev/null && {APK_TEST_PACKAGE} hello"
        result = run_guest(("sh", "-c", script))

        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stdout.strip())

    def test_workspace_share_round_trips(self) -> None:
        """Read a host file from the workspace and write one back with host ownership."""
        with tempfile.TemporaryDirectory() as host_dir:
            host_path = Path(host_dir)
            (host_path / "input.txt").write_text("hello from host\n", encoding="utf-8")

            result = run_guest(
                ("sh", "-c", "cat workspace/input.txt > workspace/output.txt"),
                cwd=host_path,
            )

            self.assertEqual(result.returncode, 0)
            output_path = host_path / "output.txt"
            self.assertEqual(
                output_path.read_text(encoding="utf-8"), "hello from host\n"
            )
            self.assertEqual(output_path.stat().st_uid, os.getuid())

    def test_concurrent_vms_exchange_files(self) -> None:
        """Boot two VMs at once and have each read a file the other wrote."""
        subdir = agent_microvm.exchange_dir() / f"itest-{uuid.uuid4().hex}"
        subdir.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, subdir, ignore_errors=True)
        commands = [
            ("sh", "-c", peer_wait_script(subdir / "peer-a", subdir / "peer-b")),
            ("sh", "-c", peer_wait_script(subdir / "peer-b", subdir / "peer-a")),
        ]
        timeout = GUEST_RUN_TIMEOUT_SECONDS + PEER_WAIT_TIMEOUT_SECONDS
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(commands)) as pool:
            futures = [
                pool.submit(run_guest, command, timeout=timeout) for command in commands
            ]
            results = [future.result() for future in futures]

        for result in results:
            self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
