---
name: agent-microvm
description: Run privileged or destructive actions in a disposable, root-capable Alpine microVM via the `agent-microvm` command. Use when a task needs real root (mounting filesystem or disk images, loop devices, low-level networking, raw sockets, privileged services) or system packages with no uv/cargo/npm equivalent. Everything stays contained inside the VM (itself inside the bwrap sandbox) and is discarded on exit, so it cannot affect the host.
---

# agent-microvm

`agent-microvm` boots a throwaway Alpine Linux microVM, runs your command in it, and destroys it on exit. Inside the guest you are an unprivileged user with **passwordless `sudo`**, so you get real root without touching the host.

## When to use it

Reach for the microVM when the task needs privileges the normal sandbox denies:

- Mounting filesystem or disk images, loop devices, LVM, LUKS.
- Low-level networking: network namespaces, `tun`/`tap`, raw sockets, packet capture, firewall rules.
- Running privileged services or daemons.
- Installing OS-level system packages (via `apk`) that have no `uv`/`cargo`/`npm` equivalent.
- Running anything risky or destructive and having it vanish cleanly afterwards.

Everything is doubly contained — the microVM runs inside the bwrap sandbox — so even `sudo rm -rf /` inside the guest **cannot affect the host**. Boot takes <3s, so use it freely; you can also run several VMs at once.

## When not to use it

- The dependency installs fine in the normal environment (`uv`, `cargo`, `npm`). Prefer that.
- A non-VM approach gets the job done without compromising the result.

The VM is for privilege, not convenience.

## Running it

The examples below write that path as `<skill-dir>/agent-microvm` — substitute the directory you read this file from.

- Run one command and exit (its exit status is propagated to the host):

  ```sh
  <skill-dir>/agent-microvm bash -c 'sudo apk add nmap && nmap -sn 127.0.0.1'
  ```

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

## Installing packages

Packages come from Alpine's `main` and `community` repositories:

```sh
apk search -q <term>      # find a package
sudo apk add <pkg> ...    # install (the package index is already populated)
```

Install whatever you need liberally — it disappears with the VM. The package index and pre-installed packages are at most about a week old: the rootfs is rebuilt whenever it is older than 7 days, so `apk` works without a manual `update`.

## Pre-installed packages

The guest already ships these `apk` packages (install more with `sudo apk add`):

- `bind-tools`
- `build-base`
- `ca-certificates`
- `cargo`
- `coreutils`
- `curl`
- `diffutils`
- `fd`
- `file`
- `findutils`
- `gawk`
- `gcc`
- `git`
- `grep`
- `gzip`
- `iproute2`
- `iputils`
- `jq`
- `jujutsu`
- `libffi-dev`
- `linux-headers`
- `netcat-openbsd`
- `nodejs`
- `npm`
- `openssl`
- `openssl-dev`
- `pkgconf`
- `procps-ng`
- `python3`
- `python3-dev`
- `ripgrep`
- `ruff`
- `sed`
- `shellcheck`
- `sqlite`
- `sqlite-dev`
- `strace`
- `sudo`
- `tar`
- `tree`
- `typescript`
- `unzip`
- `uv`
- `which`
- `xz`
- `xz-dev`
- `zip`
- `zlib-dev`
- `zstd`
