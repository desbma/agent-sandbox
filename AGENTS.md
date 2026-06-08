# agent-sandbox

Tooling to run coding agents (Claude Code, Codex, Amp, pi) inside tight, disposable sandboxes on the host, without ever needing `sudo`. Two layers are assembled here and used together:

- **`sandbox-coding-agent`** (repository root) — the default host boundary. A Bubblewrap launcher that starts an agent in a locked-down namespace: the home directory and most of `/etc` are replaced by tmpfs or read-only binds, the working directory and a few caches are the only writable host paths, and a sandbox-specific section is appended to the agent's global instructions. It also exposes `/dev/kvm` so the inner microVM can boot.
- **`agent-microvm/`** — an escalation path for tasks the bwrap sandbox denies (real root, mounting images, low-level networking, privileged services). It boots a throwaway, root-capable Alpine microVM via rootless QEMU, nested inside the bwrap sandbox, and is also packaged as the `agent-microvm` Claude skill. Its design intent lives in the [agent-microvm component](#agent-microvm-component) section below; `agent-microvm/SKILL.md` covers usage.

The two compose: an agent normally lives in the bwrap sandbox and reaches for the microVM only when it needs privileges the sandbox withholds. Because the microVM runs inside the bwrap sandbox, everything stays doubly contained and is discarded on exit.

Alongside these, **`agent-proxy/`** is an optional host-side [mitmproxy](https://mitmproxy.org/) user service: it injects a real GitHub token into `gh`'s requests to `api.github.com` so the agent can use the GitHub API without the sandbox ever holding the token. When the service is running, `sandbox-coding-agent` routes `gh` through it; README covers setup.

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

All code must pass linting, type checking, and formatting under the single strict config in the root `pyproject.toml` (ruff `select = ["ALL"]`), without `sudo`. From the repository root:

```sh
ruff check
ruff format --check
ty check
```

The two extensionless launcher scripts (`sandbox-coding-agent` and `agent-microvm/agent-microvm`) are pulled into both tools' file discovery (ruff `extend-include`, ty `src.include`); directory traversal would otherwise skip them. `sandbox-coding-agent` has no unit tests. `agent-microvm` adds a unit suite that must also pass without `sudo`, and an opt-in VM-booting integration suite that needs outbound network. From `agent-microvm/`:

```sh
python3 -m unittest discover -s tests
AGENT_MICROVM_INTEGRATION=1 python3 -m unittest tests.test_integration
```

To exercise a change end to end, boot a real VM with a command, e.g. `./agent-microvm uname -a`; the first run downloads Alpine artifacts and builds the rootfs.

## agent-microvm component

Design intent and constraints behind the `agent-microvm/agent-microvm` launcher that the code and `SKILL.md` do not make obvious:

- **Session-bound, throwaway lifetime.** A VM lives exactly one foreground SSH session: it powers off when the session ends, and the launcher kills QEMU/virtiofsd when the `ssh` client exits (closed terminal, dropped link, or a signal received before `ssh` owns the tty). No orphaned VMs survive.
- **Native SIGINT.** Ctrl-C inside the session must reach the foreground guest process, not kill the VM, so interactive programs can be interrupted without losing the session.
- **Native terminal UX.** Correct `TERM`/terminfo/locale, a real controlling TTY, working Ctrl-C/Ctrl-Z/job control, dynamic resize propagation, and no visible boot noise unless `DEBUG_SANDBOX_AGENT` is set.
- **Rejected alternatives.** The baseline is rootless QEMU `microvm` with SSH over passt TCP; virtio-serial SSH, TAP/bridge networking, and vsock were considered and rejected for this path.
- **Bump `PREPARE_RECIPE_VERSION`** on any change to the guest init recipe or baked artifact contents, or existing caches keep running the old recipe.
