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

From this directory (replace the example server names with actual servers):

```bash
./nordvpn-ns-setup check
sudo ./nordvpn-ns-setup --user mst \
  --instance tango1:us1234 --instance tango2:us5678

# Or choose an exit country at random for each instance:
# sudo ./nordvpn-ns-setup --user mst \
#   --instance tango1:random --instance tango2:random

# These commands run as mst; no sudo or password prompt.
./nordvpn-ns-run tango1 -- curl https://api.ipify.org
./nordvpn-ns-run tango2 -- curl https://api.ipify.org
./nordvpn-ns-run tango1 -- yt-dlp -f bestaudio URL
./nordvpn-ns-run tango2 -- bash
./nordvpn-ns-run tango1 --status

# Administrative inspection/teardown:
sudo ./nordvpn-ns-setup status
sudo ./nordvpn-ns-setup stop --all
```

Run simultaneous commands in separate terminals or background them with `&`.
An exit can be a country, city, specific server, or `random`. `random` chooses
uniformly from the installed client's available countries at each start, then
lets NordVPN choose a server in that country. Setup prints the selected country;
two independent random choices may select the same country. Inside an existing
shell, use `nordvpn connect COUNTRY` to change the exit, or `nordvpn connect`
for NordVPN's recommended server.
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
and loopback; an unavailable VPN route also prevents new launches. This is
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
