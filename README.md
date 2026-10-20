A collection of small scripts for development

## nordproxy

Run a command through a random NordVPN SOCKS5 proxy:

    ./nordproxy curl https://example.com

Reads `username:password` from `~/.nordvpn-username:password.txt`
and randomly selects a `server:port` line from `~/.nordproxies.txt`.
Sets `http_proxy` and `https_proxy`, then replaces itself with the command,
preserving its arguments. The command must support these proxy variables.

Requires: Bash, `sed`, `shuf`

## nordvpn-ns-setup / nordvpn-ns-run

See the [NordVPN namespace usage guide](nordvpn-namespaces.md) for setup,
ten-instance examples, exit selection, recovery, and cleanup.

Run the regular Linux NordVPN app (NordLynx) in separate namespaces, then
launch commands as your normal user without sudo. Each instance has a root
keeper/launcher and its own daemon, network, process tree, DNS, and app state.
Requires Linux, Python 3, the installed NordVPN app, iproute2, nftables,
util-linux, `chattr`, and kernel overlayfs support.

Log in to the host NordVPN app first, then disconnect its VPN. Setup copies
`/var/lib/nordvpn` into root-protected temporary storage in `/run` at each start;
it does not print credentials or pass login tokens on command lines. Reusing
the copied login depends on the installed app accepting it and needs a live
check. Stop deletes private state; each new start copies the host state again.

Setup creates disconnected instances. Connection is always explicit:

```bash
./nordvpn-ns-setup check
sudo ./nordvpn-ns-setup --user mst --instance tango1 --instance tango2
./nordvpn-ns-run tango1 -- nordvpn connect France
./nordvpn-ns-run tango2 -- nordvpn connect United_Kingdom
./nordvpn-ns-run tango1 -- curl https://api.ipify.org

# Disconnect while preserving namespaces, daemons and private state:
./nordvpn-ns-run tango1 -- nordvpn disconnect
./nordvpn-ns-run tango2 -- nordvpn disconnect

# Inspect, or explicitly destroy all instances:
sudo ./nordvpn-ns-setup status
sudo ./nordvpn-ns-setup stop --all
```

Legacy `NAME:EXIT` setup arguments remain accepted as metadata only; they do
not connect or select an exit. Choose a country, city or server with an explicit
`nordvpn connect LOCATION`, or omit LOCATION for NordVPN's recommended server.
Run simultaneous commands in separate terminals or background them with `&`.
The runner preserves arguments, environment, working directory, standard
streams, and exit status. Interactive terminals use a PTY with Ctrl-C and
window-size forwarding. Client disconnection kills its command process group;
stopping an instance kills all of its remaining processes.

Your home and data files are shared. `/etc` has a private writable overlay,
`/run` and `/tmp` are private, and `~/.config` / `~/.cache` have private writable
overlays so NordVPN's user helper cannot alter host settings. Existing
configuration is readable, but writes to those overlays stay with the
instance. Host runtime sockets (SSH agent, desktop bus, etc.) are unavailable
inside. Create `~/.config` and `~/.cache` as your user if they do not exist.

The root launcher authenticates the socket's kernel-supplied peer UID and
accepts requests only from the instance owner or root. It drops to the owner's
UID/groups, adds the NordVPN group only inside the instance, and disables new
privileges before executing the command or applying its environment. The
instance's firewall blocks owner-UID network traffic except through `nordlynx`
and loopback; an unavailable VPN route prevents ordinary command launches.
Explicit NordVPN connect/disconnect/status/settings/countries/cities commands
remain available while disconnected. Autoconnect is disabled during setup; a
temporary firewall blocks daemon egress until copied settings are reset. This is
network isolation for trusted local commands, not a sandbox for hostile code.

Setup selects a free private /30 from the candidate pools in
[nordvpn-ns-config.json](nordvpn-ns-config.json), checking all host IPv4 route
tables and existing instances for conflicts. The first default pool is
`172.30.203.0/24`. Use `--config FILE` to select another configuration or
`--subnet CIDR` to override its pools for one invocation. Use `check` to preview
a free subnet. Each instance gets a `/30` inside the selected pool; a `/24`
supports 64 links. It creates a veth pair and a
separate NAT table per instance. It enables host IPv4 forwarding and restores
the previous value when the last instance stops. If firewalld is running,
setup temporarily assigns only its new veth interfaces to the `trusted` zone
to permit forwarding; stop removes those assignments. This also permits
traffic to host services from those interfaces. Other host firewall rules can
still block traffic. A connected host `nordlynx`/`tun` route is rejected before setup.
Instances are temporary and do not restart after a reboot. After a crash,
use `stop --all` to clean registered resources before starting again.

