"""Test QEMU microvm prototype command and initramfs construction."""

import base64
import fcntl
import importlib.machinery
import importlib.util
import logging
import os
import stat
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import unittest.mock
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "agent-microvm"
SKILL_PATH = SCRIPT_PATH.parent / "SKILL.md"
SCRIPT_LOADER = importlib.machinery.SourceFileLoader("agent_microvm", str(SCRIPT_PATH))
SPEC = importlib.util.spec_from_loader("agent_microvm", SCRIPT_LOADER)
assert SPEC is not None
agent_microvm = importlib.util.module_from_spec(SPEC)
sys.modules["agent_microvm"] = agent_microvm
SCRIPT_LOADER.exec_module(agent_microvm)

KERNEL_PATH = Path("/images/alpine/vmlinuz-virt")
INITRAMFS_PATH = Path("/images/alpine/rootfs.cpio")
USR_SQUASHFS_PATH = Path("/images/alpine/usr.squashfs")
PASST_PATH = Path("/usr/bin/passt")
WORKSPACE_PATH = Path("/home/user/project")
RUNTIME_DIR = Path(os.environ["XDG_RUNTIME_DIR"]) / SCRIPT_PATH.name
WORKSPACE_SOCKET_PATH = RUNTIME_DIR / "workspace.sock"
CONSOLE_LOG_PATH = RUNTIME_DIR / "console.log"
SSH_PRIVATE_KEY_PATH = RUNTIME_DIR / "id_ed25519"
SSH_HOST_PORT = 49152
DNS_FORWARD_TARGET = "127.0.0.53"
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAExampleKeyData comment"
MEMORY_MIB = 1024
CPU_COUNT = 2
HOST_UID = 1000
HOST_GID = 1000
EXECUTABLE_MODE = 0o700
NEWC_MAGIC = b"070701"
NEWC_FIELD_WIDTH = 8
NEWC_HEADER_LEN = 110
NEWC_FIELD_COUNT = 13
CPIO_ALIGNMENT = 4
CPIO_TRAILER_NAME = "TRAILER!!!"


def align(size: int) -> int:
    """Round a size up to the cpio 4-byte alignment."""
    return size + (-size % CPIO_ALIGNMENT)


def read_newc(blob: bytes) -> list[tuple[str, int, int, int, bytes]]:
    """Parse a single uncompressed newc cpio into (name, mode, uid, mtime, data) tuples."""
    entries: list[tuple[str, int, int, int, bytes]] = []
    offset = 0
    while True:
        if blob[offset : offset + len(NEWC_MAGIC)] != NEWC_MAGIC:
            raise ValueError("Missing newc magic")
        base = offset + len(NEWC_MAGIC)
        width = NEWC_FIELD_WIDTH
        fields = [
            int(blob[base + index * width : base + (index + 1) * width], 16)
            for index in range(NEWC_FIELD_COUNT)
        ]
        filesize, namesize = fields[6], fields[11]
        name_start = offset + NEWC_HEADER_LEN
        name = blob[name_start : name_start + namesize - 1].decode("utf-8")
        data_start = align(name_start + namesize)
        data = blob[data_start : data_start + filesize]
        offset = align(data_start + filesize)
        if name == CPIO_TRAILER_NAME:
            return entries
        entries.append((name, fields[1], fields[2], fields[5], data))


def workspace_share(*, readonly: bool = False) -> agent_microvm.VirtiofsShare:
    """Build a workspace virtio-fs share for tests."""
    return agent_microvm.VirtiofsShare(
        tag=agent_microvm.WORKSPACE_SHARE_TAG,
        source=WORKSPACE_PATH,
        socket_path=WORKSPACE_SOCKET_PATH,
        readonly=readonly,
    )


def sample_microvm_config(
    public_key: str = PUBLIC_KEY, dns_forward_target: str | None = DNS_FORWARD_TARGET
) -> agent_microvm.MicrovmConfig:
    """Build a microvm configuration for command-builder tests."""
    return agent_microvm.MicrovmConfig(
        kernel_path=KERNEL_PATH,
        initramfs_path=INITRAMFS_PATH,
        usr_squashfs_path=USR_SQUASHFS_PATH,
        passt_path=PASST_PATH,
        dns_forward_target=dns_forward_target,
        memory_mib=MEMORY_MIB,
        cpu_count=CPU_COUNT,
        shares=(workspace_share(),),
        ssh_host_port=SSH_HOST_PORT,
        ssh_public_key=public_key,
        console_log_path=CONSOLE_LOG_PATH,
    )


