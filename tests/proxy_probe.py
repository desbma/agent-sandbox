"""Check how agent-proxy handles the tunnels a client asks for, and the GitHub API requests it intercepts in them.

Run inside private user, mount and network namespaces: unshare -rnm python3 proxy_probe.py AGENT_PROXY HOST PORT SCENARIO
"""

import collections.abc
import concurrent.futures
import contextlib
import enum
import http.client
import os
import select
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# address standing in for a remote upstream server: not loopback, yet local to the namespace
UPSTREAM = "192.0.2.1"
SETUP_TIMEOUT_SECONDS = 10.0
POLL_SECONDS = 0.1
# time within which a dead tunnel closes and beyond which a healthy one stays open
CLOSE_DEADLINE_SECONDS = 90.0
# spellings of the host's loopback a client may ask the proxy to tunnel to
LOOPBACK_TARGETS = ("127.0.0.1", "localhost", "0.0.0.0", "[::1]", "[::ffff:127.0.0.1]")
GREETING = b"greeting\r\n"
# endpoint whose requests the proxy intercepts, served by the upstream address
API_HOST = "api.github.com"
API_PORT = 443
# GitHub token the proxy reads from its credentials
TOKEN = "probe-token"
# the two parts of the request body
FIRST_PART = b"first part, "
SECOND_PART = b"second part"
REQUEST_BODY = FIRST_PART + SECOND_PART
FIRST_CHUNK = b"first chunk"


class Scenario(enum.StrEnum):
    """Tunnel a client asks the proxy for, and what then happens to it."""

    LOOPBACK_REFUSAL = "loopback-refusal"
    SERVER_GREETING = "server-greeting"
    INTERCEPTION = "interception"
    HEALTHY = "healthy"
    WAITING = "waiting"
    UNACKNOWLEDGED = "unacknowledged"


@contextlib.contextmanager
def running_proxy(
    agent_proxy: str, address: tuple[str, int], work: Path
) -> collections.abc.Generator[None]:
    """Run agent-proxy with its state and credentials in work, until the context exits."""
    env = os.environ | dict.fromkeys(
        ("XDG_DATA_HOME", "RUNTIME_DIRECTORY", "CREDENTIALS_DIRECTORY"), str(work)
    )
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


def request_tunnel(proxy: tuple[str, int], target: str) -> tuple[socket.socket, bytes]:
    """Ask the proxy for a CONNECT tunnel to the host:port target, and return the connection and the proxy's response."""
    client = socket.create_connection(proxy)
    client.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    return client, client.recv(4096)


def open_tunnel(proxy: tuple[str, int], target: str) -> socket.socket:
    """Open a CONNECT tunnel through the proxy to the host:port target."""
    client, response = request_tunnel(proxy, target)
    if not (response.startswith(b"HTTP/1.1 200 ") and response.endswith(b"\r\n\r\n")):
        raise RuntimeError(f"Unexpected CONNECT response: {response!r}")
    return client


def run_command(command: collections.abc.Sequence[str]) -> None:
    """Run a setup command, raising if it fails."""
    subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=None,
        check=True,
    )


def configure_namespace() -> None:
    """Bring up the namespace's loopback interface, with the upstream address on it."""
    for command in ("ip link set lo up", f"ip addr add {UPSTREAM}/32 dev lo"):
        run_command(command.split())


def drop_packets(port: int) -> None:
    """Silently drop every packet to or from port, as a path that forgot its connections does."""
    # the priomap sends unfiltered traffic to band 1, only the filtered port reaches the lossy band 0
    for command in (
        "tc qdisc add dev lo root handle 1: prio bands 3 priomap 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1",
        "tc qdisc add dev lo parent 1:1 handle 10: netem loss 100%",
        f"tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip dport {port} 0xffff flowid 1:1",
        f"tc filter add dev lo parent 1: protocol ip prio 1 u32 match ip sport {port} 0xffff flowid 1:1",
    ):
        run_command(command.split())


def resolve_api_host(work: Path) -> None:
    """Make this mount namespace resolve the GitHub API host to the upstream address."""
    hosts = work / "hosts"
    hosts.write_text(f"127.0.0.1 localhost\n::1 localhost\n{UPSTREAM} {API_HOST}\n")
    run_command(["mount", "--bind", str(hosts), "/etc/hosts"])
    addresses = {info[4][0] for info in socket.getaddrinfo(API_HOST, API_PORT)}
    if addresses != {UPSTREAM}:
        raise RuntimeError(
            f"{API_HOST} resolves to {addresses}, not through /etc/hosts"
        )


def prepare_interception(work: Path) -> None:
    """Resolve the GitHub API host to the upstream address, create its self-signed certificate, and give the proxy its token with upstream verification off."""
    resolve_api_host(work)
    run_command(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-noenc",
            "-keyout",
            str(work / "api.key"),
            "-out",
            str(work / "api.pem"),
            "-subj",
            f"/CN={API_HOST}",
        ]
    )
    confdir = work / "agent-proxy"
    confdir.mkdir()
    (confdir / "config.yaml").write_text("ssl_insecure: true\n")
    (work / "github_token").write_text(TOKEN)


def receive_until(connection: socket.socket, received: bytes, end: bytes) -> bytes:
    """Add what connection receives to received until it ends with end, and return it."""
    while not received.endswith(end):
        data = connection.recv(4096)
        if not data:
            raise RuntimeError(f"Request cut short: {received!r}")
        received += data
    return received