Private state and logs are under `/run/mikistools-nordvpn-ns/state/NAME/`:
`logs/keeper.log`, `logs/daemon.log`, and `logs/setup.log`. A failed batch start
rolls back instances created by that invocation, retaining their private state
and logs for diagnosis. Inspect logs with sudo; they can contain account
information. Normal stop deletes the instance's files; `stop --all` also clears
files from failed starts. A reboot clears the runtime tree. To refresh the copied
login, stop and start the instance again.

Unprivileged protocol/process tests:

```bash
python3 -m unittest discover -s tests -p test_nordvpn_ns.py
```

## vmtest

Build a test on the host, package it as `/init` in a cpio initramfs,
and boot it in QEMU with a chosen kernel. No disk image and no guest
userspace setup: QEMU gets the initramfs on its command line, the
kernel runs the test as PID 1, the test reports on the serial console
and powers the guest off. The whole console is teed to a log file.

    KERN=/path/to/arch/x86/boot/bzImage ./vmtest test.c

A `.c` source is compiled with `gcc -static -O2 -Wall` (further `.c`
arguments are linked in; any other source is copied to `/init` as is
and must be self-contained, for example a static binary). The guest
reports its outcome with a `RESULT TOKEN` console line, the last one
wins. `PASS`, `FAIL`, and `SKIP` map to exit statuses 0, 1, and 4;
`--map TOKEN:RC` adds or overrides mappings. A test whose expected
outcome is a kernel crash never reaches its `RESULT` line: give the
crash's console signature with `--pass-regex` instead. `--poison` boots
with `slub_debug=P init_on_alloc=0 init_on_free=0` so freed slab data
reads `0x6b`, which makes out-of-bounds reads recognizable in captured
traffic.

Per-test defaults can live in a `SOURCE.vmtest` file next to the source
(one option per line, `#` starts a comment, quotes group words). Command
line options are parsed after the file and win; the file may not select
the kernel or the log, which come from `--kern`/`--out` or the
`KERN`/`OUT` environment variables.

`--lib` packs a shared library into the initramfs together with
everything it needs: pass a soname from the linker cache
(`--lib libcrypto.so.3`) or a path to a specific build
(`--lib build/libfoo.so.1`, which then wins over any installed
version). With a `.c` source the test is compiled dynamically and
linked against the given libraries; a dynamically linked binary as
SOURCE also works, its interpreter and libraries are packed the same
way. Everything lands under the loader's own directory, where the
guest looks without any `ld.so.conf`:

```bash
gcc -shared -fPIC -Wl,-soname,libdemo.so.1 -o build/libdemo.so.1 demo.c
KERN=/path/to/bzImage ./vmtest --lib build/libdemo.so.1 test.c
```

An existing reproducer with its `.vmtest` defaults, only the kernel is
selected:

```bash
KERN=/data/mst/scm/build-csum-test/arch/x86/boot/bzImage \
  /data/mst/scm/mikistools/vmtest /data/mst/scm/virtio-csum-test/tests/test_csum_leak.c
```

Requires: Bash, `gcc`, `cpio`, `gzip`, `timeout`,
`qemu-system-x86_64`, and writable `/dev/kvm` for KVM (otherwise warns
and falls back to TCG). The guest kernel must provide `/dev/console`
from its built-in initramfs, which linux 5.17 and later do, and the
test must power off on its own; the `--timeout` backstop is 90 seconds
by default.

## sashiko-review

Query the [sashiko.dev](https://sashiko.dev) patch review API.

Usage:

    sashiko-review [-m|--markdown] <message-id>
    sashiko-review [-m|--markdown] < message.mbox

Without `-m`/`--markdown`, prints the raw JSON response.
With `-m`/`--markdown`, formats the review as readable markdown.

When run without a message-id argument, reads an email in mbox format from
stdin and extracts the Message-ID automatically.

Requires: `curl`, `jq`
