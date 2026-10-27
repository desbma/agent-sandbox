# agent-sandbox

Tooling to run coding agents (Claude Code, Codex, Amp, hax, pi) inside tight, disposable sandboxes on the host, without ever needing `sudo`:

- **`sandbox-coding-agent`** (repository root): the default host boundary, a Bubblewrap launcher giving the agent a locked-down filesystem and its own network, cut off from host loopback. pasta must never spawn bwrap itself: it would close the memfds bwrap reads generated files from, and leave SIGUSR1 blocked in the sandbox.
- **`agent-microvm/`**: an escalation path for tasks the sandbox denies (real root, mounting images, low-level networking, privileged services), booting a throwaway, root-capable microVM nested inside the sandbox, so everything stays doubly contained. It is packaged as a skill; see the [agent-microvm component](#agent-microvm-component) section.
- **`agent-proxy/`**: an optional host-side mitmproxy service giving `gh` access to the GitHub API without the sandbox ever holding the token, and carrying all of the sandbox's HTTPS traffic. mitmproxy exposes no hook reaching upstream sockets, so the proxy patches how mitmproxy opens them; only its integration test catches a mitmproxy release that bypasses the patch.
- **`agent-worker/`**: a launcher starting a coding-agent harness non-interactively, for multi-agent workflows. The harness behavior it relies on was checked against Claude Code 2.1.309, pi 1.4.0 and hax 0.6.1, and can drift with later releases.

## Code Style

These conventions apply repository-wide, to test code as much as to main code.

- Python 3.13+
- Dataclasses for all structured data.
- No `_` prefix on any name (methods, functions, variables, attributes) unless it is genuinely unused (e.g. `for _ in range(n)`). All names are plain, even internal helpers.
- Do not use `del` to discard unused function parameters. Instead, prefix the parameter name with `_` in the signature (e.g. `_prompt: str`).
- Docstrings mandatory on all functions (imperative mood).
- Typing:
  - Annotations mandatory on all function signatures. Always write the real type, never a string-quoted annotation.
  - Use `from __future__ import annotations` only when genuinely required for unresolved forward references.
  - Avoid `typing.Any`; use precise types, protocols, or generics instead. `Any` is acceptable only as a last resort when no precise type is feasible, never as a shortcut to skip proper typing.
  - Avoid `typing.cast`; prefer precise annotations, runtime narrowing (`isinstance` / assertions), or API shapes that type-check without casts.
- No verbose comments that paraphrase the code.
- Split large functions into small, single-responsibility ones when needed.
- Favor importing the root module and using fully qualified names in code. Exceptions: names from `typing`, and `pathlib.Path`.
- Name a constant (module-level or class-level) for any value whose meaning is not obvious from the literal, that is reused across the file, or that is a genuine tunable or threshold (timeouts, poll intervals, resource sizes, version pins, protocol markers). Inline self-evident literals instead: values whose name would merely restate them, command-line flags and arguments with no reason to vary, fixed well-known paths (e.g. `/etc/passwd`, `/usr/bin/sudo`), and one-off values specific to a single call site. Prefer a short, meaningful constant list over exhaustively naming every literal.
- Never use `""` or `0` as sentinel values to mean "absent" or "not set". Use `None` (with `| None` in the type annotation) so the type system distinguishes missing from legitimately empty/zero.
- Group all module-level constants at the top of the file, before class and function definitions.
- Do not add large section-separator comment blocks (e.g. `# ===...` banners). Use class docstrings and natural whitespace to organize code.
- At the end of any refactor, remove dead code (unused constants, types, helpers, and imports) before finishing.
- Always favor f-strings for string formatting, unless there is a specific reason not to (e.g., logging lazy formatting with `%s` is acceptable when performance matters).
- All logging messages must start with a capital letter.
- In test code, do not pass a `msg` argument to `assert*` calls; let the assertion's default rendering of the values stand, and if extra context is needed refactor the surrounding code (helper, context manager) rather than stuffing it into a string.

## Testing, Linting & Formatting

All code must pass linting, type checking and formatting under the strict config in the root `pyproject.toml`, and every unit suite must pass, all without `sudo`. Extensionless scripts must be listed in `pyproject.toml`, ruff's and ty's directory traversal skipping them. From the repository root, then from each of `agent-microvm/` and `agent-worker/` for their unit suites:

```sh
ruff check
ruff format --check
ty check
python3 -m unittest discover -s tests
```

Opt-in suites, from the repository root:

```sh
# sandbox-coding-agent integration and unit suites, in a microVM's Debian root: they need /dev/net/tun, which the agent sandbox withholds
agent-microvm/agent-microvm bash workspace/tests/microvm-integration.sh
# sandbox-coding-agent performance: also needs hyperfine 2 or later and jj, so it does not run inside the agent sandbox either
SANDBOX_CODING_AGENT_PERFORMANCE=1 python3 -m unittest discover -s tests -p test_performance.py
# agent-proxy integration: needs mitmdump, openssl, iproute2, the netem qdisc and unprivileged user namespaces; takes about 3.5 minutes
AGENT_PROXY_INTEGRATION=1 python3 -m unittest discover -s tests -p test_agent_proxy.py
```

From `agent-microvm/`:

```sh
# needs outbound network
AGENT_MICROVM_INTEGRATION=1 python3 -m unittest tests.test_integration
# needs hyperfine 2 or later
AGENT_MICROVM_PERFORMANCE=1 python3 -m unittest tests.test_performance
```

To exercise an `agent-microvm` change end to end, boot a real VM with a command, e.g. `./agent-microvm uname -a`; the first run downloads Alpine artifacts and builds the rootfs.

## agent-microvm component

Design intent the code does not make obvious:

- **Session-bound lifetime.** A VM lives exactly one foreground SSH session, and no VM outlives it, whatever ends the session.
- **Native terminal.** Ctrl-C reaches the foreground guest process instead of killing the VM, the terminal behaves as on the host (type, locale, job control, resizes), and boot shows no noise unless debugging.
- **Rejected alternatives.** Virtio-serial SSH, TAP/bridge networking and vsock were considered and rejected in favor of rootless QEMU `microvm` with SSH over passt TCP.
- **Recipe version.** Bump it on any change to the guest init recipe or baked artifact contents, or existing caches keep running the old recipe.
