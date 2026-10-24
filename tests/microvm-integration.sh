#!/bin/bash
# Run the sandbox-coding-agent test suite, integration tests included, in an agent-microvm guest
# launched from the repository root: agent-microvm bash workspace/tests/microvm-integration.sh

set -euo pipefail

sudo bash -euo pipefail <<'ROOT'
if grep -qw vmx /proc/cpuinfo
then
    modprobe -a tun kvm_intel
else
    modprobe -a tun kvm_amd
fi
chown user /dev/kvm /dev/net/tun
apk add -q debootstrap dnsmasq perl
# the launcher expects a merged-/usr host, which Alpine is not; Debian sid ships a recent enough pasta
debootstrap --variant=minbase --include=bubblewrap,ca-certificates,git,iproute2,passt,python3 \
    sid /debian http://deb.debian.org/debian >/dev/null
# a pacman database stub, for the launcher's pacman support
mkdir -p /debian/var/lib/pacman/local /debian/var/lib/pacman/sync
touch /debian/var/lib/pacman/sync/core.db /debian/var/lib/pacman/sync/core.files
chroot /debian /usr/sbin/useradd --uid "$(id -u user)" --create-home user
# the Debian root resolves names through host loopback, as with systemd-resolved
dnsmasq --listen-address=127.0.0.53 --bind-interfaces
printf 'nameserver 127.0.0.53\n' >/debian/etc/resolv.conf
for dir in dev proc sys home/user/workspace
do
    mkdir -p "/debian/${dir}"
    mount --rbind "/${dir}" "/debian/${dir}"
done
# pivot_root refuses the initramfs root, and user namespaces a chroot: stack the Debian tree over /
# and enter it through busybox, as no path to it climbs back through the stacked root
mount --rbind /debian /debian
cd /debian
mount --move . /
exec /bin/busybox chroot . /usr/bin/su user -c \
    'cd /home/user/workspace && SANDBOX_CODING_AGENT_INTEGRATION=1 python3 -m unittest discover -s tests'
ROOT
