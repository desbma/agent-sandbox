"""Check that agent-proxy closes a silent tunnel once its upstream connection dies, and only then.

Run inside a private user and network namespace: unshare -rn python3 dead_upstream_probe.py AGENT_PROXY HOST PORT CASE
"""

import collections.abc
import contextlib
import enum
import os
import select
import socket
import subprocess
import sys
import tempfile
import time

LOOPBACK = "127.0.0.1"
SETUP_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 0.1
# time within which a dead tunnel closes and beyond which a healthy one stays open
CLOSE_DEADLINE_SECONDS = 90.0


class Case(enum.StrEnum):
    """Fate of the upstream path of a tunnel, and what the client then does."""

    HEALTHY = "healthy"
    WAITING = "waiting"
    UNACKNOWLEDGED = "unacknowledged"


@contextlib.contextmanager
def running_proxy(
    agent_proxy: str, address: tuple[str, int], work: str
) -> collections.abc.Iterator[None]:
    """Run agent-proxy with its state in work, until the context exits."""
    env = os.environ | {"XDG_DATA_HOME": work, "RUNTIME_DIRECTORY": work}
    with subprocess.Popen(
        [agent_proxy],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=None,
    ) as proxy:
        try:
            wait_listening(address)
            yield
        finally:
            proxy.terminate()


def wait_listening(address: tuple[str, int]) -> None:
    """Wait until address accepts connections."""
    deadline = time.monotonic() + SETUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            socket.create_connection(address).close()
        except ConnectionRefusedError:
            time.sleep(POLL_SECONDS)
        else:
            return
    raise RuntimeError(
        f"Proxy not listening on {address} after {SETUP_TIMEOUT_SECONDS} s"
    )


def open_tunnel(proxy: tuple[str, int], upstream_port: int) -> socket.socket:
    """Open a CONNECT tunnel through the proxy to the upstream port."""
    client = socket.create_connection(proxy)
    target = f"{LOOPBACK}:{upstream_port}"
    client.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    response = client.recv(4096)
    if not (response.startswith(b"HTTP/1.1 200 ") and response.endswith(b"\r\n\r\n")):
        raise RuntimeError(f"Unexpected CONNECT response: {response!r}")
    return client


def drop_packets(port: int) -> None:
    """Silently drop every packet to or from port, as a path that forgot its connections does."""
    # the priomap sends unfiltered traffic to band 1, only the filtered port reaches the lossy band 0
    for command in (
        "tc qdisc add dev lo root handle 1: prio bands 3 priomap 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1",
        "tc qdisc add dev lo parent 1:1 handle 10: netem loss 100%",
        f"tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip dport {port} 0xffff flowid 1:1",
        f"tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip sport {port} 0xffff flowid 1:1",
    ):
        subprocess.run(
            command.split(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=None,
            check=True,
        )


def main() -> None:
    """Run a case on an established tunnel and check that the proxy closes the tunnel only if its upstream path died."""
    agent_proxy, host, port = sys.argv[1:4]
    case = Case(sys.argv[4])
    proxy = (host, int(port))
    socket.setdefaulttimeout(SETUP_TIMEOUT_SECONDS)
    subprocess.run(
        ["ip", "link", "set", "lo", "up"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=None,
        check=True,
    )
    with (
        socket.create_server((LOOPBACK, 0)) as upstream,
        tempfile.TemporaryDirectory() as work,
    ):
        upstream_port = upstream.getsockname()[1]
        with (
            running_proxy(agent_proxy, proxy, work),
            open_tunnel(proxy, upstream_port) as client,
        ):
            # the proxy connects upstream on the first bytes, which the upstream reads without answering
            client.sendall(b"request")
            connection, _ = upstream.accept()
            with connection:
                assert connection.recv(64) == b"request"
                if case is Case.HEALTHY:
                    readable, _, _ = select.select(
                        [client], [], [], CLOSE_DEADLINE_SECONDS
                    )
                    assert not readable
                    connection.sendall(b"response")
                    assert client.recv(64) == b"response"
                else:
                    drop_packets(upstream_port)
                    if case is Case.UNACKNOWLEDGED:
                        client.sendall(b"close")
                    client.settimeout(CLOSE_DEADLINE_SECONDS)
                    data = client.recv(1)
                    assert data == b"", data


if __name__ == "__main__":
    main()