class MicrovmCommandTests(unittest.TestCase):
    """Test microvm command builders."""

    def tearDown(self) -> None:
        """Reset the root logging configuration mutated by main()."""
        root = logging.getLogger()
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        root.setLevel(logging.WARNING)

    def test_builds_qemu_microvm_command(self) -> None:
        """Build a QEMU microvm command booting a RAM initramfs with a virtio-fs share."""
        share = workspace_share()
        config = sample_microvm_config()

        command = agent_microvm.build_qemu_command(config)
        machine_value = command[command.index("-M") + 1]
        append_value = command[command.index("-append") + 1]

        self.assertEqual(command[0], "qemu-system-x86_64")
        self.assertIn("microvm", machine_value)
        self.assertIn("accel=kvm", machine_value)
        self.assertIn("memory-backend=mem", machine_value)
        self.assertEqual(command[command.index("-kernel") + 1], str(KERNEL_PATH))
        self.assertEqual(command[command.index("-initrd") + 1], str(INITRAMFS_PATH))
        self.assertIn("console=ttyS0", append_value)
        self.assertIn(
            f"file={USR_SQUASHFS_PATH},if=none,id=usr,format=raw,readonly=on", command
        )
        self.assertIn("virtio-blk-device,drive=usr", command)
        self.assertTrue(any("memory-backend-memfd" in part for part in command))
        self.assertIn(f"socket,id=char-{share.tag},path={share.socket_path}", command)
        self.assertIn(
            f"vhost-user-fs-device,chardev=char-{share.tag},tag={share.tag}", command
        )
        self.assertIn("virtio-net-device,netdev=net0", command)

    def test_qemu_command_logs_console_to_a_file(self) -> None:
        """Send the guest serial console to a log file instead of stdio."""
        command = agent_microvm.build_qemu_command(sample_microvm_config())

        self.assertEqual(
            command[command.index("-serial") + 1], f"file:{CONSOLE_LOG_PATH}"
        )
        self.assertNotIn("stdio", command)

    def test_qemu_command_forwards_host_port_to_guest_sshd(self) -> None:
        """Forward the host SSH port to the guest sshd through the passt netdev."""
        command = agent_microvm.build_qemu_command(sample_microvm_config())
        forward = f"127.0.0.1/{SSH_HOST_PORT}:{agent_microvm.GUEST_SSH_TCP_PORT}"
        netdev = command[command.index("-netdev") + 1]

        self.assertTrue(
            netdev.startswith(
                f"passt,id=net0,path={PASST_PATH},quiet=on,tcp-ports={forward}"
            )
        )
        self.assertFalse(any("virtio-serial" in part for part in command))

    def test_qemu_command_forwards_guest_dns_to_host_resolver(self) -> None:
        """Advertise the sentinel resolver and forward it to the host nameserver."""
        command = agent_microvm.build_qemu_command(
            sample_microvm_config(dns_forward_target="10.0.0.53")
        )
        netdev = command[command.index("-netdev") + 1]

        self.assertIn(f"dns={agent_microvm.GUEST_DNS_FORWARD_ADDR}", netdev)
        self.assertIn(f"dns-forward={agent_microvm.GUEST_DNS_FORWARD_ADDR}", netdev)
        self.assertIn("dns-host=10.0.0.53", netdev)

    def test_qemu_command_falls_back_to_public_dns_without_host_resolver(self) -> None:
        """Advertise a public resolver when the host exposes no nameserver."""
        command = agent_microvm.build_qemu_command(
            sample_microvm_config(dns_forward_target=None)
        )
        netdev = command[command.index("-netdev") + 1]

        self.assertIn(f"dns={agent_microvm.GUEST_DNS_FALLBACK}", netdev)
        self.assertNotIn("dns-forward=", netdev)
        self.assertNotIn("dns-host=", netdev)

    def test_qemu_command_embeds_ssh_public_key_in_kernel_cmdline(self) -> None:
        """Embed the SSH public key in the kernel cmdline as a space-free base64 value."""
        command = agent_microvm.build_qemu_command(sample_microvm_config())
        append_value = command[command.index("-append") + 1]
        prefix = f"{agent_microvm.GUEST_PUBKEY_CMDLINE_KEY}="
        encoded = next(part for part in append_value.split() if part.startswith(prefix))
        decoded = base64.b64decode(encoded.removeprefix(prefix)).decode("utf-8")

        self.assertEqual(decoded, PUBLIC_KEY)
        self.assertNotIn(" ", encoded)

    def test_builds_ssh_command(self) -> None:
        """Build an ssh command targeting the forwarded host port with the session key."""
        session = agent_microvm.SshSession(
            private_key_path=SSH_PRIVATE_KEY_PATH,
            host_port=SSH_HOST_PORT,
            command=("htop",),
            allocate_pty=True,
            environment=(("LANG", "en_US.UTF-8"),),
        )

        command = agent_microvm.build_ssh_command(session)
        remote = command[-1]

        self.assertEqual(command[0], "ssh")
        self.assertEqual(command[command.index("-i") + 1], str(SSH_PRIVATE_KEY_PATH))
        self.assertEqual(command[command.index("-p") + 1], str(SSH_HOST_PORT))
        self.assertIn("StrictHostKeyChecking=no", command)
        self.assertIn("UserKnownHostsFile=/dev/null", command)
        self.assertNotIn("ProxyCommand", " ".join(command))
        self.assertIn("-t", command)
        self.assertIn(f"{agent_microvm.GUEST_USER_NAME}@127.0.0.1", command)
        self.assertIn("cd /home/user", remote)
        self.assertIn("export LANG=en_US.UTF-8", remote)
        self.assertIn("exec htop", remote)

    def test_ssh_command_omits_pty_when_not_a_tty(self) -> None:
        """Skip pseudo-terminal allocation when the local stdin is not a tty."""
        session = agent_microvm.SshSession(
            private_key_path=SSH_PRIVATE_KEY_PATH,
            host_port=SSH_HOST_PORT,
            command=(),
            allocate_pty=False,
            environment=(),
        )

        command = agent_microvm.build_ssh_command(session)

        self.assertNotIn("-t", command)

    def test_build_remote_command_runs_command_with_env(self) -> None:
        """Wrap a guest command with a workspace chdir and quoted environment exports."""
        remote = agent_microvm.build_remote_command(
            ("bash", "-c", "echo hi"), (("LANG", "with space"),)
        )

        self.assertIn("cd /home/user", remote)
        self.assertIn("export LANG='with space'", remote)
        self.assertIn("exec bash -c 'echo hi'", remote)

    def test_build_remote_command_sources_profile_before_commands(self) -> None:
        """Load /etc/profile before guest commands so PATH includes sbin directories."""
        remote = agent_microvm.build_remote_command(("ifconfig",), ())

        self.assertIn(". /etc/profile", remote)
        self.assertIn("exec ifconfig", remote)
        self.assertLess(remote.index(". /etc/profile"), remote.index("exec ifconfig"))
        self.assertNotIn("/bin/bash -lc", remote)

    def test_build_remote_command_starts_login_shell(self) -> None:
        """Start the configured login shell when no guest command is requested."""
        remote = agent_microvm.build_remote_command((), ())

        self.assertIn("cd /home/user", remote)
        self.assertIn("exec /bin/bash -l", remote)

    def test_find_free_tcp_port_returns_a_bindable_port(self) -> None:
        """Return a positive TCP port on the SSH forward address."""
        port = agent_microvm.find_free_tcp_port()

        self.assertGreater(port, 0)

    def test_generate_ssh_keypair_reads_public_key(self) -> None:
        """Generate an ed25519 keypair and return its private path and public key text."""

        def fake_keygen(command: tuple[str, ...], **_kwargs: object) -> None:
            """Write a fake keypair where ssh-keygen would."""
            key_path = Path(command[command.index("-f") + 1])
            key_path.write_text("PRIVATE", encoding="utf-8")
            key_path.with_suffix(".pub").write_text(f"{PUBLIC_KEY}\n", encoding="utf-8")

        with tempfile.TemporaryDirectory() as temporary_directory:
            runtime_path = Path(temporary_directory)
            with unittest.mock.patch.object(
                agent_microvm.subprocess, "run", side_effect=fake_keygen
            ):
                private_key_path, public_key = agent_microvm.generate_ssh_keypair(
                    runtime_path
                )

            self.assertEqual(private_key_path, runtime_path / "id_ed25519")
            self.assertEqual(public_key, PUBLIC_KEY)

    def test_forwarded_environment_selects_locale_variables(self) -> None:
        """Forward only the configured locale environment variables that are set."""
        with unittest.mock.patch.dict(
            os.environ, {"LANG": "en_US.UTF-8", "LC_ALL": "C"}, clear=True
        ):
            self.assertEqual(
                agent_microvm.forwarded_environment(),
                (("LANG", "en_US.UTF-8"), ("LC_ALL", "C")),
            )

    def test_builds_rootless_virtiofsd_command(self) -> None:
        """Build a rootless Rust virtiofsd command mapping guest root to the host user."""
        command = agent_microvm.build_virtiofsd_command(
            virtiofsd_path=Path("/usr/lib/virtiofsd"),
            share=workspace_share(readonly=True),
            host_uid=HOST_UID,
            host_gid=HOST_GID,
        )

        self.assertEqual(command[0], "/usr/lib/virtiofsd")
        self.assertIn("--shared-dir", command)
        self.assertIn(str(WORKSPACE_PATH), command)
        self.assertIn("--socket-path", command)
        self.assertIn(str(WORKSPACE_SOCKET_PATH), command)
        self.assertIn("--readonly", command)
        self.assertIn("--sandbox=namespace", command)
        self.assertIn(f"--uid-map=:0:{HOST_UID}:1:", command)
        self.assertIn(f"--gid-map=:0:{HOST_GID}:1:", command)
        self.assertIn(
            f"--translate-uid=map:{agent_microvm.GUEST_USER_UID}:0:1", command
        )
        self.assertIn(
            f"--translate-gid=map:{agent_microvm.GUEST_USER_UID}:0:1", command
        )

    def test_install_alpine_packages_runs_apk_inside_rootfs(self) -> None:
        """Install configured Alpine packages with persistent repository indexes."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            tree = Path(temporary_directory) / "tree"
            tree.mkdir()

            with unittest.mock.patch.object(agent_microvm.subprocess, "run") as run:
                agent_microvm.install_alpine_packages(tree)

            repositories = (tree / "etc/apk/repositories").read_text(encoding="utf-8")
            commands = [call.args[0] for call in run.call_args_list]

            self.assertEqual(
                repositories, "\n".join(agent_microvm.ALPINE_REPOSITORY_URLS) + "\n"
            )
            self.assertEqual(len(commands), 3)
            add_index = commands[1].index("add")
            self.assertEqual(commands[0][-1], "update")
            self.assertEqual(
                commands[1][add_index : add_index + 3],
                ["add", "--usermode", "--initdb"],
            )
            self.assertEqual(
                commands[1][add_index + 3 :], list(agent_microvm.ALPINE_PACKAGES)
            )
            self.assertEqual(commands[2][-2:], ["upgrade", "--available"])
            self.assertNotIn("--usermode", commands[0])
            self.assertNotIn("--usermode", commands[2])
            for command in commands:
                self.assertEqual(command[0], str(tree / "lib/ld-musl-x86_64.so.1"))
                self.assertEqual(command[command.index("--root") + 1], str(tree))
                self.assertNotIn("--no-cache", command)
            for call in run.call_args_list:
                self.assertEqual(
                    call.kwargs,
                    {
                        "check": True,
                        "stdin": agent_microvm.subprocess.DEVNULL,
                        "stdout": agent_microvm.subprocess.DEVNULL,
                        "stderr": agent_microvm.subprocess.STDOUT,
                    },
                )

    def test_prepare_guest_builds_artifacts_in_order(self) -> None:
        """Seed the rootfs, then split /usr and pack the initramfs and squashfs in order."""
        events: list[str] = []

        def seed_rootfs(_archive: Path, tree: Path) -> None:
            """Populate the rootfs with the /usr subtree and account databases."""
            (tree / "usr").mkdir()
            for database in ("passwd", "group", "shadow"):
                path = tree / "etc" / database
                path.parent.mkdir(exist_ok=True)
                path.write_text("root:x:0:0:root:/root:/bin/ash\n", encoding="utf-8")
            events.append("seed")

        def install_packages(_tree: Path) -> None:
            """Record package installation."""
            events.append("install")

        def pack_initramfs(_tree: Path) -> bytes:
            """Record initramfs packing."""
            events.append("pack")
            return b"cpio"

        def build_squashfs(_usr_tree: Path, output_path: Path) -> None:
            """Record squashfs build."""
            events.append("squash")
            output_path.write_bytes(b"squashfs")

        def download(_url: str, destination: Path) -> None:
            """Create a placeholder file at the download destination."""
            destination.write_bytes(b"artifact")

        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_dir = Path(temporary_directory)
            artifacts = agent_microvm.GuestArtifacts(
                kernel_path=artifact_dir / "vmlinuz-virt",
                initramfs_path=artifact_dir / "rootfs.cpio",
                usr_squashfs_path=artifact_dir / "usr.squashfs",
            )

            with (
                unittest.mock.patch.object(
                    agent_microvm, "download_file", side_effect=download
                ),
                unittest.mock.patch.object(
                    agent_microvm, "extract_minirootfs", side_effect=seed_rootfs
                ),
                unittest.mock.patch.object(agent_microvm, "extract_modloop"),
                unittest.mock.patch.object(
                    agent_microvm,
                    "install_alpine_packages",
                    side_effect=install_packages,
                ),
                unittest.mock.patch.object(
                    agent_microvm,
                    "pack_directory_initramfs",
                    side_effect=pack_initramfs,
                ),
                unittest.mock.patch.object(
                    agent_microvm,
                    "build_usr_squashfs",
                    side_effect=build_squashfs,
                ),
            ):
                agent_microvm.prepare_guest(artifacts)

            self.assertEqual(events, ["seed", "install", "pack", "squash"])
            self.assertEqual(artifacts.kernel_path.read_bytes(), b"artifact")
            self.assertEqual(artifacts.initramfs_path.read_bytes(), b"cpio")
            self.assertEqual(artifacts.usr_squashfs_path.read_bytes(), b"squashfs")
            self.assertEqual(
                artifacts.prepare_marker_path.read_text(encoding="utf-8"),
                f"{agent_microvm.PREPARE_RECIPE_VERSION}\n",
            )
            self.assertFalse(agent_microvm.staging_path(artifacts.kernel_path).exists())

    def test_ensure_artifacts_builds_once_under_concurrency(self) -> None:
        """Serialize racing preparers so a stale rootfs is regenerated a single time."""
        builds = 0
        builds_lock = threading.Lock()

        def seed_rootfs(_archive: Path, tree: Path) -> None:
            """Populate the rootfs with the /usr subtree and account databases."""
            (tree / "usr").mkdir()
            for database in ("passwd", "group", "shadow"):
                path = tree / "etc" / database
                path.parent.mkdir(exist_ok=True)
                path.write_text("root:x:0:0:root:/root:/bin/ash\n", encoding="utf-8")

        def download(_url: str, destination: Path) -> None:
            """Create a placeholder file at the download destination."""
            destination.write_bytes(b"artifact")

        def pack_initramfs(_tree: Path) -> bytes:
            """Count the build and widen the race window."""
            nonlocal builds
            with builds_lock:
                builds += 1
            time.sleep(0.05)
            return b"cpio"

        def build_squashfs(_usr_tree: Path, output_path: Path) -> None:
            """Emit a placeholder squashfs."""
            output_path.write_bytes(b"squashfs")

        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_dir = Path(temporary_directory)
            artifacts = agent_microvm.GuestArtifacts(
                kernel_path=artifact_dir / "vmlinuz-virt",
                initramfs_path=artifact_dir / "rootfs.cpio",
                usr_squashfs_path=artifact_dir / "usr.squashfs",
            )
            barrier = threading.Barrier(2)
            failures: list[BaseException] = []

            def prepare() -> None:
                """Race a single preparer through the shared build lock."""
                barrier.wait()
                try:
                    agent_microvm.ensure_artifacts(artifacts)
                except BaseException as error:  # noqa: BLE001
                    failures.append(error)

            with (
                unittest.mock.patch.object(
                    agent_microvm, "download_file", side_effect=download
                ),
                unittest.mock.patch.object(
                    agent_microvm, "extract_minirootfs", side_effect=seed_rootfs
                ),
                unittest.mock.patch.object(agent_microvm, "extract_modloop"),
                unittest.mock.patch.object(agent_microvm, "install_alpine_packages"),
                unittest.mock.patch.object(
                    agent_microvm,
                    "pack_directory_initramfs",
                    side_effect=pack_initramfs,
                ),
                unittest.mock.patch.object(
                    agent_microvm, "build_usr_squashfs", side_effect=build_squashfs
                ),
            ):
                threads = [threading.Thread(target=prepare) for _ in range(2)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(failures, [])
            self.assertEqual(builds, 1)
            self.assertEqual(artifacts.initramfs_path.read_bytes(), b"cpio")
            self.assertEqual(artifacts.usr_squashfs_path.read_bytes(), b"squashfs")
            self.assertEqual(
                artifacts.prepare_marker_path.read_text(encoding="utf-8"),
                f"{agent_microvm.PREPARE_RECIPE_VERSION}\n",
            )

    def test_build_usr_squashfs_runs_mksquashfs_with_zstd_and_root(self) -> None:
        """Pack the /usr tree with mksquashfs, zstd compression, and root ownership."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            parent = Path(temporary_directory)
            usr_tree = parent / "usr-tree"
            usr_tree.mkdir()
            output_path = parent / "usr.squashfs"

            with unittest.mock.patch.object(agent_microvm.subprocess, "run") as run:
                agent_microvm.build_usr_squashfs(usr_tree, output_path)

            command = run.call_args.args[0]
            self.assertEqual(command[0], "mksquashfs")
            self.assertEqual(command[1], str(usr_tree))
            self.assertEqual(command[2], str(output_path))
            self.assertIn("-comp", command)
            self.assertEqual(command[command.index("-comp") + 1], "zstd")
            self.assertIn("-all-root", command)
            self.assertIn("-noappend", command)

    def test_set_guest_root_shell_rewrites_only_root(self) -> None:
        """Set the root login shell to bash while leaving other accounts untouched."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            tree = Path(temporary_directory)
            passwd_path = tree / "etc/passwd"
            passwd_path.parent.mkdir(parents=True)
            passwd_path.write_text(
                "root:x:0:0:root:/root:/bin/ash\nuser:x:1000:1000::/home/user:/bin/sh\n",
                encoding="utf-8",
            )

            agent_microvm.set_guest_root_shell(tree)
            lines = passwd_path.read_text(encoding="utf-8").splitlines()

            self.assertTrue(lines[0].endswith(":/bin/bash"))
            self.assertTrue(lines[1].endswith(":/bin/sh"))

    def test_guest_account_and_sudoers_setup(self) -> None:
        """Append the unprivileged user account and write its passwordless sudo drop-in."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            tree = Path(temporary_directory)
            for relative_path in ("etc/passwd", "etc/group", "etc/shadow"):
                path = tree / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("root:x:0:0:root:/root:/bin/ash\n", encoding="utf-8")

            agent_microvm.append_guest_account(tree)
            agent_microvm.write_guest_sudoers(tree)

            passwd = (tree / "etc/passwd").read_text(encoding="utf-8").splitlines()
            group = (tree / "etc/group").read_text(encoding="utf-8").splitlines()
            sudoers_path = tree / "etc/sudoers.d" / agent_microvm.GUEST_USER_NAME

            self.assertEqual(
                passwd[-1],
                f"{agent_microvm.GUEST_USER_NAME}:x:{agent_microvm.GUEST_USER_UID}:"
                f"{agent_microvm.GUEST_USER_UID}:{agent_microvm.GUEST_USER_NAME}:"
                f"{agent_microvm.GUEST_USER_HOME}:/bin/bash",
            )
            self.assertTrue(group[-1].startswith(f"{agent_microvm.GUEST_USER_NAME}:x:"))
            self.assertEqual(
                sudoers_path.read_text(encoding="utf-8"),
                f"{agent_microvm.GUEST_USER_NAME} ALL=(ALL) NOPASSWD: ALL\n",
            )
            self.assertEqual(stat.S_IMODE(sudoers_path.stat().st_mode), 0o440)

    def test_install_guest_bash_prompt_writes_drop_in(self) -> None:
        """Pin the interactive bash prompt with a /etc/bash drop-in."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            tree = Path(temporary_directory)

            agent_microvm.install_guest_bash_prompt(tree)

            prompt_path = tree / agent_microvm.GUEST_BASH_PROMPT_PATH
            self.assertEqual(
                prompt_path.read_text(encoding="utf-8"),
                f"PS1='{agent_microvm.GUEST_BASH_PROMPT}'\n",
            )
            self.assertEqual(stat.S_IMODE(prompt_path.stat().st_mode), 0o644)

    def test_default_artifacts_paths(self) -> None:
        """Resolve default Alpine artifact filenames under the cache directory."""
        artifacts = agent_microvm.default_artifacts()

        self.assertEqual(artifacts.kernel_path.name, "vmlinuz-virt")
        self.assertEqual(artifacts.initramfs_path.name, "rootfs.cpio")
        self.assertEqual(artifacts.usr_squashfs_path.name, "usr.squashfs")

    def test_packs_directory_tree_into_uncompressed_cpio(self) -> None:
        """Pack a rootfs tree into one root-owned uncompressed newc cpio."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            tree = Path(temporary_directory) / "tree"
            (tree / "bin").mkdir(parents=True)
            (tree / "bin" / "hello.txt").write_text("hi", encoding="utf-8")
            (tree / "link").symlink_to("bin/hello.txt")
            init_path = tree / "init"
            init_path.write_text(agent_microvm.guest_init_script(), encoding="utf-8")
            init_path.chmod(0o755)

            blob = agent_microvm.pack_directory_initramfs(tree)
            entries = {
                name: (mode, uid, mtime, data)
                for name, mode, uid, mtime, data in read_newc(blob)
            }

            self.assertEqual(blob[: len(NEWC_MAGIC)], NEWC_MAGIC)
            self.assertEqual(blob.count(CPIO_TRAILER_NAME.encode("ascii")), 1)
            self.assertTrue(stat.S_ISDIR(entries["bin"][0]))
            file_mode, file_uid, file_mtime, file_data = entries["bin/hello.txt"]
            self.assertTrue(stat.S_ISREG(file_mode))
            self.assertEqual(file_uid, 0)
            self.assertEqual(file_mtime, 0)
            self.assertEqual(file_data, b"hi")
            self.assertTrue(stat.S_ISLNK(entries["link"][0]))
            self.assertEqual(entries["link"][3], b"bin/hello.txt")
            self.assertTrue(entries["init"][0] & stat.S_IXUSR)
            self.assertIn(agent_microvm.GUEST_READY_MARKER.encode(), entries["init"][3])

    def test_minirootfs_filter_drops_devices_keeps_absolute_symlinks(self) -> None:
        """Drop device members and preserve absolute symlinks during extraction."""
        device = tarfile.TarInfo("dev/null")
        device.type = tarfile.CHRTYPE
        symlink = tarfile.TarInfo("usr/bin/yes")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "/bin/busybox"

        kept = agent_microvm.minirootfs_tar_filter(symlink, ".")

        self.assertIsNone(agent_microvm.minirootfs_tar_filter(device, "."))
        assert isinstance(kept, tarfile.TarInfo)
        self.assertEqual(kept.linkname, "/bin/busybox")

    def test_resolve_executable_finds_command_on_path(self) -> None:
        """Resolve an executable present on PATH."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            binary = Path(temporary_directory) / "passt"
            binary.write_text("", encoding="utf-8")
            binary.chmod(EXECUTABLE_MODE)

            with unittest.mock.patch.dict(os.environ, {"PATH": temporary_directory}):
                self.assertEqual(agent_microvm.resolve_executable("passt"), binary)

    def test_resolve_executable_raises_when_missing(self) -> None:
        """Raise FileNotFoundError when the executable is on neither PATH nor /usr/lib."""
        with (
            unittest.mock.patch.dict(os.environ, {"PATH": ""}),
            self.assertRaises(FileNotFoundError),
        ):
            agent_microvm.resolve_executable("definitely-missing-executable-xyz")

    def host_dns_server_for(self, resolv_conf: str) -> str | None:
        """Run host_dns_server against a temporary resolver configuration."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "resolv.conf"
            path.write_text(resolv_conf, encoding="utf-8")
            with unittest.mock.patch.object(agent_microvm, "HOST_RESOLV_CONF", path):
                return agent_microvm.host_dns_server()

    def test_host_dns_server_returns_first_ipv4_nameserver(self) -> None:
        """Skip comments and IPv6 entries and return the first IPv4 nameserver."""
        resolv_conf = (
            "# managed by systemd-resolved\n"
            "nameserver 2001:db8::1\n"
            "nameserver 127.0.0.53\n"
            "nameserver 1.1.1.1\n"
            "search example.test\n"
        )

        self.assertEqual(self.host_dns_server_for(resolv_conf), "127.0.0.53")

    def test_host_dns_server_none_without_ipv4_nameserver(self) -> None:
        """Return None when the resolver configuration lists no IPv4 nameserver."""
        self.assertIsNone(self.host_dns_server_for("search example.test\n"))

    def test_host_dns_server_none_when_file_missing(self) -> None:
        """Return None when the host resolver configuration is absent."""
        missing = Path("/nonexistent") / "resolv.conf"
        with unittest.mock.patch.object(agent_microvm, "HOST_RESOLV_CONF", missing):
            self.assertIsNone(agent_microvm.host_dns_server())

    def current_artifacts(self, artifact_dir: Path) -> agent_microvm.GuestArtifacts:
        """Build artifacts with present files and a current, fresh recipe marker."""
        artifacts = agent_microvm.GuestArtifacts(
            kernel_path=artifact_dir / "vmlinuz-virt",
            initramfs_path=artifact_dir / "rootfs.cpio",
            usr_squashfs_path=artifact_dir / "usr.squashfs",
        )
        artifacts.kernel_path.write_bytes(b"")
        artifacts.initramfs_path.write_bytes(b"")
        artifacts.usr_squashfs_path.write_bytes(b"")
        artifacts.prepare_marker_path.write_text(
            f"{agent_microvm.PREPARE_RECIPE_VERSION}\n", encoding="utf-8"
        )
        return artifacts

    def test_artifacts_not_current_when_files_missing(self) -> None:
        """Report artifacts as not current when required files are missing."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifacts = agent_microvm.GuestArtifacts(
                kernel_path=Path(temporary_directory) / "vmlinuz-virt",
                initramfs_path=Path(temporary_directory) / "rootfs.cpio",
                usr_squashfs_path=Path(temporary_directory) / "usr.squashfs",
            )

            self.assertFalse(agent_microvm.artifacts_are_current(artifacts))

    def test_artifacts_not_current_when_recipe_marker_mismatches(self) -> None:
        """Report artifacts as not current when the recipe marker version differs."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifacts = self.current_artifacts(Path(temporary_directory))
            artifacts.prepare_marker_path.write_text("old\n", encoding="utf-8")

            self.assertFalse(agent_microvm.artifacts_are_current(artifacts))

    def test_artifacts_not_current_when_recipe_marker_is_too_old(self) -> None:
        """Report artifacts as not current when the marker exceeds the rebuild interval."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifacts = self.current_artifacts(Path(temporary_directory))
            stale_mtime = agent_microvm.time.time() - 8 * 24 * 60 * 60
            os.utime(artifacts.prepare_marker_path, (stale_mtime, stale_mtime))

            self.assertFalse(agent_microvm.artifacts_are_current(artifacts))

    def test_rootfs_state_rebuild_interval_is_seven_days(self) -> None:
        """Use a one-week rebuild interval for prepared guest artifacts."""
        self.assertEqual(
            agent_microvm.ROOTFS_STATE_MAX_AGE_SECONDS,
            7 * 24 * 60 * 60,
        )

    def test_artifacts_not_current_when_usr_squashfs_is_missing(self) -> None:
        """Report artifacts as not current when the /usr squashfs has not been built."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifacts = self.current_artifacts(Path(temporary_directory))
            artifacts.usr_squashfs_path.unlink()

            self.assertFalse(agent_microvm.artifacts_are_current(artifacts))

    def test_artifacts_current_with_present_files_and_fresh_marker(self) -> None:
        """Report artifacts as current when files exist and the recipe marker is fresh."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifacts = self.current_artifacts(Path(temporary_directory))

            self.assertTrue(agent_microvm.artifacts_are_current(artifacts))

    def test_prune_removes_stale_version_directories(self) -> None:
        """Delete cached artifact directories of other Alpine versions."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_root = Path(temporary_directory)
            current_dir = cache_root / "alpine-v3.99"
            current_dir.mkdir()
            artifacts = self.current_artifacts(current_dir)
            stale_dir = cache_root / "alpine-v3.98"
            stale_dir.mkdir()
            (stale_dir / "usr.squashfs").write_bytes(b"")
            unrelated_dir = cache_root / "other"
            unrelated_dir.mkdir()

            agent_microvm.prune_stale_artifacts(artifacts)

            self.assertFalse(stale_dir.exists())
            self.assertTrue(current_dir.exists())
            self.assertTrue(unrelated_dir.exists())

    def test_prune_keeps_directory_with_held_build_lock(self) -> None:
        """Keep a stale directory while another process holds its build lock."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_root = Path(temporary_directory)
            current_dir = cache_root / "alpine-v3.99"
            current_dir.mkdir()
            artifacts = self.current_artifacts(current_dir)
            stale_dir = cache_root / "alpine-v3.98"
            stale_dir.mkdir()
            stale_artifacts = agent_microvm.GuestArtifacts(
                kernel_path=stale_dir / "vmlinuz-virt",
                initramfs_path=stale_dir / "rootfs.cpio",
                usr_squashfs_path=stale_dir / "usr.squashfs",
            )

            with stale_artifacts.lock_path.open("w") as holder:
                fcntl.flock(holder, fcntl.LOCK_EX)
                agent_microvm.prune_stale_artifacts(artifacts)
                self.assertTrue(stale_dir.exists())

            agent_microvm.prune_stale_artifacts(artifacts)
            self.assertFalse(stale_dir.exists())

    def test_prune_keeps_directory_with_live_session(self) -> None:
        """Keep a stale directory while a session holds its shared session lock."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_root = Path(temporary_directory)
            current_dir = cache_root / "alpine-v3.99"
            current_dir.mkdir()
            artifacts = self.current_artifacts(current_dir)
            stale_dir = cache_root / "alpine-v3.98"
            stale_dir.mkdir()
            stale_artifacts = agent_microvm.GuestArtifacts(
                kernel_path=stale_dir / "vmlinuz-virt",
                initramfs_path=stale_dir / "rootfs.cpio",
                usr_squashfs_path=stale_dir / "usr.squashfs",
            )

            with agent_microvm.artifact_session_lock(stale_artifacts):
                agent_microvm.prune_stale_artifacts(artifacts)
                self.assertTrue(stale_dir.exists())

            agent_microvm.prune_stale_artifacts(artifacts)
            self.assertFalse(stale_dir.exists())

    def test_session_lock_is_shared(self) -> None:
        """Admit concurrent sessions on the session lock but block exclusive takers."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifacts = self.current_artifacts(Path(temporary_directory))

            with agent_microvm.artifact_session_lock(artifacts):
                with artifacts.session_lock_path.open("w") as probe:
                    fcntl.flock(probe, fcntl.LOCK_SH | fcntl.LOCK_NB)
                with (
                    artifacts.session_lock_path.open("w") as probe,
                    self.assertRaises(BlockingIOError),
                ):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_run_microvm_prunes_and_holds_session_lock(self) -> None:
        """Prune stale versions and hold the session lock across the VM run."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            cache_root = Path(temporary_directory)
            current_dir = cache_root / "alpine-v3.99"
            current_dir.mkdir()
            artifacts = self.current_artifacts(current_dir)
            stale_dir = cache_root / "alpine-v3.98"
            stale_dir.mkdir()

            def probe_session_lock(**_kwargs: object) -> int:
                """Assert the session lock is held while the VM runs."""
                with (
                    artifacts.session_lock_path.open("w") as probe,
                    self.assertRaises(BlockingIOError),
                ):
                    fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return 0

            with (
                unittest.mock.patch.object(
                    agent_microvm, "default_artifacts", return_value=artifacts
                ),
                unittest.mock.patch.object(
                    agent_microvm,
                    "generate_ssh_keypair",
                    return_value=(SSH_PRIVATE_KEY_PATH, PUBLIC_KEY),
                ),
                unittest.mock.patch.object(
                    agent_microvm,
                    "resolve_executable",
                    side_effect=(Path("/usr/bin/passt"), Path("/usr/bin/virtiofsd")),
                ),
                unittest.mock.patch.object(
                    agent_microvm, "run_processes", side_effect=probe_session_lock
                ),
            ):
                self.assertEqual(agent_microvm.run_microvm(()), 0)

            self.assertFalse(stale_dir.exists())

    def test_guest_init_mounts_workspace_best_effort(self) -> None:
        """Generate guest init that mounts the workspace best-effort and powers off."""
        script = agent_microvm.guest_init_script()

        self.assertIn("mount -t virtiofs workspace /home/user/workspace", script)
        self.assertIn("modprobe virtiofs", script)
        self.assertIn("workspace mount failed", script)
        self.assertIn("poweroff -f", script)

    def test_guest_init_mounts_exchange_at_shared_path(self) -> None:
        """Generate guest init that mounts the exchange share at the shared host path."""
        script = agent_microvm.guest_init_script()
        mountpoint = agent_microvm.exchange_dir()

        self.assertIn(f"mkdir -p {mountpoint}", script)
        self.assertIn(f"mount -t virtiofs exchange {mountpoint}", script)
        self.assertIn("exchange mount failed", script)

    def test_guest_init_restores_sudo_setuid(self) -> None:
        """Generate guest init that restores the sudo setuid bit dropped by rootless apk."""
        script = agent_microvm.guest_init_script()

        self.assertIn("chmod u+s /usr/bin/sudo", script)

    def test_guest_init_enables_unprivileged_icmp(self) -> None:
        """Generate guest init that widens ping_group_range for unprivileged ICMP."""
        script = agent_microvm.guest_init_script()

        self.assertIn("/proc/sys/net/ipv4/ping_group_range", script)

    def test_guest_init_installs_ssh_key_and_starts_dropbear(self) -> None:
        """Generate guest init that injects the SSH key and serves dropbear over TCP."""
        script = agent_microvm.guest_init_script()

        self.assertIn("/proc/cmdline", script)
        self.assertIn(f"{agent_microvm.GUEST_PUBKEY_CMDLINE_KEY}=", script)
        self.assertIn("base64 -d", script)
        self.assertIn(str(agent_microvm.GUEST_AUTHORIZED_KEYS_PATH), script)
        self.assertIn(
            f"chown -R {agent_microvm.GUEST_USER_NAME}:{agent_microvm.GUEST_USER_NAME} "
            f"{agent_microvm.GUEST_USER_SSH_DIR}",
            script,
        )
        self.assertIn(
            f"chown {agent_microvm.GUEST_USER_NAME}:{agent_microvm.GUEST_USER_NAME} "
            f"{agent_microvm.GUEST_USER_HOME}",
            script,
        )
        self.assertIn(f"dropbearkey -t {agent_microvm.SSH_KEY_TYPE}", script)
        self.assertIn(
            f"dropbear -F -E -s -r /etc/dropbear/ed25519_host_key -p {agent_microvm.GUEST_SSH_TCP_PORT}",
            script,
        )

    def test_guest_init_starts_dropbear_before_ready_marker(self) -> None:
        """Launch dropbear before printing the marker the host waits on to connect."""
        script = agent_microvm.guest_init_script()

        dropbear_index = script.index(
            "dropbear -F -E -s -r /etc/dropbear/ed25519_host_key"
        )
        marker_index = script.index(f'echo "{agent_microvm.GUEST_READY_MARKER}"')

        self.assertLess(dropbear_index, marker_index)

    def test_guest_init_brings_up_dhcp_networking(self) -> None:
        """Generate guest init that loads virtio-net and configures the NIC over DHCP."""
        script = agent_microvm.guest_init_script()

        self.assertIn("modprobe virtio_net", script)
        self.assertIn("ifconfig eth0 up", script)
        self.assertIn("udhcpc -i eth0", script)
        self.assertIn("network setup failed", script)

    def test_guest_init_emits_boot_trace_lines(self) -> None:
        """Emit boot-trace lines at the usr, workspace, network, and dropbear phases."""
        script = agent_microvm.guest_init_script()

        for phase in (
            "mounts-ready",
            "usr-ready",
            "workspace-ready",
            "network-ready",
            "dropbear-launched",
        ):
            self.assertIn(f"boot_trace {phase}", script)
        self.assertIn(f'printf "{agent_microvm.GUEST_TRACE_PREFIX}', script)

    def test_guest_init_mounts_usr_overlay_from_squashfs(self) -> None:
        """Mount /dev/vda as squashfs and stack a tmpfs overlay on /usr."""
        script = agent_microvm.guest_init_script()

        self.assertIn("modprobe virtio_blk", script)
        self.assertIn("modprobe squashfs", script)
        self.assertIn("modprobe overlay", script)
        self.assertIn(
            f"mount -t squashfs -o ro /dev/vda {agent_microvm.GUEST_USR_LOWER_MOUNTPOINT}",
            script,
        )
        self.assertIn(
            f"lowerdir={agent_microvm.GUEST_USR_LOWER_MOUNTPOINT},"
            f"upperdir={agent_microvm.GUEST_USR_OVERLAY_UPPER},"
            f"workdir={agent_microvm.GUEST_USR_OVERLAY_WORK}",
            script,
        )
        usr_mount_index = script.index(
            f"mount -t squashfs -o ro /dev/vda {agent_microvm.GUEST_USR_LOWER_MOUNTPOINT}"
        )
        workspace_index = script.index(
            f"mount -t virtiofs {agent_microvm.WORKSPACE_SHARE_TAG}"
        )
        self.assertLess(usr_mount_index, workspace_index)

    def test_boot_trace_mark_records_offsets(self) -> None:
        """Record host events with monotonic offsets from the trace start."""
        trace = agent_microvm.BootTrace(started_at=100.0)
        timestamps = iter([100.25, 100.75])
        with unittest.mock.patch.object(
            agent_microvm.time, "monotonic", side_effect=lambda: next(timestamps)
        ):
            trace.mark("host:first")
            trace.mark(agent_microvm.HOST_TRACE_QEMU_LAUNCHED)

        self.assertEqual(trace.host_events[0], ("host:first", 0.25))
        self.assertEqual(
            trace.host_events[1], (agent_microvm.HOST_TRACE_QEMU_LAUNCHED, 0.75)
        )
        self.assertEqual(trace.qemu_offset, 0.75)

    def test_parse_guest_trace_translates_uptime_to_host_offset(self) -> None:
        """Translate boot-trace uptime values to host monotonic offsets."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            console_log_path = Path(temporary_directory) / "console.log"
            console_log_path.write_text(
                "kernel chatter\n"
                f"{agent_microvm.GUEST_TRACE_PREFIX} mounts-ready 0.12\n"
                "garbage\n"
                f"{agent_microvm.GUEST_TRACE_PREFIX} dropbear-launched 0.40\n",
                encoding="utf-8",
            )

            events = agent_microvm.parse_guest_trace(console_log_path, qemu_offset=0.5)

            self.assertEqual(
                events,
                [
                    ("guest:mounts-ready", 0.62),
                    ("guest:dropbear-launched", 0.90),
                ],
            )

    def test_report_boot_trace_skipped_without_env_var(self) -> None:
        """Skip the boot trace report when the env var is unset."""
        trace = agent_microvm.BootTrace()
        trace.host_events.append(("host:first", 0.1))
        with (
            unittest.mock.patch.dict(os.environ, {}, clear=True),
            unittest.mock.patch.object(agent_microvm.LOGGER, "info") as info,
        ):
            agent_microvm.report_boot_trace(trace, Path("/missing"))

        info.assert_not_called()

    def test_report_boot_trace_logs_sorted_events(self) -> None:
        """Log sorted host and guest events when the env var is set."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            console_log_path = Path(temporary_directory) / "console.log"
            console_log_path.write_text(
                f"{agent_microvm.GUEST_TRACE_PREFIX} workspace-ready 0.20\n",
                encoding="utf-8",
            )
            trace = agent_microvm.BootTrace()
            trace.host_events.append(("host:virtiofsd-ready", 0.05))
            trace.host_events.append((agent_microvm.HOST_TRACE_QEMU_LAUNCHED, 0.10))
            trace.qemu_offset = 0.10

            with (
                unittest.mock.patch.dict(
                    os.environ, {agent_microvm.BOOT_TRACE_ENV_VAR: "1"}
                ),
                unittest.mock.patch.object(agent_microvm.LOGGER, "info") as info,
            ):
                agent_microvm.report_boot_trace(trace, console_log_path)

            labels = [call.args[-1] for call in info.call_args_list[1:]]
            self.assertEqual(
                labels,
                [
                    "host:virtiofsd-ready",
                    agent_microvm.HOST_TRACE_QEMU_LAUNCHED,
                    "guest:workspace-ready",
                ],
            )

    def test_main_prints_usage_for_help_flag(self) -> None:
        """Print usage on the host and skip the VM run when the first argument asks for help."""
        for flag in agent_microvm.HELP_FLAGS:
            with (
                unittest.mock.patch.object(sys, "argv", ("agent-microvm", flag)),
                unittest.mock.patch.object(agent_microvm, "run_microvm") as run_microvm,
                unittest.mock.patch("builtins.print") as printer,
            ):
                self.assertEqual(agent_microvm.main(), 0)

            run_microvm.assert_not_called()
            printer.assert_called_once_with(agent_microvm.usage(), end="")

    def test_skill_doc_states_current_alpine_version_and_arch(self) -> None:
        """Keep the SKILL.md Alpine version and arch in sync with the script constants."""
        text = SKILL_PATH.read_text(encoding="utf-8")
        release = agent_microvm.ALPINE_BRANCH.removeprefix("v")

        self.assertIn(f"Alpine Linux {release}", text)
        self.assertIn(agent_microvm.ALPINE_ARCH, text)

    def test_defaults_to_host_cpu_count_and_memory_formula(self) -> None:
        """Derive default guest resources from host resources."""
        page_size = 4096
        host_memory_mib = 64 * 1024
        page_count = host_memory_mib * 1024 * 1024 // page_size

        def fake_sysconf(name: str) -> int:
            """Return fake sysconf values for host memory."""
            return {"SC_PAGE_SIZE": page_size, "SC_PHYS_PAGES": page_count}[name]

        with (
            unittest.mock.patch.object(
                agent_microvm.os, "sched_getaffinity", return_value=set(range(12))
            ),
            unittest.mock.patch.object(
                agent_microvm.os, "sysconf", side_effect=fake_sysconf
            ),
        ):
            self.assertEqual(agent_microvm.default_cpu_count(), 12)
            self.assertEqual(agent_microvm.default_memory_mib(), 16 * 1024)

    def test_default_memory_has_eight_gib_floor(self) -> None:
        """Use at least eight GiB of guest memory."""
        page_size = 4096
        host_memory_mib = 16 * 1024
        page_count = host_memory_mib * 1024 * 1024 // page_size

        def fake_sysconf(name: str) -> int:
            """Return fake sysconf values for host memory."""
            return {"SC_PAGE_SIZE": page_size, "SC_PHYS_PAGES": page_count}[name]

        with unittest.mock.patch.object(
            agent_microvm.os, "sysconf", side_effect=fake_sysconf
        ):
            self.assertEqual(agent_microvm.default_memory_mib(), 8 * 1024)

    def test_main_runs_default_request_without_subcommand(self) -> None:
        """Run the default request when no command-line arguments are passed."""
        with (
            unittest.mock.patch.object(sys, "argv", ("agent-microvm",)),
            unittest.mock.patch.object(
                agent_microvm, "run_microvm", return_value=23
            ) as run_microvm,
        ):
            self.assertEqual(agent_microvm.main(), 23)

        self.assertEqual(run_microvm.call_args.args[0], ())

    def test_main_forwards_positional_command_to_guest(self) -> None:
        """Forward positional CLI arguments as the guest command."""
        argv = ("bash", "-c", "echo hi")
        with (
            unittest.mock.patch.object(sys, "argv", ("agent-microvm", *argv)),
            unittest.mock.patch.object(
                agent_microvm, "run_microvm", return_value=0
            ) as run_microvm,
        ):
            agent_microvm.main()

        self.assertEqual(run_microvm.call_args.args[0], argv)

    def test_verbose_subprocess_stdout_discards_output_without_debug(self) -> None:
        """Discard chatty subprocess output when debugging is disabled."""
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                agent_microvm.verbose_subprocess_stdout(),
                agent_microvm.subprocess.DEVNULL,
            )

    def test_verbose_subprocess_stdout_routes_to_stderr_under_debug(self) -> None:
        """Route chatty subprocess output to stderr when debugging is enabled."""
        with unittest.mock.patch.dict(os.environ, {"DEBUG_SANDBOX_AGENT": "1"}):
            self.assertEqual(
                agent_microvm.verbose_subprocess_stdout(), sys.stderr.fileno()
            )

    def test_debug_env_prints_commands_and_runs_vm(self) -> None:
        """Print commands in debug mode without replacing the VM run."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            artifact_dir = Path(temporary_directory)
            artifacts = agent_microvm.GuestArtifacts(
                kernel_path=artifact_dir / "vmlinuz-virt",
                initramfs_path=artifact_dir / "rootfs.cpio",
                usr_squashfs_path=artifact_dir / "usr.squashfs",
            )
            artifacts.kernel_path.write_bytes(b"")
            artifacts.initramfs_path.write_bytes(b"")
            artifacts.usr_squashfs_path.write_bytes(b"")
            artifacts.prepare_marker_path.write_text(
                f"{agent_microvm.PREPARE_RECIPE_VERSION}\n",
                encoding="utf-8",
            )

            with (
                unittest.mock.patch.dict(os.environ, {"DEBUG_SANDBOX_AGENT": "1"}),
                unittest.mock.patch.object(
                    agent_microvm, "default_artifacts", return_value=artifacts
                ),
                unittest.mock.patch.object(
                    agent_microvm,
                    "generate_ssh_keypair",
                    return_value=(SSH_PRIVATE_KEY_PATH, PUBLIC_KEY),
                ),
                unittest.mock.patch.object(
                    agent_microvm,
                    "resolve_executable",
                    side_effect=(Path("/usr/bin/passt"), Path("/usr/bin/virtiofsd")),
                ),
                unittest.mock.patch.object(
                    agent_microvm, "print_commands"
                ) as print_commands,
                unittest.mock.patch.object(
                    agent_microvm, "run_processes", return_value=7
                ) as run_processes,
            ):
                self.assertEqual(agent_microvm.run_microvm(()), 7)

            print_commands.assert_called_once()
            run_processes.assert_called_once()


if __name__ == "__main__":
    os.environ["PYTHONPYCACHEPREFIX"] = os.environ.get("PYTHONPYCACHEPREFIX", "/tmp")
    unittest.main()
