"""Test the agent-proxy mitmproxy addon."""

import asyncio
import importlib.machinery
import importlib.util
import os
import socket
import subprocess
import sys
import types
import unittest
import unittest.mock
from pathlib import Path

from dead_upstream_probe import Case

AGENT_PROXY_PATH = Path(__file__).resolve().parent.parent / "agent-proxy/agent-proxy"
PROBE_PATH = Path(__file__).resolve().parent / "dead_upstream_probe.py"
INTEGRATION_ENV_VAR = "AGENT_PROXY_INTEGRATION"
INTEGRATION_ENABLED = os.environ.get(INTEGRATION_ENV_VAR) is not None
SKIP_REASON = f"Set {INTEGRATION_ENV_VAR} to run a real proxy for these tests"


def load_agent_proxy() -> types.ModuleType:
    """Load the agent-proxy script as a module."""
    loader = importlib.machinery.SourceFileLoader("agent_proxy", str(AGENT_PROXY_PATH))
    spec = importlib.util.spec_from_loader("agent_proxy", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


agent_proxy = load_agent_proxy()


class UpstreamConnectionTests(unittest.IsolatedAsyncioTestCase):
    """Tests of the connections the proxy opens to upstream servers."""

    async def test_dead_peer_detection(self) -> None:
        """Enable keepalive probes and an unacknowledged data timeout on opened connections."""
        with (
            socket.create_server(("127.0.0.1", 0)) as listener,
            unittest.mock.patch.object(
                asyncio, "open_connection", asyncio.open_connection
            ),
        ):
            agent_proxy.load(unittest.mock.sentinel.loader)
            _reader, writer = await asyncio.open_connection(*listener.getsockname())
            sock = writer.get_extra_info("socket")
            for level, option, expected in (
                (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1),
                (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30),
                (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10),
                (socket.IPPROTO_TCP, socket.TCP_USER_TIMEOUT, 60_000),
            ):
                with self.subTest(option=option):
                    self.assertEqual(sock.getsockopt(level, option), expected)
            writer.close()
            await writer.wait_closed()


@unittest.skipUnless(INTEGRATION_ENABLED, SKIP_REASON)
class DeadUpstreamTests(unittest.TestCase):
    """Tests of a real proxy holding a silent tunnel whose upstream path dies or stays alive."""

    def run_probe(self, case: Case) -> None:
        """Run the probe for case in a private user and network namespace and raise if it fails."""
        result = subprocess.run(
            [
                "unshare",
                "-rn",
                sys.executable,
                str(PROBE_PATH),
                str(AGENT_PROXY_PATH),
                agent_proxy.LISTEN_HOST,
                agent_proxy.LISTEN_PORT,
                case,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Probe failed ({result.returncode}): {result.stderr}")

    def test_healthy_tunnel_kept_open(self) -> None:
        """Keep a silent tunnel open while its upstream path stays alive."""
        self.run_probe(Case.HEALTHY)

    def test_waiting_tunnel_closed(self) -> None:
        """Close a tunnel waiting for an answer once its upstream path dies."""
        self.run_probe(Case.WAITING)

    def test_unacknowledged_tunnel_closed(self) -> None:
        """Close a tunnel whose bytes sent after its upstream path died stay unacknowledged."""
        self.run_probe(Case.UNACKNOWLEDGED)


if __name__ == "__main__":
    unittest.main()
