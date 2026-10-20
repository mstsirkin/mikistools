# NordVPN namespace usage

Use `nordvpn-ns-setup` to create independent regular NordVPN app instances,
then `nordvpn-ns-run` to run commands through them as your ordinary user.
Each named instance has its own daemon, network, DNS, and process namespace.
Your music and other data files remain shared.

## Create one instance

The installed Linux NordVPN app must already be logged in. Disconnect the host
VPN before setup. The scripts require Python 3, iproute2, nftables, util-linux,
`chattr`, and kernel overlayfs support. Create `~/.config` and `~/.cache` as
your own user if they do not exist.

```bash
cd /data/mst/scm/mikistools
./nordvpn-ns-setup check
sudo ./nordvpn-ns-setup --user mst --instance tango1
```

Setup copies the host login state, starts a private daemon, disables autoconnect,
and leaves the instance **disconnected**. A temporary firewall blocks outbound
traffic during initialization, including inherited autoconnect attempts. The
permanent guard blocks user traffic outside the VPN even while disconnected.
Setup does not enable NordVPN's kill switch, so the daemon can resolve servers
for an explicit later connection; the namespace guard remains in place.

## Connect and disconnect explicitly

```bash
./nordvpn-ns-run tango1 -- nordvpn connect France
./nordvpn-ns-run tango1 -- nordvpn status
./nordvpn-ns-run tango1 -- curl https://api.ipify.org
./nordvpn-ns-run tango1 -- nordvpn disconnect
```

Disconnect preserves namespaces, daemon processes, and private app state.
Reconnect with the same explicit command. Omitting the location selects
NordVPN's recommended server. Setup accepts legacy `NAME:EXIT` arguments,
but the exit is metadata only; it is never used to connect automatically.

## Prepare ten disconnected instances

```bash
instance_args=()
for instance_index in {1..10}; do
  instance_args+=(--instance "tango${instance_index}")
done
sudo ./nordvpn-ns-setup --user mst "${instance_args[@]}"
```

Preparation does not consume active VPN connections. Each explicitly connected
instance uses one connection. If setup fails, it rolls back only instances
created by that invocation.

## Configure internal address pools

Setup reads [nordvpn-ns-config.json](nordvpn-ns-config.json) beside the scripts:

```json
{
  "address_pools": [
    "172.30.203.0/24",
    "192.168.203.0/24",
    "10.203.0.0/16"
  ]
}
```

It tries pools in order, allocating a free `/30` for each host-to-instance link.
It checks IPv4 routes in all host routing tables and subnets allocated to other
instances. A broad tunnel route such as `10.0.0.0/8` excludes all candidates
inside that range. If every candidate conflicts, setup stops with an error.
The check command previews a free subnet without creating resources:

```bash
./nordvpn-ns-setup check
sudo ./nordvpn-ns-setup --config ./nordvpn-ns-config.json \
  --user mst --instance tango1
```

Use `--subnet CIDR` to override the config pools for one invocation:

```bash
./nordvpn-ns-setup check --subnet 172.30.204.0/24
sudo ./nordvpn-ns-setup --user mst --subnet 172.30.204.0/24 \
  --instance tango1 --instance tango2:random
```

Setup allocates a separate `/30` within that subnet for each instance and still
checks for conflicts. A `/30` can accommodate one instance; a `/24` accommodates
64. When `--subnet` is present, setup does not read the config file and does not
fall back to other pools if the specified subnet has no free link. It applies
to `start` and `check`; existing instances retain their assigned addresses.

