# Agent Sandbox

Opinionated solution for sandboxing coding agents on Linux.

---

**This repository contains personal tools that I publish for sharing, documentation and convenience.**

**They may or may not work for your use case, and little effort has been made to support systems and workflows other than the ones I use.**

---

## Rationale & philosophy

The core principle of coding agents involves allowing a stochastic model to run random commands on your system. That premise is RCE _by design_.

Built-in solutions in existing agents either involve a simplistic permission system, that everyone bypasses in practice because of approval fatigue, or internal "security theater" sandboxes that the model can bypass on demand.

Instead of adding complexity like some [other](https://github.com/earendil-works/gondolin) third-party solutions, I think a better approach is to run the agent process inside a constrained environment, using a proven solution, to follow the minimum privilege principle.

## Content

This is made from 3 parts:

- [`sandbox-coding-agent`](#sandbox-coding-agent): a [Bubblewrap](https://github.com/containers/bubblewrap) based wrapper, that runs coding agents in an isolated environment, sharing some directories only.
- [`agent-proxy`](#agent-proxy): an optional host-side [mitmproxy](https://mitmproxy.org/) user service that lets [`gh`](https://github.com/cli/cli) reach the GitHub API from inside the sandbox without exposing the token to the agent.
- [`agent-microvm`](#agent-microvm): a [skill](https://agentskills.io/) the agent can use to spawn a throwaway microVM and run privileged commands in it, completely isolated from the host.

The sandbox and microVM scripts were preceded by experiments with different container ([Firejail](https://github.com/netblue30/firejail), [systemd-nspawn](https://www.freedesktop.org/software/systemd/man/latest/systemd-nspawn.html)) and VMM ([CrosVM](https://github.com/google/crosvm), [cloud-hypervisor](https://github.com/cloud-hypervisor/cloud-hypervisor), [Firecracker](https://firecracker-microvm.github.io/), [systemd-vmspawn](https://www.freedesktop.org/software/systemd/man/latest/systemd-vmspawn.html)) solutions, and were then refined during months of daily use.

## `sandbox-coding-agent`

### Features

- Supported agents: Amp, Claude Code, Codex CLI, Pi
- Startup overhead <30ms
- Unshares all namespaces except network: the agent can not access processes, devices, users, etc. outside of its sandbox
- Builds filesystem namespace tailored for the agent:
  - current directory shared as read/write
  - `.git`/`.jj` VCS directories are mounted read-only: the agent can not do commits or change history
  - most of the filesystem (`/etc/`, `/home`, `/run`, `/var`, etc.) is either not mounted or cleaned up to contain a minimal allowlist
  - cache directories used for development (`cargo`, `uv`...) are mounted as OverlayFS: the agent inherits the host's content (major speedup), but the changes it makes are not reflected on the host
  - project build directories (Rust `target/`, Python `.venv`) are mounted as OverlayFS as well: reusing the artifacts already built on the host avoids full rebuilds, which can save minutes on big projects
- Provides an exchange directory for sharing files that don't belong in the repository
- [Jujutsu](https://jj-vcs.github.io/jj/) aware:
  - `jj` is wrapped to always run with `--ignore-working-copy`, which keeps it usable despite the read-only `.jj` directory
  - the default workspace's VCS directories are exposed alongside the current one, so commands still work from a secondary workspace
  - the exchange directory and Claude Code's project memory are shared between all workspaces of a repository
- Injects a small prompt to describe the sandbox to the agent, its directories and mount points, etc. It is appended to the agent's global instructions, built from `~/.config/agents/AGENTS.md` and, if present, the agent specific `~/.config/agents/AGENTS.<agent>.md` (ie. `AGENTS.claude.md`)
- Remaps agent directories to [XDG](https://specifications.freedesktop.org/basedir/latest/) compliant ones (ie. Claude config lives in `~/.config/claude` on the host, instead of the default `~/.claude`)
- Provisions the other installed agents alongside the one being launched, so it can spawn them as subagents, typically to get a review from a different model
- Applies per-agent quality of life fixes: Codex CLI trusts the launch directory instead of prompting about it, Pi keeps its extension modules in its own state directory, etc.
- Safeguards against accidental launches from the wrong directory: the home directory is rejected, and a repository subdirectory offers to switch to the repository root
- Exposes `/dev/kvm` and the host CPU topology, so the agent can run nested VMs with a matching core layout and pinned vCPUs: this is what lets `agent-microvm` work from inside the sandbox
- Optionally routes `gh` through [`agent-proxy`](#agent-proxy), so the GitHub API works inside the sandbox without ever exposing the token to the agent

**Network isolation is out of scope: the agent has access to the same network as the host.**

### Requirements

- [Bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`)
- Python 3

### Usage

Example for Claude Code:

1. Copy `sandbox-coding-agent` in `~/.local/bin` or `/usr/local/bin`

2. Create a wrapper script in `~/.local/bin/claude` or `/usr/local/bin/claude`:

```sh
#!/bin/sh
export CLAUDE_CODE_SANDBOXED=1
exec sandbox-coding-agent /usr/bin/claude --dangerously-skip-permissions "$@"
```

Replace `/usr/bin/claude` by the location of the main Claude Code binary. Use `~/.local/libexec/claude` if you have manually downloaded the `claude` binary, to avoid running it outside of the sandbox by accident.

3. Just run `claude` as usual, it will go through the sandbox

## `agent-proxy`

An optional host-side [mitmproxy](https://mitmproxy.org/) user service lets `gh` reach the GitHub API from inside the sandbox without the sandbox ever seeing the token: `gh` is given a placeholder token and routed through the proxy, which swaps in the real token (from an encrypted systemd credential) only for `api.github.com`. When the service is running the launcher injects the env vars that point `gh` at it.

Setup, on the host:

```sh
install -Dm755 agent-proxy/agent-proxy ~/.local/bin/agent-proxy
install -Dm644 agent-proxy/agent-proxy.service ~/.config/systemd/user/agent-proxy.service

# Encrypt a GitHub token into the user credstore
install -d -m700 ~/.config/credstore.encrypted
printf 'GitHub token: '; stty -echo; read -r token; stty echo; echo
printf '%s' "$token" |
  systemd-creds --user encrypt --name=github_token - ~/.config/credstore.encrypted/github_token
unset token

systemctl --user daemon-reload
systemctl --user enable --now agent-proxy.service
```

## `agent-microvm`

### Features

- Runs a light Alpine Linux distribution, with common developer packages installed
- Unprivileged guest user with passwordless `sudo`: real root in the VM, fully isolated from the host
- Current directory shared read-write at `/home/user/workspace`
- Persistent exchange directory shared at the same absolute path in guest and host, for artifacts that don't belong in the source tree
- The distribution is transparently rebuilt if needed, to always have up to date packages
- Supported host distributions: Arch Linux, Debian
- Uses common tools: `qemu` for virtualization and `passt` for networking
- Startup overhead <3s hot, ~20s cold (first time only)

### Requirements

Host packages (binaries the skill invokes):

- Arch Linux: `cpio openssh passt python qemu-system-x86 squashfs-tools virtiofsd`
- Debian: `cpio openssh-client passt python3 qemu-system-x86 squashfs-tools virtiofsd`

Access to `/dev/kvm` is required. Because the skill runs inside the Bubblewrap sandbox, which does not preserve supplementary groups, your user must own the device directly rather than rely on `kvm` group membership. A udev rule grants this:

```sh
printf 'KERNEL=="kvm", OWNER:="%s", GROUP:="kvm", MODE:="0660"\n' "$USER" | sudo tee /etc/udev/rules.d/99-local-kvm-owner.rules
sudo udevadm control --reload-rules && sudo udevadm trigger --name-match=kvm
```

### Usage

1. Install the `agent-microvm` skill for your agent of choice. Look up its documentation for the possible locations.
2. Ask the agent to use the skill, or let it use it when it sees fit.

## License

[GPL-3.0-only](LICENSE).
