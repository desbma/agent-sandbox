---
name: agent-microvm
description: Runs privileged, risky, OS-level, or system-dependent work in a disposable Alpine microVM. Use proactively when the normal sandbox may lack root, kernel features, services, network capabilities, or OS packages; prefer uv, cargo binstall/cargo install, npm, or npx outside the VM for normal user-level tool installs.
---

# agent-microvm

`agent-microvm` boots a throwaway Alpine Linux microVM, runs your command in it, and destroys it on exit. Inside the guest you are an unprivileged user with **passwordless `sudo`**, so you get real root without touching the host.

## Installation preference

Prefer normal sandbox, user-level package managers for ordinary tools and project dependencies when they fit the task:

- Python: `uv`, `uvx`, `uv tool run`, `uv tool install`.
- Rust: `cargo binstall`, then `cargo install`.
- Node.js: `npm`, `npx`, project-local package scripts.
- Other project-local package managers already used by the repository.

Use these outside the VM when they are likely to work without root and without mutating system paths. This keeps normal development and project verification close to the user's workspace environment.

Use the microVM instead when the task needs OS packages, native system libraries, privileged operations, system services, kernel/network features, mounts, risky/destructive commands, or tools that are awkward to install cleanly in the normal sandbox.

## When to use it

Use the microVM whenever it is the faster, safer, or more reliable path to the task. You do not need the user to explicitly ask for it.

Reach for it early when:

- The normal sandbox might lack privileges, devices, kernel features, system services, or writable system paths.
- The task involves mounting filesystem or disk images, loop devices, LVM, LUKS, network namespaces, `tun`/`tap`, raw sockets, packet capture, firewall rules, or privileged daemons.
- The task may need OS packages, native system libraries, or tools that are not cleanly available through normal user-level package managers.
- Installing tools in the normal sandbox would be noisy, awkward, impossible, or likely to leave unwanted state.
- The command is risky, destructive, or benefits from a disposable environment.
- You would otherwise spend time checking whether system tools are installed or inventing inferior substitutes; in the VM, install the intended `apk` package and run it.

Everything is doubly contained — the microVM runs inside the bwrap sandbox — so even `sudo rm -rf /` inside the guest **cannot affect the host**. Boot takes <3s, so prefer it over elaborate sandbox workarounds; you can also run several VMs at once.

## When not to use it

- The normal sandbox clearly has everything needed and using the VM would add no value, such as a simple file read, local text search, small source edit, or project-local test that already runs normally.
- A normal user-level package install outside the VM is likely to work cleanly with `uv`, `cargo binstall`, `cargo install`, `npm`, `npx`, or a project-local package manager.
- The task must verify behavior in the exact normal sandbox environment rather than a disposable Alpine guest.

Avoid the VM for changes that must affect the host environment outside the mounted workspace or exchange directory, because the guest is reset on every invocation.

## Running it

The examples below write that path as `<skill-dir>/agent-microvm` — substitute the directory you read this file from.

- Run one command and exit (its exit status is propagated to the host):

  ```sh
  <skill-dir>/agent-microvm bash -c 'sudo apk add nmap && nmap -sn 127.0.0.1'
  ```

  For one-shot tasks, put any needed package installation at the front of the VM command.

- Open an interactive shell (no arguments):

  ```sh
  <skill-dir>/agent-microvm
  ```

## What persists, what doesn't

- **The guest is reset on every invocation.** Nothing installed or written inside the VM survives, except under the shared directories below.
- The **current directory** is mounted read-write at `/home/user/workspace`. Write results there to keep them on the host.
- The **exchange directory** `$XDG_RUNTIME_DIR/agent-microvm-exchange` is mounted at the *same absolute path* in guest and host and persists across VMs — use it for artifacts that don't belong in the source tree (logs, captures, images).

## Guest facts

- Alpine Linux 3.23, `x86_64`.
- Login user `user` (uid 1000), passwordless `sudo` to root.
- Network access is available (DHCP, outbound to the internet).

## Installing OS packages in the VM

Packages come from Alpine's `main` and `community` repositories:

```sh
sudo apk add <pkg> ...
```

Install needed OS packages directly inside the VM. Do not spend time probing whether a tool is already installed; installing an already-present package is fine and cheap.

Use `apk search -q <term>` only when you do not know the Alpine package name. If a reasonable package name is known, try `sudo apk add` first and let `apk` report whether it exists.

Do not work around missing tools with inferior substitutes when installing the correct package would be straightforward. The VM is disposable, package installs vanish on exit, and the package index is already populated.

The rootfs is rebuilt whenever it is older than 7 days, so `apk` works without a manual `update`.

## Pre-installed packages

The guest includes a baseline set of common development, networking, build, Python, Node.js, Rust, archive, and diagnostic tools.

Treat the baseline as an optimization, not a contract. If a command relies on a package, include `sudo apk add <pkg>` in the VM command instead of first checking whether it happens to be installed.
