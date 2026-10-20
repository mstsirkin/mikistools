"""Linux-only, standard-library NordVPN namespace setup and local launcher.

Root code never executes an environment or executable supplied by the client
until after setgroups/setgid/setuid. The socket authenticates SO_PEERCRED.
"""
import argparse
import array
import contextlib
import fcntl
import grp
import ipaddress
import json
import os
import pathlib
import pty
import pwd
import re
import secrets
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import time
import tty

RUN = pathlib.Path("/run/mikistools-nordvpn-ns")
STATE = RUN / "state"
FORWARD = pathlib.Path("/proc/sys/net/ipv4/ip_forward")
MAX_REQUEST = 262144
SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "HOME": "/root", "LC_ALL": "C"}
NAME = re.compile(r"[a-z][a-z0-9_-]{0,23}\Z")
SERVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\Z")


def checked_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value):
        raise ValueError("Instance names must start with a lowercase letter; use letters, digits, _ or - (24 characters max).")
    return value


def parse_instance(value):
    name, sep, server = value.partition(":")
    checked_name(name)
    if not sep or not SERVER.fullmatch(server):
        raise ValueError("Use --instance NAME:SERVER, for example tango1:us1234.")
    return name, server


def command(args, **kwargs):
    return subprocess.run(args, check=True, env=SAFE_ENV, **kwargs)


def json_write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def write_readiness(directory_fd, value):
    # The directory FD remains valid even if namespace mount setup fails and
    # hides the original /run. Always report startup failures to the parent.
    fd = os.open("ready.tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                 0o600, dir_fd=directory_fd)
    with os.fdopen(fd, "w") as stream:
        stream.write(json.dumps(value) + "\n")
    os.rename("ready.tmp", "ready.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)


def read_registry():
    path = STATE / "registry.json"
    return json.loads(path.read_text()) if path.exists() else {"instances": {}, "forward_before": None}


def save_registry(registry):
    json_write(STATE / "registry.json", registry)


def process_start(pid):
    # comm can contain spaces and closing parentheses.
    return pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]


def alive(info):
    try:
        return process_start(info["pid"]) == info["start"]
    except (OSError, KeyError):
        return False


def root_directories():
    for path, mode in ((STATE, 0o700), (RUN, 0o755)):
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.stat().st_uid != 0:
            raise RuntimeError(f"Refusing non-root-owned directory: {path}")
        path.chmod(mode)


def socket_path(name):
    return RUN / checked_name(name) / "control.sock"


def rpc(name, request, fds=()):
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    try:
        connection.connect(str(socket_path(name)))
        packet = json.dumps(request).encode()
        if len(packet) > MAX_REQUEST:
            raise ValueError("Request is too large.")
        ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))] if fds else []
        connection.sendmsg([packet], ancillary)
        return connection
    except BaseException:
        connection.close()
        raise


def receive_request(connection, owner_uid):
    _pid, uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
    if uid not in (0, owner_uid):
        raise PermissionError("This instance belongs to another user.")
    packet, ancillary, flags, _address = connection.recvmsg(MAX_REQUEST, socket.CMSG_SPACE(3 * array.array("i").itemsize))
    descriptors = []
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            received = array.array("i")
            received.frombytes(data[:len(data) - len(data) % received.itemsize])
            descriptors.extend(received)
    try:
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ValueError("Truncated request.")
        request = json.loads(packet)
        if not isinstance(request, dict):
            raise ValueError("Invalid request.")
        if request.get("op") == "run":
            validate_run(request)
            if len(descriptors) != 3:
                raise ValueError("Supply stdin, stdout and stderr.")
        elif request.get("op") != "status" or descriptors:
            raise ValueError("Unknown operation.")
        return request, descriptors
    except BaseException:
        for fd in descriptors:
            os.close(fd)
        raise


def validate_run(request):
    argv, env, cwd = request.get("argv"), request.get("env"), request.get("cwd")
    if not isinstance(argv, list) or not argv or len(argv) > 4096:
        raise ValueError("Supply a command and arguments.")
    if not all(isinstance(arg, str) and "\0" not in arg for arg in argv) or not argv[0]:
        raise ValueError("Invalid command argument.")
    if not isinstance(cwd, str) or not cwd.startswith("/") or "\0" in cwd:
        raise ValueError("Working directory must be absolute.")
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and k and "=" not in k and "\0" not in k
        and isinstance(v, str) and "\0" not in v for k, v in env.items()
    ):
        raise ValueError("Invalid environment.")