def answer_api_request(listener: socket.socket, work: Path) -> bytes:
    """Answer one request as the GitHub API, starting a chunked response once the first part of its body arrives and ending it once the rest does, and return the request."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(work / "api.pem", work / "api.key")
    connection, _ = listener.accept()
    with context.wrap_socket(connection, server_side=True) as server:
        request = receive_until(server, b"", FIRST_PART)
        server.sendall(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            + f"{len(FIRST_CHUNK):x}\r\n".encode()
            + FIRST_CHUNK
            + b"\r\n"
        )
        request = receive_until(server, request, REQUEST_BODY)
        server.sendall(b"0\r\n\r\n")
    return request


def stream_api_request(proxy: tuple[str, int], work: Path) -> None:
    """Send a request to the GitHub API through the proxy, holding back the second part of its body until the first chunk of the response arrives."""
    context = ssl.create_default_context(
        cafile=work / "agent-proxy/mitmproxy-ca-cert.pem"
    )
    with contextlib.closing(
        http.client.HTTPSConnection(*proxy, context=context)
    ) as client:
        client.set_tunnel(API_HOST, API_PORT)
        client.putrequest("POST", "/")
        client.putheader("Authorization", "Bearer placeholder")
        client.putheader("Content-Length", len(REQUEST_BODY))
        client.endheaders(FIRST_PART)
        response = client.getresponse()
        assert response.read(len(FIRST_CHUNK)) == FIRST_CHUNK
        client.send(SECOND_PART)
        assert response.read() == b""


def check_loopback_refusal(proxy: tuple[str, int], upstream: socket.socket) -> None:
    """Check that the proxy answers 502 to tunnels to loopback without reaching it, while opening a tunnel to another address."""
    with socket.create_server(
        ("::", 0), family=socket.AF_INET6, dualstack_ipv6=True
    ) as loopback:
        for target in LOOPBACK_TARGETS:
            client, response = request_tunnel(
                proxy, f"{target}:{loopback.getsockname()[1]}"
            )
            with client:
                assert response.startswith(b"HTTP/1.1 502 "), (target, response)
        readable, _, _ = select.select([loopback], [], [], 0)
        assert not readable
    with open_tunnel(proxy, f"{UPSTREAM}:{upstream.getsockname()[1]}") as client:
        client.sendall(b"request")
        connection, _ = upstream.accept()
        with connection:
            assert connection.recv(64) == b"request"


def check_server_greeting(proxy: tuple[str, int], upstream: socket.socket) -> None:
    """Check that a greeting the upstream sends first reaches a client that has sent nothing."""
    with open_tunnel(proxy, f"{UPSTREAM}:{upstream.getsockname()[1]}") as client:
        connection, _ = upstream.accept()
        with connection:
            connection.sendall(GREETING)
            assert client.recv(64) == GREETING


def check_interception(
    proxy: tuple[str, int], upstream: socket.socket, work: Path
) -> None:
    """Check that a request to the GitHub API reaches it with the proxy's token, and that its body and its response both stream through."""
    with concurrent.futures.ThreadPoolExecutor() as executor:
        client = executor.submit(stream_api_request, proxy, work)
        request = answer_api_request(upstream, work)
    client.result()
    head, _, body = request.partition(b"\r\n\r\n")
    fields = [line.partition(b": ") for line in head.split(b"\r\n")[1:]]
    authorizations = [
        value for name, _, value in fields if name.lower() == b"authorization"
    ]
    assert authorizations == [f"Bearer {TOKEN}".encode()], authorizations
    assert body == REQUEST_BODY, body


def check_upstream_fate(
    proxy: tuple[str, int], upstream: socket.socket, scenario: Scenario
) -> None:
    """Check that the proxy closes an established silent tunnel only if its upstream path died."""
    upstream_port = upstream.getsockname()[1]
    with open_tunnel(proxy, f"{UPSTREAM}:{upstream_port}") as client:
        # the upstream reads the first bytes without answering
        client.sendall(b"request")
        connection, _ = upstream.accept()
        with connection:
            assert connection.recv(64) == b"request"
            if scenario is Scenario.HEALTHY:
                readable, _, _ = select.select([client], [], [], CLOSE_DEADLINE_SECONDS)
                assert not readable
                connection.sendall(b"response")
                assert client.recv(64) == b"response"
            else:
                drop_packets(upstream_port)
                if scenario is Scenario.UNACKNOWLEDGED:
                    client.sendall(b"close")
                client.settimeout(CLOSE_DEADLINE_SECONDS)
                data = client.recv(1)
                assert data == b"", data


def main() -> None:
    """Run a scenario against a fresh proxy and check how the proxy handles its tunnel."""
    agent_proxy, host, port = sys.argv[1:4]
    scenario = Scenario(sys.argv[4])
    proxy = (host, int(port))
    socket.setdefaulttimeout(SETUP_TIMEOUT_SECONDS)
    configure_namespace()
    upstream_port = API_PORT if scenario is Scenario.INTERCEPTION else 0
    with (
        socket.create_server((UPSTREAM, upstream_port)) as upstream,
        tempfile.TemporaryDirectory() as work_dir,
    ):
        work = Path(work_dir)
        if scenario is Scenario.INTERCEPTION:
            prepare_interception(work)
        with running_proxy(agent_proxy, proxy, work):
            if scenario is Scenario.LOOPBACK_REFUSAL:
                check_loopback_refusal(proxy, upstream)
            elif scenario is Scenario.SERVER_GREETING:
                check_server_greeting(proxy, upstream)
            elif scenario is Scenario.INTERCEPTION:
                check_interception(proxy, upstream, work)
            else:
                check_upstream_fate(proxy, upstream, scenario)


if __name__ == "__main__":
    main()