All of `10.0.0.0/8` is private, alongside `172.16.0.0/12` and `192.168.0.0/16`,
as defined by [RFC 1918](https://www.rfc-editor.org/rfc/rfc1918#section-3).
Private addresses can overlap with another LAN or VPN. The configuration accepts
subnets within those ranges, with prefixes `/16` through `/30`; a `/24` provides
64 possible `/30` links. These are internal link addresses, separate from the
VPN exit address. Changing the config affects new instances only.

## Run commands without sudo

```bash
./nordvpn-ns-run tango1 -- curl https://api.ipify.org
./nordvpn-ns-run tango2 -- yt-dlp -f bestaudio URL
./nordvpn-ns-run tango1 -- bash
```

Use separate terminals or `&` to run commands concurrently. All commands in the
same instance share its VPN. Inside its shell, you can change the exit:

```bash
nordvpn status
nordvpn connect Germany
nordvpn connect            # NordVPN's recommended server
```

The launcher authenticates your UID through a private Unix socket. It drops
privileges before executing your command, preserves your working directory,
environment, streams, and exit code, and adds the NordVPN group inside the
instance. Interactive sessions have a PTY with Ctrl-C and resize forwarding.
Closing the runner kills its command process group; exiting a shell leaves the
VPN instance running.

## Check connections and recover

```bash
./nordvpn-ns-run tango1 --status
sudo ./nordvpn-ns-setup status
```

Administrative status shows the initial exit selection. Use `nordvpn status`
inside an instance to see its current server after switching exits.

If the VPN disconnects, ordinary commands are refused by the launcher. The
firewall also blocks existing user processes from sending traffic outside the
tunnel. Explicit `nordvpn connect`, `disconnect`, `status`, `settings`, `countries`
and `cities` remain available through the launcher while offline. No teardown
is needed to reconnect. If the daemon or keeper exits, recreate the instance.

## Temporary state and logs

Everything the scripts store is under `/run/mikistools-nordvpn-ns`, which is on
tmpfs on this machine and is cleared on reboot. Each start copies the host's
current app state; changes in an instance do not update the host configuration.

| Path under `/run/mikistools-nordvpn-ns` | Contents |
| --- | --- |
| `tango1/control.sock` | Ordinary-user launcher socket |
| `state/tango1/data/` | Private copy of NordVPN login and app state |
| `state/tango1/mounts/` | Private writable configuration overlays |
| `state/tango1/logs/` | `keeper.log`, `daemon.log`, and `setup.log` |
| `state/registry.json` | Registered resources and previous forwarding setting |
| `state/launcher.py` | Root-owned launcher copy |

State is protected from other users. Inspect logs with sudo; they can contain
account information. Failed starts retain temporary files for diagnosis until
`stop --all` or reboot clears them.

`/etc`, `~/.config`, and `~/.cache` have private writable overlays. Existing
configuration remains readable; writes stay with the instance. `/run` and `/tmp`
are private, with only the scripts' runtime tree retained inside `/run`. Host
SSH agent and desktop bus sockets are unavailable. Other home and data files
are shared. Use these instances for trusted local commands; they are not a
sandbox for hostile programs.

## Stop and clean up

```bash
# One instance:
sudo ./nordvpn-ns-setup stop tango1

# Every instance, including temporary files left by failed starts:
sudo ./nordvpn-ns-setup stop --all
```

Stopping kills the instance processes, removes its namespace, veth and NAT
table, and deletes its temporary login, settings, and logs. Stopping the last
instance restores the host's previous IPv4 forwarding setting. Small shared
control files remain under `/run` until reboot. Downloads in shared data
directories remain available.

Use the stop command before deleting runtime files manually so networking can
be cleaned up. A reboot also ends all instances and clears their runtime files;
they do not restart automatically.

## Troubleshoot setup

```bash
sudo tail -n 80 /run/mikistools-nordvpn-ns/state/tango1/logs/setup.log
sudo tail -n 80 /run/mikistools-nordvpn-ns/state/tango1/logs/keeper.log
sudo tail -n 80 /run/mikistools-nordvpn-ns/state/tango1/logs/daemon.log
```

The host needs a normal internet route. Setup rejects an active `nordlynx` or
`tun` route. It enables IPv4 forwarding and installs its own NAT rules. When
firewalld is running, it temporarily assigns each new host veth interface to the
`trusted` zone, which permits forwarding and traffic to the host from that
interface. Setup checks that this zone has target `ACCEPT`; it leaves physical
interfaces and permanent configuration alone. Stop removes each assignment.
A firewalld reload can clear these runtime assignments, requiring stop/start.
Other host firewall rules can still block traffic. If authentication
fails, confirm the host app is logged in before starting a fresh instance.

Tests cover local protocol, command execution, and the runtime bind mount in an
isolated user namespace when supported. They do not cover live VPN connectivity:

```bash
python3 -m unittest discover -s tests -p test_nordvpn_ns.py
```