def drop_user(user):
    groups = os.getgrouplist(user.pw_name, user.pw_gid)
    nord_gid = grp.getgrnam("nordvpn").gr_gid
    os.setgroups(sorted(set(groups + [nord_gid])))
    os.setgid(user.pw_gid)
    os.setuid(user.pw_uid)
    # Prevent setuid programs/file capabilities from regaining privileges.
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(38, 1, 0, 0, 0) != 0:  # PR_SET_NO_NEW_PRIVS
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_NO_NEW_PRIVS)")


def vpn_connected():
    result = subprocess.run(["ip", "route", "get", "1.1.1.1"], env=SAFE_ENV, capture_output=True, text=True)
    return result.returncode == 0 and re.search(r"\bdev nordlynx(?:\s|$)", result.stdout) is not None


def launch_user(request, descriptors, user):
    ready_read, ready_write = os.pipe2(os.O_CLOEXEC)
    child = os.fork()
    if child:
        os.close(ready_write)
        os.read(ready_read, 1)
        os.close(ready_read)
        return child
    os.close(ready_read)
    try:
        os.setsid()
        os.write(ready_write, b"1")
        os.close(ready_write)
        for target, source in enumerate(descriptors):
            os.dup2(source, target)
        # Take control of the client-allocated PTY, rather than its original tty.
        if os.isatty(0):
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        drop_user(user)
        os.chdir(request["cwd"])
        # Everything outside stdio is private launcher/daemon state.
        os.closerange(3, 65536)
        os.execvpe(request["argv"][0], request["argv"], request["env"])
    except BaseException as error:
        os.write(2, f"nordvpn-ns-run: {error}\n".encode())
        os._exit(127)


def serve_connection(connection, listener, user):
    listener.close()
    descriptors = []
    child = None
    try:
        connection.settimeout(10)
        request, descriptors = receive_request(connection, user.pw_uid)
        connection.settimeout(None)
        connected = vpn_connected()
        if request["op"] == "status":
            connection.send(json.dumps({"connected": connected, "uid": user.pw_uid}).encode())
            return
        if not connected:
            raise RuntimeError("VPN route is unavailable; command was not started.")
        child = launch_user(request, descriptors, user)
        for fd in descriptors:
            os.close(fd)
        descriptors = []
        while True:
            waited, status = os.waitpid(child, os.WNOHANG)
            if waited:
                code = os.waitstatus_to_exitcode(status)
                connection.send(json.dumps({"exit": code if code >= 0 else 128 - code}).encode())
                child = None
                break
            if select.select([connection], [], [], 0.1)[0]:
                packet = connection.recv(256)
                if not packet:
                    break
                message = json.loads(packet)
                sig = message.get("signal")
                if sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child, sig)
    except (Exception, KeyboardInterrupt) as error:
        with contextlib.suppress(OSError):
            connection.send(json.dumps({"error": str(error)}).encode())
    finally:
        for fd in descriptors:
            os.close(fd)
        if child is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(child, 0)
        connection.close()


def mount_overlay(target, storage, uid=0, gid=0):
    upper, work = storage / "upper", storage / "work"
    for path in (upper, work):
        path.mkdir(parents=True, exist_ok=True)
    os.chown(upper, uid, gid)
    command(["mount", "-t", "overlay", "overlay", "-o", f"lowerdir={target},upperdir={upper},workdir={work}", str(target)])


