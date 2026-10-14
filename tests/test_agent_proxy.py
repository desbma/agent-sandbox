"""Test the agent-proxy mitmproxy addon."""

import asyncio
import contextlib
import importlib.machinery
import importlib.util
import os
import select
import socket
import subprocess
import sys
import types
import unittest
import unittest.mock
from pathlib import Path

from proxy_probe import Scenario

AGENT_PROXY_PATH = Path(__file__).resolve().parent.parent / "agent-proxy/agent-proxy"
PROBE_PATH = Path(__file__).resolve().parent / "proxy_probe.py"
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
        open_connection = agent_proxy.with_dead_peer_detection(asyncio.open_connection)
        with socket.create_server(("127.0.0.1", 0)) as listener:
            _reader, writer = await open_connection(*listener.getsockname())
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

    async def test_loopback_refused(self) -> None:
        """Refuse opening connections to the host's loopback, leaving its listeners untouched."""
        with (
            socket.create_server(("127.0.0.1", 0)) as listener,
            unittest.mock.patch.object(
                asyncio, "open_connection", asyncio.open_connection
            ),
        ):
            agent_proxy.load(unittest.mock.sentinel.loader)
            with self.assertRaises(PermissionError):
                await asyncio.open_connection(*listener.getsockname())
            readable, _, _ = select.select([listener], [], [], 0)
            self.assertEqual(readable, [])

    async def test_hosts_reaching_loopback_refused(self) -> None:
        """Refuse every spelling of a loopback or unspecified address without trying to connect."""
        open_connection = unittest.mock.AsyncMock()
        open_outward_connection = agent_proxy.with_loopback_refusal(open_connection)
        for host in (
            "127.0.0.1",
            "127.1.2.3",
            "2130706433",
            "localhost",
            "0.0.0.0",
            "::1",
            "::",
            "::ffff:127.0.0.1",
            "::ffff:0.0.0.0",
        ):
            with self.subTest(host=host), self.assertRaises(PermissionError):
                await open_outward_connection(host, 443)
        open_connection.assert_not_called()

    async def test_name_with_one_loopback_address_refused(self) -> None:
        """Refuse a name when any of its addresses reaches loopback, even after others."""
        open_connection = unittest.mock.AsyncMock()
        with (
            self.resolving(("192.0.2.1", 443), ("127.0.0.1", 443)),
            self.assertRaises(PermissionError),
        ):
            await agent_proxy.with_loopback_refusal(open_connection)(
                "upstream.test", 443
            )
        open_connection.assert_not_called()

    async def test_resolved_addresses_tried_in_order(self) -> None:
        """Connect to the resolved addresses in order until one accepts, passing other arguments through."""
        streams = (unittest.mock.sentinel.reader, unittest.mock.sentinel.writer)
        open_connection = unittest.mock.AsyncMock(
            side_effect=[ConnectionRefusedError("refused"), streams]
        )
        with self.resolving(
            ("2001:db8::1", 443, 0, 0), ("192.0.2.1", 443), ("192.0.2.2", 443)
        ):
            result = await agent_proxy.with_loopback_refusal(open_connection)(
                "upstream.test", 443, local_addr=None
            )
        self.assertEqual(result, streams)
        self.assertEqual(
            open_connection.await_args_list,
            [
                unittest.mock.call("2001:db8::1", 443, local_addr=None),
                unittest.mock.call("192.0.2.1", 443, local_addr=None),
            ],
        )

    async def test_every_failure_reported(self) -> None:
        """Report the failure of each resolved address when none accepts."""
        open_connection = unittest.mock.AsyncMock(
            side_effect=[ConnectionRefusedError("refused"), TimeoutError("timed out")]
        )
        with (
            self.resolving(("2001:db8::1", 443, 0, 0), ("192.0.2.1", 443)),
            self.assertRaisesRegex(OSError, "^refused; timed out$"),
        ):
            await agent_proxy.with_loopback_refusal(open_connection)(
                "upstream.test", 443
            )

    async def test_scoped_address_keeps_its_zone(self) -> None:
        """Connect to a link-local IPv6 address through the interface its scope id names."""
        open_connection = unittest.mock.AsyncMock()
        with self.resolving(("fe80::1", 443, 0, 1)):
            await agent_proxy.with_loopback_refusal(open_connection)("fe80::1%1", 443)
        open_connection.assert_awaited_once_with("fe80::1%1", 443)

    def resolving(
        self, *sockaddrs: tuple[str, int] | tuple[str, int, int, int]
    ) -> contextlib.AbstractContextManager[unittest.mock.AsyncMock]:
        """Make the running loop resolve any name to the given socket addresses, in order."""
        infos = [(0, 0, 0, "", sockaddr) for sockaddr in sockaddrs]
        return unittest.mock.patch.object(
            asyncio.get_running_loop(),
            "getaddrinfo",
            unittest.mock.AsyncMock(return_value=infos),
        )


def run_probe(scenario: Scenario) -> None:
    """Run a scenario of the agent-proxy probe in a private user and network namespace and raise if it fails."""
    result = subprocess.run(
        [
            "unshare",
            "-rn",
            sys.executable,
            str(PROBE_PATH),
            str(AGENT_PROXY_PATH),
            agent_proxy.LISTEN_HOST,
            agent_proxy.LISTEN_PORT,
            scenario,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Probe failed ({result.returncode}): {result.stderr}")


@unittest.skipUnless(INTEGRATION_ENABLED, SKIP_REASON)
class TunnelTests(unittest.TestCase):
    """Tests of tunnels through a real proxy."""

    def test_tunnels_to_loopback_refused(self) -> None:
        """Close tunnels to any spelling of loopback without reaching it, while opening others."""
        run_probe(Scenario.LOOPBACK_REFUSAL)

    def test_healthy_tunnel_kept_open(self) -> None:
        """Keep a silent tunnel open while its upstream path stays alive."""
        run_probe(Scenario.HEALTHY)

    def test_waiting_tunnel_closed(self) -> None:
        """Close a tunnel waiting for an answer once its upstream path dies."""
        run_probe(Scenario.WAITING)

    def test_unacknowledged_tunnel_closed(self) -> None:
        """Close a tunnel whose bytes sent after its upstream path died stay unacknowledged."""
        run_probe(Scenario.UNACKNOWLEDGED)


if __name__ == "__main__":
    unittest.main()
