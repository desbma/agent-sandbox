# Agent Sandbox

This is my opinionated solution for the coding agent sandboxing problem.

## Rationale & philosophy

The core principle of coding agents involves allowing a stochastic model to run random commands on your system. This is not a security _bug_, this is RCE _by design_.

Built-in solutions in existing agents either involve a simplistic permission system, that everyone bypasses in practice because of approval fatigue, or internal "security theater" sandboxes that the model can bypass on demand.

Instead of adding complexity like some [other](https://github.com/earendil-works/gondolin) third-party solutions, I think a better approach is to run the agent process inside a constrained environment, using a proven solution, to follow the minimum privilege principle.

## Content

This is made from 2 independent parts:

- [`sandbox-coding-agent`](#sandbox-coding-agent): a [Bubblewrap](https://github.com/containers/bubblewrap) based wrapper, that runs coding agents in an isolated environment, sharing some directories only.
- [`agent-microvm`](#agent-microvm): a [skill](https://agentskills.io/) the agent can use to spawn a throwaway microVM and run privileged commands in it, completely isolated from the host.

These scripts were preceded by experiments with different container ([Firejail](https://github.com/netblue30/firejail), [systemd-nspawn](https://www.freedesktop.org/software/systemd/man/latest/systemd-nspawn.html)) and VMM ([CrosVM](https://github.com/google/crosvm), [cloud-hypervisor](https://github.com/cloud-hypervisor/cloud-hypervisor), [Firecracker](https://firecracker-microvm.github.io/), [systemd-vmspawn](https://www.freedesktop.org/software/systemd/man/latest/systemd-vmspawn.html)) solutions, and were then refined during months of daily use. They are heavily tailored for my workflow, and probably won't work as-is for yours.

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
- Injects a small prompt to describe the sandbox to the agent, its directories and mount points, etc.
- Remaps agent directories to [XDG](https://specifications.freedesktop.org/basedir/latest/) compliant ones (ie. Claude config lives in `~/.config/claude` on the host, instead of the default `~/.claude`)
- Provides an exchange directory for sharing files that don't belong in the repository
- Exposes `/dev/kvm`, so the agent can run nested VMs: this is what lets `agent-microvm` work from inside the sandbox

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