def private_mounts(directory, user):
    command(["mount", "--make-rprivate", "/"])
    mount_overlay("/etc", directory / "mounts/etc")
    resolver = pathlib.Path("/etc/resolv.conf")
    # NordVPN replaces this file. Overlaying /etc handles both writes and renames.
    if resolver.is_file() and not resolver.is_symlink():
        subprocess.run(["chattr", "-i", str(resolver)], env=SAFE_ENV,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    resolver.unlink(missing_ok=True)
    resolver.write_text("nameserver 103.86.96.100\nnameserver 103.86.99.100\n")
    resolver.chmod(0o644)
    nss = pathlib.Path("/etc/nsswitch.conf")
    nss.write_text(re.sub(r"^hosts:.*$", "hosts: files dns", nss.read_text(), flags=re.MULTILINE))
    private_run()
    command(["mount", "-t", "tmpfs", "-o", "mode=1777", "tmpfs", "/tmp"])
    # ip netns exec may already mount sysfs for this network namespace.
    # Detach it only in our private mount namespace before mounting anew.
    command(["umount", "--lazy", "/sys"])
    command(["mount", "-t", "sysfs", "sysfs", "/sys"])
    nord_gid = grp.getgrnam("nordvpn").gr_gid
    for target in ("/run/nordvpn", "/run/norduserd"):
        pathlib.Path(target).mkdir(mode=0o750)
        os.chown(target, 0, nord_gid)
    user_run = pathlib.Path(f"/run/user/{user.pw_uid}")
    user_run.mkdir(parents=True, mode=0o700)
    os.chown(user_run, user.pw_uid, user.pw_gid)
    for target, source in (("/var/lib/nordvpn", directory / "data"), ("/var/log/nordvpn", directory / "logs")):
        command(["mount", "--bind", str(source), target])
    # Keep NordVPN's per-user helper settings private too. Other existing
    # configuration remains readable; changes in these two trees stay private.
    for name in (".config", ".cache"):
        target = pathlib.Path(user.pw_dir) / name
        if not target.is_dir():
            raise RuntimeError(f"Create {target} as {user.pw_name} before setup.")
        mount_overlay(target, directory / f"mounts/{name[1:]}", user.pw_uid, user.pw_gid)


def private_run():
    # Preserve only our own runtime tree while hiding host systemd/DBus and
    # NordVPN sockets. The open FD still refers to the host tree after mounting
    # a new /run. Pass it to mount so /proc/self/fd resolves in that subprocess.
    runtime_fd = os.open(RUN, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        command(["mount", "-t", "tmpfs", "-o", "mode=755", "tmpfs", "/run"])
        RUN.mkdir(mode=0o755)
        # libmount otherwise canonicalizes this magic link to the old pathname,
        # which now names the empty directory on the NEW /run mount.
        command(["mount", "--no-canonicalize", "--bind", f"/proc/self/fd/{runtime_fd}", str(RUN)],
                pass_fds=(runtime_fd,))
        if not STATE.is_dir():
            raise RuntimeError("Private /run bind mount did not preserve the instance state.")
    finally:
        os.close(runtime_fd)


def nord_cli(user, *args):
    env = dict(SAFE_ENV, HOME=user.pw_dir, USER=user.pw_name, LOGNAME=user.pw_name,
               XDG_RUNTIME_DIR=f"/run/user/{user.pw_uid}")
    return subprocess.run(["nordvpn", *args], env=env, preexec_fn=lambda: drop_user(user),
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, timeout=120)


def parse_countries(output):
    # The client prints one column when stdout is a pipe; also accept its
    # terminal columns and virtual-location ANSI colors. Keep spaces in names.
    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    countries = []
    for line in plain.splitlines():
        for item in re.split(r"\s{2,}", line.strip()):
            if item and item[0].isalpha() and all(char.isalpha() or char in " _'-" for char in item):
                if item not in countries:
                    countries.append(item)
            elif item:
                raise RuntimeError("Unexpected country-list output from NordVPN; refusing to select an exit.")
    if not countries:
        raise RuntimeError("NordVPN returned an empty country list.")
    return countries


def resolve_exit(value, user):
    if value.lower() != "random":
        return value
    result = nord_cli(user, "countries")
    if result.returncode:
        raise RuntimeError("Cannot list NordVPN exit countries for random selection.")
    return secrets.choice(parse_countries(result.stdout))


def nord_step(user, directory, *args):
    result = nord_cli(user, *args)
    with open(directory / "logs/setup.log", "a") as log:
        log.write(f"nordvpn {' '.join(args)}\n{result.stdout}\n")
    if result.returncode:
        raise RuntimeError(f"nordvpn {' '.join(args)} failed (exit {result.returncode}). See setup.log and daemon.log.")


def connect_private_vpn(user, requested_exit, directory):
    # No user commands are served during bootstrap. Clear the copied kill
    # switch so unmarked bootstrap DNS works before the tunnel is installed.
    for args in (("set", "autoconnect", "off"), ("set", "killswitch", "off"),
                 ("set", "technology", "NordLynx"), ("set", "routing", "on"),
                 ("set", "firewall", "on"), ("set", "fwmark", "0xe1f1")):
        nord_step(user, directory, *args)
    exit_location = resolve_exit(requested_exit, user)
    nord_step(user, directory, "connect", exit_location)
    if not vpn_connected():
        raise RuntimeError("NordVPN returned without installing a NordLynx route.")
    nord_step(user, directory, "set", "killswitch", "on")
    return exit_location


def keeper_main(name, listener_fd):
    directory = STATE / name
    readiness_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    config = json.loads((directory / "instance.json").read_text())
    user = pwd.getpwuid(config["uid"])
    listener = socket.socket(fileno=listener_fd)
    daemon = None
    handlers = set()
    stopping = False

    def stop(_sig, _frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        private_mounts(directory, user)
        daemon_log = open(directory / "logs/daemon.log", "ab", buffering=0)
        daemon = subprocess.Popen([shutil.which("nordvpnd")], env=SAFE_ENV, stdin=subprocess.DEVNULL,
                                  stdout=daemon_log, stderr=daemon_log,
                                  group=grp.getgrnam("nordvpn").gr_gid)
        deadline = time.monotonic() + 30
        while not pathlib.Path("/run/nordvpn/nordvpnd.sock").exists():
            if daemon.poll() is not None or time.monotonic() > deadline or stopping:
                raise RuntimeError("NordVPN daemon did not create its socket. See the private daemon log.")
            time.sleep(0.2)
        exit_location = connect_private_vpn(user, config["server"], directory)
        config["selected_exit"] = exit_location
        json_write(directory / "instance.json", config)
        rules = f'''table inet mikistools_guard {{
 chain output {{ type filter hook output priority 100; policy accept;
  meta mark 0xe1f1 meta l4proto udp accept
  meta skuid {user.pw_uid} oifname "lo" accept
  meta skuid {user.pw_uid} oifname "nordlynx" accept
  meta skuid {user.pw_uid} drop
 }}
}}\n'''
        # WireGuard can retain the sending user's socket identity on encrypted
        # packets emitted through the uplink. Allow its privileged transport
        # mark, fixed during bootstrap, before dropping user traffic off-tunnel.
        command(["nft", "-f", "-"], input=rules, text=True)
        write_readiness(readiness_fd, {"ready": True, "exit": exit_location})
        listener.settimeout(0.5)
        while not stopping and daemon.poll() is None:
            # PID 1 must reap orphaned NordVPN helpers as well as handlers.
            while True:
                try:
                    pid, _status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if not pid:
                    break
                handlers.discard(pid)
                if pid == daemon.pid:
                    stopping = True
            if len(handlers) >= 64:
                time.sleep(0.1)
                continue
            try:
                connection, _address = listener.accept()
            except socket.timeout:
                continue
            pid = os.fork()
            if pid == 0:
                signal.signal(signal.SIGTERM, signal.SIG_DFL)
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                serve_connection(connection, listener, user)
                os._exit(0)
            handlers.add(pid)
            connection.close()
    except BaseException as error:
        write_readiness(readiness_fd, {"error": str(error)})
        return 1
    finally:
        os.close(readiness_fd)
        listener.close()
        if daemon is not None and daemon.poll() is None:
            daemon.terminate()
        # Exiting namespace PID 1 kills every remaining instance process.
    return 0


def permit_firewalld_forwarding(veth):
    if not shutil.which("firewall-cmd", path=SAFE_ENV["PATH"]):
        return None
    state = subprocess.run(["firewall-cmd", "--state"], env=SAFE_ENV, capture_output=True, text=True)
    if state.returncode:
        if "not running" in (state.stdout + state.stderr).lower() or state.returncode == 252:
            return None
        raise RuntimeError("Cannot determine host firewalld state; inspect firewall-cmd --state.")
    zone = command(["firewall-cmd", "--zone=trusted", "--list-all"], capture_output=True, text=True).stdout
    if not re.search(r"^\s*target:\s*ACCEPT\s*$", zone, re.MULTILINE):
        raise RuntimeError("Host firewalld trusted zone does not have target ACCEPT; cannot enable namespace forwarding.")
    # Runtime only, scoped to the newly-created host veth. No permanent zone
    # changes, no assignment of the physical interface, no service shutdown.
    command(["firewall-cmd", "--zone=trusted", f"--add-interface={veth}"], capture_output=True, text=True)
    return "trusted"


def remove_network(info):
    if info.get("firewalld_zone"):
        subprocess.run(["firewall-cmd", f"--zone={info['firewalld_zone']}", f"--remove-interface={info['veth']}"],
                       env=SAFE_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for key, args in (("made_table", ("nft", "delete", "table", "ip", info["table"])),
                      ("made_netns", ("ip", "netns", "delete", info["netns"])),
                      ("made_veth", ("ip", "link", "delete", info["veth"]))):
        if info.get(key):
            subprocess.run(args, env=SAFE_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def remove_instance_files(name):
    checked_name(name)
    directory = STATE / name
    # Some NordVPN versions protect their private DNS files with chattr.
    upper = directory / "mounts/etc/upper"
    if upper.is_dir():
        for parent, _directories, files in os.walk(upper, followlinks=False):
            for filename in files:
                path = pathlib.Path(parent) / filename
                if not path.is_symlink():
                    subprocess.run(["chattr", "-i", str(path)], env=SAFE_ENV,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for path in (directory, RUN / name):
        if path.exists():
            shutil.rmtree(path)


def stop_instance(name, registry, keep_files=False):
    info = registry["instances"][name]
    if alive(info):
        os.kill(info["pid"], signal.SIGTERM)
        deadline = time.monotonic() + 5
        while alive(info) and time.monotonic() < deadline:
            time.sleep(0.1)
        if alive(info):
            os.kill(info["pid"], signal.SIGKILL)
    remove_network(info)
    socket_path(name).unlink(missing_ok=True)
    del registry["instances"][name]
    if not registry["instances"] and registry["forward_before"] is not None:
        # Do not overwrite a different value set by another administrator.
        if FORWARD.read_text().strip() == "1":
            FORWARD.write_text(registry["forward_before"] + "\n")
        registry["forward_before"] = None
    save_registry(registry)
    if not keep_files:
        remove_instance_files(name)


def load_pools(path):
    config = json.loads(pathlib.Path(path).read_text())
    if not isinstance(config, dict) or set(config) != {"address_pools"}:
        raise ValueError("Config must contain only an address_pools list.")
    return validate_pools(config["address_pools"])


def validate_pools(values):
    if not isinstance(values, list) or not values:
        raise ValueError("Configure at least one private IPv4 address pool.")
    private = [ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
    pools = []
    for value in values:
        if not isinstance(value, str):
            raise ValueError("Address pools must be CIDR strings.")
        network = ipaddress.ip_network(value, strict=True)
        if network.version != 4 or not 16 <= network.prefixlen <= 30 or not any(network.subnet_of(block) for block in private):
            raise ValueError(f"Pool {value} must be an RFC 1918 IPv4 subnet, with prefix /16 through /30.")
        if network not in pools:
            pools.append(network)
    return pools


def select_pools(config_path, subnet):
    return validate_pools([subnet]) if subnet is not None else load_pools(config_path)


def choose_network(registry, pools):
    used = {item["slot"] for item in registry["instances"].values()}
    slot = next((candidate for candidate in range(1, 255) if candidate not in used), None)
    if slot is None:
        raise RuntimeError("All 254 instance resource slots are in use.")
    routes = json.loads(command(["ip", "-j", "-4", "route", "show", "table", "all"], capture_output=True, text=True).stdout)
    networks = []
    for route in routes:
        with contextlib.suppress(ValueError):
            network = ipaddress.ip_network(route.get("dst", "default"), strict=False)
            if network.prefixlen:
                networks.append(network)
    for item in registry["instances"].values():
        # Older running instances used a fixed 10.203.SLOT.0/30 subnet.
        networks.append(ipaddress.ip_network(item.get("subnet", f"10.203.{item['slot']}.0/30")))
    for pool in pools:
        # Skip entire covered pools without enumerating thousands of subnets.
        if any(pool.subnet_of(network) for network in networks if network.version == 4):
            continue
        for subnet in pool.subnets(new_prefix=30):
            if not any(subnet.overlaps(network) for network in networks if network.version == 4):
                gateway, address = list(subnet.hosts())
                return {"slot": slot, "subnet": str(subnet), "gateway": str(gateway), "address": str(address)}
    raise RuntimeError("No unused /30 in configured address_pools; choose another private pool in --config.")


def start_instance(name, server, user, registry, pools):
    if name in registry["instances"]:
        raise RuntimeError(f"{name} already exists. Stop it before restarting.")
    directory = STATE / name
    # Every new instance gets the host's current configuration/login snapshot.
    # Leftover files from a failed start must not become a persistent config.
    remove_instance_files(name)
    directory.mkdir(mode=0o700, exist_ok=True)
    configuration = directory / "instance.json"
    if configuration.exists() and json.loads(configuration.read_text())["uid"] != user.pw_uid:
        raise RuntimeError(f"{name} has retained state belonging to another user; choose a different name.")
    data = directory / "data"
    if not data.exists():
        shutil.copytree("/var/lib/nordvpn", data, symlinks=True)
    logs = directory / "logs"
    logs.mkdir(mode=0o750, exist_ok=True)
    os.chown(logs, 0, grp.getgrnam("nordvpn").gr_gid)
    json_write(configuration, {"uid": user.pw_uid, "server": server})
    ready = directory / "ready.json"
    ready.unlink(missing_ok=True)
    allocation = choose_network(registry, pools)
    slot = allocation["slot"]
    info = {**allocation, "netns": f"miki-nord-{name}", "veth": f"mnv{slot}",
            "table": f"miki_nord_{slot}", "uid": user.pw_uid, "server": server}
    # Refuse collisions even when an existing device has no configured routes.
    for args in (("ip", "link", "show", info["veth"]),
                 ("nft", "list", "table", "ip", info["table"])):
        if subprocess.run(args, env=SAFE_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            raise RuntimeError("A network resource with this instance's name already exists; refusing to replace it.")
    namespaces = command(["ip", "netns", "list"], capture_output=True, text=True).stdout
    if info["netns"] in {line.split()[0] for line in namespaces.splitlines() if line.split()}:
        raise RuntimeError(f"Namespace {info['netns']} already exists; refusing to replace it.")
    # Reserve resources before changing the host, so failure cleanup is exact.
    registry["instances"][name] = info
    if registry["forward_before"] is None:
        registry["forward_before"] = FORWARD.read_text().strip()
    save_registry(registry)
    listener = None
    process = None
    try:
        FORWARD.write_text("1\n")
        command(["ip", "netns", "add", info["netns"]])
        info["made_netns"] = True
        save_registry(registry)
        command(["ip", "link", "add", info["veth"], "type", "veth", "peer", "name", "vpnpeer"])
        info["made_veth"] = True
        save_registry(registry)
        command(["ip", "link", "set", "vpnpeer", "netns", info["netns"]])
        command(["ip", "addr", "add", f"{info['gateway']}/30", "dev", info["veth"]])
        command(["ip", "link", "set", info["veth"], "up"])
        for args in (("link", "set", "lo", "up"), ("link", "set", "vpnpeer", "name", "eth0"),
                     ("addr", "add", f"{info['address']}/30", "dev", "eth0"),
                     ("link", "set", "eth0", "up"), ("route", "add", "default", "via", info["gateway"])):
            command(["ip", "-n", info["netns"], *args])
        command(["ip", "netns", "exec", info["netns"], "sysctl", "-q", "-w", "net.ipv6.conf.all.disable_ipv6=1"])
        rules = f'''table ip {info['table']} {{
 chain postrouting {{ type nat hook postrouting priority srcnat; policy accept;
  ip saddr {info['address']} masquerade
 }}
 chain forward {{ type filter hook forward priority 0; policy accept;
  iifname "{info['veth']}" accept
  oifname "{info['veth']}" ct state established,related accept
 }}
}}\n'''
        command(["nft", "-f", "-"], input=rules, text=True)
        info["made_table"] = True
        save_registry(registry)
        info["firewalld_zone"] = permit_firewalld_forwarding(info["veth"])
        save_registry(registry)
        socket_directory = RUN / name
        socket_directory.mkdir(mode=0o755, exist_ok=True)
        socket_path(name).unlink(missing_ok=True)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener.bind(str(socket_path(name)))
        os.chown(socket_path(name), user.pw_uid, user.pw_gid)
        socket_path(name).chmod(0o600)
        listener.listen(64)
        with open(logs / "keeper.log", "ab", buffering=0) as log:
            process = subprocess.Popen(["ip", "netns", "exec", info["netns"], "unshare",
                "--mount", "--pid", "--fork", "--kill-child", "--mount-proc",
                sys.executable, "-I", str(STATE / "launcher.py"), "--keeper", name, str(listener.fileno())],
                pass_fds=(listener.fileno(),), env=SAFE_ENV, stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, start_new_session=True, cwd="/")
        info.update(pid=process.pid, start=process_start(process.pid))
        save_registry(registry)
        deadline = time.monotonic() + 300
        while not ready.exists():
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError(f"{name}: keeper failed or timed out; inspect {logs}/keeper.log")
            time.sleep(0.2)
        result = json.loads(ready.read_text())
        if not result.get("ready"):
            raise RuntimeError(f"{name}: {result.get('error')}; logs: {logs}")
        info["exit"] = result["exit"]
        save_registry(registry)
        print(f"{name}: connected to {info['exit']}; link={info['subnet']}; run: nordvpn-ns-run {name} -- COMMAND")
    except BaseException:
        stop_instance(name, registry, keep_files=True)
        if process is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
        raise
    finally:
        if listener is not None:
            listener.close()


def setup_main():
    parser = argparse.ArgumentParser(description="Create independent regular NordVPN/NordLynx instances and non-sudo launchers.")
    parser.add_argument("action", nargs="?", choices=("start", "stop", "status", "check"), default="start")
    parser.add_argument("names", nargs="*")
    parser.add_argument("--user", default=os.environ.get("SUDO_USER"))
    parser.add_argument("--instance", action="append", default=[], metavar="NAME:EXIT",
                        help="country, city, server, or random (a random available country)")
    parser.add_argument("--all", action="store_true", help="stop all instances")
    parser.add_argument("--config", default=str(pathlib.Path(__file__).with_name("nordvpn-ns-config.json")),
                        help="JSON configuration containing candidate address_pools")
    parser.add_argument("--subnet", metavar="CIDR",
                        help="allocate instance /30 links only from this private subnet; overrides config pools")
    args = parser.parse_args()
    try:
        instances = [parse_instance(item) for item in args.instance]
        for name in args.names:
            checked_name(name)
        if len({name for name, _server in instances}) != len(instances):
            raise ValueError("Duplicate instance name.")
        required = ("ip", "nft", "unshare", "mount", "chattr", "sysctl", "nordvpn", "nordvpnd")
        missing = [name for name in required if not shutil.which(name, path=SAFE_ENV["PATH"])]
        if missing:
            raise RuntimeError("Missing commands: " + ", ".join(missing))
        if args.subnet is not None and args.action not in ("start", "check"):
            raise ValueError("--subnet applies only to start or check.")
        pools = select_pools(args.config, args.subnet) if args.action in ("start", "check") else None
        if args.action == "check":
            allocation = choose_network({"instances": {}}, pools)
            source = f"--subnet {args.subnet}" if args.subnet is not None else f"Config: {args.config}"
            print(f"{source}; candidate free link: {allocation['subnet']} (no resources created).")
            print("Required commands found. Live setup requires root, a logged-in NordVPN app, and host forwarding allowed by its firewall.")
            return 0
        if os.geteuid() != 0:
            raise RuntimeError("Run setup with sudo. nordvpn-ns-run needs no sudo.")
        root_directories()
        with open(RUN / "setup.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            registry = read_registry()
            if args.action == "status":
                for name, info in registry["instances"].items():
                    state = "stopped (run setup stop to clean up)"
                    if alive(info):
                        try:
                            with rpc(name, {"op": "status"}) as connection:
                                connection.settimeout(3)
                                result = json.loads(connection.recv(4096))
                                state = "connected" if result.get("connected") else "VPN unavailable"
                        except OSError:
                            state = "launcher unavailable"
                    print(f"{name}: {state}; initial_exit={info.get('exit', info['server'])} link={info.get('subnet', 'legacy')} uid={info['uid']}")
                return 0
            if args.action == "stop":
                names = list(registry["instances"]) if args.all else args.names
                if not names and not args.all:
                    raise ValueError("Supply instance names or --all.")
                for name in names:
                    if name not in registry["instances"]:
                        raise ValueError(f"Unknown instance: {name}")
                    stop_instance(name, registry)
                    print(f"{name}: stopped; temporary login, settings and logs removed")
                if args.all:
                    # A failed setup retains files for diagnosis, but has
                    # already rolled back its networking and registry entry.
                    for path in STATE.iterdir():
                        if path.is_dir() and NAME.fullmatch(path.name):
                            remove_instance_files(path.name)
                return 0
            if not instances or not args.user:
                raise ValueError("Supply --user USER and one or more --instance NAME:SERVER options.")
            user = pwd.getpwnam(args.user)
            if user.pw_uid == 0:
                raise ValueError("Choose a non-root user for launched commands.")
            route = command(["ip", "route", "get", "1.1.1.1"], capture_output=True, text=True).stdout
            if re.search(r"\bdev (nordlynx|tun\d+)\b", route):
                raise RuntimeError("Disconnect the host VPN first; this setup expects a normal host internet route.")
            for name in (".config", ".cache"):
                if not (pathlib.Path(user.pw_dir) / name).is_dir():
                    raise ValueError(f"Create ~/{name} as {user.pw_name} before setup.")
            # Detach persistent root processes from this user-writable repository.
            destination = STATE / "launcher.py"
            if not registry["instances"]:
                shutil.copyfile(__file__, destination)
                destination.chmod(0o600)
            started = []
            try:
                for name, server in instances:
                    start_instance(name, server, user, registry, pools)
                    started.append(name)
            except BaseException:
                for name in reversed(started):
                    stop_instance(name, registry)
                raise
        return 0
    except (Exception, KeyboardInterrupt) as error:
        print(f"nordvpn-ns-setup: {error}", file=sys.stderr)
        return 1


def client_main():
    parser = argparse.ArgumentParser(description="Run as yourself inside a prepared NordVPN instance; no sudo.")
    parser.add_argument("name")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    descriptors = [0, 1, 2]
    master = slave = None
    saved_terminal = None
    connection = None
    saved_signals = {}
    try:
        checked_name(args.name)
        argv = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
        # REMAINDER intentionally preserves command flags and literal arguments.
        if argv == ["--status"] or args.status:
            with rpc(args.name, {"op": "status"}) as connection:
                connection.settimeout(5)
                result = json.loads(connection.recv(4096))
            print("connected" if result.get("connected") else "VPN unavailable")
            return 0 if result.get("connected") else 1
        if not argv:
            raise ValueError("Supply -- COMMAND [ARGUMENT ...].")
        if sys.stdin.isatty() and sys.stdout.isatty():
            master, slave = pty.openpty()
            fcntl.ioctl(slave, termios.TIOCSWINSZ, fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))
            descriptors = [slave, slave, slave]
        request = {"op": "run", "argv": argv, "cwd": os.getcwd(), "env": dict(os.environ)}
        validate_run(request)
        connection = rpc(args.name, request, descriptors)
        if slave is not None:
            os.close(slave)
            slave = None
            saved_terminal = termios.tcgetattr(0)
            tty.setraw(0)

        def forward_signal(sig, _frame):
            with contextlib.suppress(OSError):
                connection.send(json.dumps({"signal": sig}).encode())

        def resize(_sig, _frame):
            if master is not None:
                with contextlib.suppress(OSError):
                    fcntl.ioctl(master, termios.TIOCSWINSZ, fcntl.ioctl(0, termios.TIOCGWINSZ, b"\0" * 8))

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGWINCH):
            saved_signals[sig] = signal.signal(sig, resize if sig == signal.SIGWINCH else forward_signal)
        stdin_open = master is not None
        while True:
            readers = [connection]
            if master is not None:
                readers.append(master)
                if stdin_open:
                    readers.append(0)
            ready = select.select(readers, [], [connection])[0]
            if master is not None and master in ready:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    data = b""
                if data:
                    os.write(1, data)
                else:
                    os.close(master)
                    master = None
                    stdin_open = False
            if stdin_open and 0 in ready:
                data = os.read(0, 65536)
                if data:
                    os.write(master, data)
                else:
                    stdin_open = False
            if connection in ready:
                packet = connection.recv(4096)
                if not packet:
                    raise RuntimeError("Launcher disconnected.")
                result = json.loads(packet)
                if "error" in result:
                    raise RuntimeError(result["error"])
                # Drain remaining PTY output after the exit notification.
                if master is not None:
                    while select.select([master], [], [], 0.05)[0]:
                        try:
                            data = os.read(master, 65536)
                        except OSError:
                            break
                        if not data:
                            break
                        os.write(1, data)
                return result["exit"]
    except (Exception, KeyboardInterrupt) as error:
        print(f"nordvpn-ns-run: {error}", file=sys.stderr)
        return 1
    finally:
        if saved_terminal is not None:
            termios.tcsetattr(0, termios.TCSADRAIN, saved_terminal)
        for sig, handler in saved_signals.items():
            signal.signal(sig, handler)
        for fd in (master, slave):
            if fd is not None:
                os.close(fd)
        if connection is not None:
            connection.close()


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--keeper" and os.geteuid() == 0:
        raise SystemExit(keeper_main(checked_name(sys.argv[2]), int(sys.argv[3])))
    raise SystemExit("Use nordvpn-ns-setup or nordvpn-ns-run.")
