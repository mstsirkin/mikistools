"""Unprivileged protocol/process tests; these do not claim live VPN coverage."""
import array
import importlib.util
import ipaddress
import json
import os
import pathlib
import pwd
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("nordvpn_ns", pathlib.Path(__file__).resolve().parents[1] / "nordvpn_ns.py")
vpn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vpn)


class ValidationTests(unittest.TestCase):
    def test_bootstrap_enables_kill_switch_only_after_connected_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "logs").mkdir()
            events = []
            def cli(user, *args):
                events.append(args)
                return subprocess.CompletedProcess([], 0, "ok")
            def route():
                events.append(("route-check",))
                return True
            with mock.patch.object(vpn, "nord_cli", side_effect=cli), mock.patch.object(vpn, "vpn_connected", side_effect=route):
                self.assertEqual(vpn.connect_private_vpn("user", "Germany", root), "Germany")
            self.assertLess(events.index(("set", "killswitch", "off")), events.index(("connect", "Germany")))
            self.assertLess(events.index(("set", "fwmark", "0xe1f1")), events.index(("connect", "Germany")))
            self.assertLess(events.index(("connect", "Germany")), events.index(("route-check",)))
            self.assertEqual(events[-1], ("set", "killswitch", "on"))

    def test_failed_connection_identifies_command_and_does_not_complete_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "logs").mkdir()
            def cli(user, *args):
                return subprocess.CompletedProcess([], 1 if args[0] == "connect" else 0, "connection timed out")
            with mock.patch.object(vpn, "nord_cli", side_effect=cli), mock.patch.object(vpn, "vpn_connected") as route:
                with self.assertRaisesRegex(RuntimeError, "nordvpn connect Azerbaijan failed"):
                    vpn.connect_private_vpn("user", "Azerbaijan", root)
                route.assert_not_called()
            self.assertIn("nordvpn connect Azerbaijan", (root / "logs/setup.log").read_text())

    def test_firewalld_runtime_assignment_only_targets_owned_veth(self):
        with mock.patch.object(vpn.shutil, "which", return_value="/usr/bin/firewall-cmd"), \
             mock.patch.object(vpn.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "running", "")), \
             mock.patch.object(vpn, "command", return_value=subprocess.CompletedProcess([], 0, "trusted\n  target: ACCEPT\n")) as command:
            self.assertEqual(vpn.permit_firewalld_forwarding("mnv1"), "trusted")
            self.assertEqual(command.call_args.args[0], ["firewall-cmd", "--zone=trusted", "--add-interface=mnv1"])
            self.assertNotIn("--permanent", command.call_args.args[0])
        info = {"veth": "mnv1", "table": "t", "netns": "n", "firewalld_zone": "trusted"}
        with mock.patch.object(vpn.subprocess, "run") as run:
            vpn.remove_network(info)
            self.assertEqual(run.call_args.args[0], ["firewall-cmd", "--zone=trusted", "--remove-interface=mnv1"])

    def test_inactive_firewalld_is_not_modified(self):
        with mock.patch.object(vpn.shutil, "which", return_value="/usr/bin/firewall-cmd"), \
             mock.patch.object(vpn.subprocess, "run", return_value=subprocess.CompletedProcess([], 252, "", "FirewallD is not running")), \
             mock.patch.object(vpn, "command") as command:
            self.assertIsNone(vpn.permit_firewalld_forwarding("mnv1"))
            command.assert_not_called()

    def test_subnet_override_does_not_read_config(self):
        with mock.patch.object(vpn, "load_pools") as load:
            pools = vpn.select_pools("missing.json", "172.30.204.0/24")
            self.assertEqual([str(pool) for pool in pools], ["172.30.204.0/24"])
            load.assert_not_called()

    def test_no_subnet_override_uses_config(self):
        with mock.patch.object(vpn, "load_pools", return_value=["configured"]) as load:
            self.assertEqual(vpn.select_pools("config.json", None), ["configured"])
            load.assert_called_once_with("config.json")

    def test_subnet_override_is_validated(self):
        for value in ("8.8.8.0/24", "172.30.204.1/24", "172.30.204.0/31", "not-a-subnet"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                vpn.select_pools("missing.json", value)

    def test_pool_config_accepts_private_ranges_and_rejects_other_values(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.json"
            path.write_text(json.dumps({"address_pools": ["172.30.203.0/24", "192.168.203.0/24", "10.203.0.0/16"]}))
            self.assertEqual([str(pool) for pool in vpn.load_pools(path)], ["172.30.203.0/24", "192.168.203.0/24", "10.203.0.0/16"])
            for value in ({"address_pools": []}, {"address_pools": ["8.8.8.0/24"]},
                          {"address_pools": ["127.0.0.0/24"]}, {"address_pools": ["172.30.203.1/24"]},
                          {"address_pools": ["10.0.0.0/8"]}, {"address_pools": ["fd00::/64"]},
                          {"address_pools": [1]}, {"address_pools": ["172.30.203.0/24"], "typo": True}):
                path.write_text(json.dumps(value))
                with self.subTest(value=value), self.assertRaises(ValueError):
                    vpn.load_pools(path)

    def test_allocator_skips_broad_tunnel_route(self):
        routes = [{"dst": "10.0.0.0/8", "dev": "wt0", "table": 7120},
                  {"dst": "default"}, {"dst": "192.168.1.0/24"}]
        pools = [ipaddress.ip_network(value) for value in ("10.203.0.0/16", "172.30.203.0/24")]
        with mock.patch.object(vpn, "command", return_value=subprocess.CompletedProcess([], 0, json.dumps(routes))):
            allocated = vpn.choose_network({"instances": {}}, pools)
        self.assertEqual(allocated, {"slot": 1, "subnet": "172.30.203.0/30", "gateway": "172.30.203.1", "address": "172.30.203.2"})

    def test_allocator_uses_second_pool_when_first_is_covered(self):
        routes = [{"dst": "172.16.0.0/12"}, {"dst": "10.0.0.0/8"}]
        pools = [ipaddress.ip_network(value) for value in ("172.30.203.0/24", "192.168.203.0/24")]
        with mock.patch.object(vpn, "command", return_value=subprocess.CompletedProcess([], 0, json.dumps(routes))):
            self.assertEqual(vpn.choose_network({"instances": {}}, pools)["subnet"], "192.168.203.0/30")

    def test_allocator_assigns_ten_distinct_links_and_avoids_partial_conflicts(self):
        routes = [{"dst": "172.30.203.0/29"}, {"dst": "default"}]
        pool = ipaddress.ip_network("172.30.203.0/24")
        registry = {"instances": {}}
        with mock.patch.object(vpn, "command", return_value=subprocess.CompletedProcess([], 0, json.dumps(routes))):
            for index in range(10):
                registry["instances"][f"tango{index + 1}"] = vpn.choose_network(registry, [pool])
        links = [ipaddress.ip_network(item["subnet"]) for item in registry["instances"].values()]
        self.assertEqual(str(links[0]), "172.30.203.8/30")
        self.assertEqual(len(set(links)), 10)
        self.assertEqual({item["slot"] for item in registry["instances"].values()}, set(range(1, 11)))

    def test_allocator_reports_all_pools_blocked(self):
        routes = [{"dst": "172.30.203.0/24"}]
        with mock.patch.object(vpn, "command", return_value=subprocess.CompletedProcess([], 0, json.dumps(routes))):
            with self.assertRaisesRegex(RuntimeError, "configured address_pools"):
                vpn.choose_network({"instances": {}}, [ipaddress.ip_network("172.30.203.0/24")])

    def test_all_instance_state_is_under_run(self):
        self.assertEqual(vpn.STATE, vpn.RUN / "state")
        self.assertEqual(vpn.RUN.parts[:2], ("/", "run"))

    def test_private_run_preserves_only_our_tree_with_inherited_fd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "runtime"
            root.mkdir()
            (root / "state").mkdir()
            calls = []
            def mount(args, **kwargs):
                calls.append((args, kwargs))
                if len(calls) == 1:
                    # Simulate the host /run tree becoming hidden by a mount.
                    root.rename(root.with_name("old-runtime"))
                else:
                    fd = kwargs["pass_fds"][0]
                    self.assertEqual(os.readlink(f"/proc/self/fd/{fd}"), str(root.with_name("old-runtime")))
                    self.assertEqual(args, ["mount", "--no-canonicalize", "--bind", f"/proc/self/fd/{fd}", str(root)])
            with mock.patch.object(vpn, "RUN", root), mock.patch.object(vpn, "STATE", root.with_name("old-runtime") / "state"), mock.patch.object(vpn, "command", side_effect=mount):
                vpn.private_run()
            self.assertEqual(calls[0][0][-1], "/run")
            with self.assertRaises(OSError):
                os.fstat(calls[1][1]["pass_fds"][0])

    def test_readiness_survives_original_directory_becoming_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            visible = root / "instance"
            visible.mkdir()
            fd = os.open(visible, os.O_RDONLY | os.O_DIRECTORY)
            try:
                hidden = root / "old-instance"
                visible.rename(hidden)
                visible.mkdir()
                vpn.write_readiness(fd, {"error": "mount failed"})
                self.assertEqual(json.loads((hidden / "ready.json").read_text()), {"error": "mount failed"})
                self.assertFalse((visible / "ready.json").exists())
                self.assertEqual((hidden / "ready.json").stat().st_mode & 0o777, 0o600)
            finally:
                os.close(fd)

    def test_stop_removes_private_state_and_runtime_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            state = root / "state"
            (state / "tango1/data").mkdir(parents=True)
            (state / "tango1/data/login").write_text("private")
            (root / "tango1").mkdir()
            (root / "tango1/control.sock").touch()
            registry = {"instances": {"tango1": {"table": "a", "netns": "a", "veth": "a"}}, "forward_before": None}
            with mock.patch.object(vpn, "STATE", state), mock.patch.object(vpn, "RUN", root):
                vpn.stop_instance("tango1", registry)
            self.assertFalse((state / "tango1").exists())
            self.assertFalse((root / "tango1").exists())
            self.assertEqual(registry["instances"], {})

    def test_random_country_list_handles_colors_columns_and_spaces(self):
        output = "\x1b[32mGermany\x1b[0m    United States\nNetherlands\nGermany\nUnited_Kingdom\n"
        self.assertEqual(vpn.parse_countries(output), ["Germany", "United States", "Netherlands", "United_Kingdom"])

    def test_random_exit_uses_available_countries(self):
        result = subprocess.CompletedProcess([], 0, "Germany\nNetherlands\n")
        with mock.patch.object(vpn, "nord_cli", return_value=result) as cli, mock.patch.object(vpn.secrets, "choice", return_value="Netherlands") as choose:
            self.assertEqual(vpn.resolve_exit("random", "user"), "Netherlands")
            cli.assert_called_once_with("user", "countries")
            choose.assert_called_once_with(["Germany", "Netherlands"])

    def test_explicit_exit_does_not_query_countries(self):
        with mock.patch.object(vpn, "nord_cli") as cli:
            self.assertEqual(vpn.resolve_exit("Germany", "user"), "Germany")
            cli.assert_not_called()

    def test_random_exit_refuses_failed_empty_or_unexpected_lists(self):
        for result in (subprocess.CompletedProcess([], 1, "Permission denied"),
                       subprocess.CompletedProcess([], 0, ""),
                       subprocess.CompletedProcess([], 0, "Error: unavailable")):
            with self.subTest(result=result), mock.patch.object(vpn, "nord_cli", return_value=result), self.assertRaises(RuntimeError):
                vpn.resolve_exit("random", "user")

    def test_instance_validation(self):
        self.assertEqual(vpn.parse_instance("tango1:us1234"), ("tango1", "us1234"))
        self.assertEqual(vpn.parse_instance("tango1:random"), ("tango1", "random"))
        for value in ("../escape:us1", "Tango:us1", "tango:--help", "tango:us1;id", "tango"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                vpn.parse_instance(value)

    def test_literal_arguments_are_allowed(self):
        request = {"argv": ["echo", "$(touch /tmp/nope)", "a; b", "--flag"], "cwd": "/", "env": {"PATH": "/bin"}}
        vpn.validate_run(request)

    def test_malformed_exec_requests(self):
        good = {"argv": ["true"], "cwd": "/", "env": {"PATH": "/bin"}}
        for override in ({"argv": []}, {"argv": ["x\0y"]}, {"cwd": "relative"}, {"env": {"A=B": "c"}}, {"env": {"X": 1}}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                vpn.validate_run(dict(good, **override))

    def test_route_check_requires_actual_interface(self):
        for output, expected in (("1.1.1.1 dev nordlynx table 205", True), ("1.1.1.1 dev eth0", False), ("1.1.1.1 dev nordlynx-fake", False)):
            with mock.patch.object(vpn.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output)):
                self.assertEqual(vpn.vpn_connected(), expected)

    def test_cleanup_only_deletes_created_resources(self):
        info = {"table": "t", "netns": "n", "veth": "v", "made_netns": True}
        with mock.patch.object(vpn.subprocess, "run") as run:
            vpn.remove_network(info)
        self.assertEqual([tuple(call.args[0]) for call in run.call_args_list], [("ip", "netns", "delete", "n")])

    def test_forwarding_restored_only_after_last_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            forwarding = root / "forward"
            forwarding.write_text("1\n")
            registry = {"instances": {"one": {"table": "a", "netns": "a", "veth": "a"},
                                       "two": {"table": "b", "netns": "b", "veth": "b"}}, "forward_before": "0"}
            with mock.patch.object(vpn, "STATE", root), mock.patch.object(vpn, "RUN", root), mock.patch.object(vpn, "FORWARD", forwarding):
                vpn.stop_instance("one", registry)
                self.assertEqual(forwarding.read_text(), "1\n")
                vpn.stop_instance("two", registry)
                self.assertEqual(forwarding.read_text(), "0\n")

    def test_root_environment_is_not_client_environment(self):
        with mock.patch.object(vpn.subprocess, "run") as run:
            vpn.command(["true"])
        self.assertNotIn("LD_PRELOAD", run.call_args.kwargs["env"])
        self.assertNotIn("PYTHONPATH", run.call_args.kwargs["env"])


class ProtocolTests(unittest.TestCase):
    def test_peer_identity_is_kernel_supplied(self):
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with server, client:
            client.send(json.dumps({"op": "status", "uid": 0}).encode())
            if os.getuid() != 0:
                with self.assertRaises(PermissionError):
                    vpn.receive_request(server, os.getuid() + 1)

    def test_passed_descriptors_and_request(self):
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with server, client, open(os.devnull, "rb") as null:
            request = {"op": "run", "argv": ["echo", "a; b"], "cwd": "/", "env": {}}
            client.sendmsg([json.dumps(request).encode()], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [null.fileno()] * 3))])
            parsed, fds = vpn.receive_request(server, os.getuid())
            try:
                self.assertEqual(parsed, request)
                self.assertEqual(len(fds), 3)
                for fd in fds:
                    os.fstat(fd)
            finally:
                for fd in fds:
                    os.close(fd)

    def test_reject_missing_descriptors(self):
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with server, client:
            client.send(json.dumps({"op": "run", "argv": ["true"], "cwd": "/", "env": {}}).encode())
            with self.assertRaises(ValueError):
                vpn.receive_request(server, os.getuid())

    def test_reject_truncated_packet(self):
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with server, client, mock.patch.object(vpn, "MAX_REQUEST", 64):
            client.send(json.dumps({"op": "status", "padding": "x" * 100}).encode())
            with self.assertRaisesRegex(ValueError, "Truncated"):
                vpn.receive_request(server, os.getuid())

    def test_command_runs_after_drop_with_exact_argv_env_cwd_exit(self):
        # Fork/exec is real. Privilege changes are mocked because tests run as mst.
        with tempfile.TemporaryDirectory() as directory, open(os.devnull, "rb") as null:
            output = pathlib.Path(directory) / "output"
            with output.open("wb") as stream:
                marker = pathlib.Path(directory) / "dropped"
                request = {"argv": [sys.executable, "-c", "import os,sys; print(os.getcwd()); print(os.environ['SPECIAL']); print(sys.argv[1]); print(os.environ['DROPPED']); sys.exit(7)", "$(false); literal"],
                           "cwd": directory, "env": {"SPECIAL": "a\nb", "DROPPED": "yes"}}
                def dropped(_user):
                    marker.write_text("dropped")
                with mock.patch.object(vpn, "drop_user", dropped):
                    child = vpn.launch_user(request, [null.fileno(), stream.fileno(), stream.fileno()], pwd.getpwuid(os.getuid()))
                _pid, status = os.waitpid(child, 0)
            self.assertEqual(os.waitstatus_to_exitcode(status), 7)
            self.assertTrue(marker.exists())
            self.assertEqual(output.read_text(), directory + "\na\nb\n$(false); literal\nyes\n")

    def test_actual_handler_returns_child_exit(self):
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with open(os.devnull, "rb") as null, tempfile.TemporaryFile() as output:
            client.sendmsg([json.dumps({"op": "run", "argv": ["/bin/sh", "-c", "printf hello; exit 9"], "cwd": "/", "env": {}}).encode()],
                           [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [null.fileno(), output.fileno(), output.fileno()]))])
            pid = os.fork()
            if pid == 0:
                client.close()
                with mock.patch.object(vpn, "vpn_connected", return_value=True), mock.patch.object(vpn, "drop_user"):
                    vpn.serve_connection(server, listener, pwd.getpwuid(os.getuid()))
                os._exit(0)
            server.close()
            listener.close()
            try:
                client.settimeout(5)
                self.assertEqual(json.loads(client.recv(4096)), {"exit": 9})
                output.seek(0)
                self.assertEqual(output.read(), b"hello")
            finally:
                client.close()
                os.waitpid(pid, 0)

    def test_no_command_when_vpn_unavailable(self):
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        with client, open(os.devnull, "rb") as null:
            client.sendmsg([json.dumps({"op": "run", "argv": ["true"], "cwd": "/", "env": {}}).encode()],
                           [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [null.fileno()] * 3))])
            with mock.patch.object(vpn, "vpn_connected", return_value=False), mock.patch.object(vpn, "launch_user") as launch:
                vpn.serve_connection(server, listener, pwd.getpwuid(os.getuid()))
            self.assertIn("not started", json.loads(client.recv(4096))["error"])
            launch.assert_not_called()


class MountNamespaceTests(unittest.TestCase):
    def test_real_runtime_bind_survives_private_run(self):
        # This uses user-namespace root only: host mounts/network stay untouched.
        probe = subprocess.run(["unshare", "--user", "--map-root-user", "--mount", "--net", "true"],
                               capture_output=True, text=True)
        if probe.returncode:
            self.skipTest("Unprivileged user/mount namespaces unavailable")
        module_path = pathlib.Path(vpn.__file__).resolve()
        program = f'''
import importlib.util, os
spec = importlib.util.spec_from_file_location("vpn", {str(module_path)!r})
vpn = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vpn)
vpn.command(["mount", "--make-rprivate", "/"])
vpn.command(["mount", "-t", "tmpfs", "-o", "mode=755", "tmpfs", "/run"])
vpn.root_directories()
directory = vpn.STATE / "probe"
directory.mkdir()
(directory / "marker").write_text("original runtime")
fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
vpn.private_run()
assert (directory / "marker").read_text() == "original runtime"
vpn.write_readiness(fd, {{"ready": True}})
assert (directory / "ready.json").exists()
os.close(fd)
'''
        result = subprocess.run(["unshare", "--user", "--map-root-user", "--mount", "--net",
                                 sys.executable, "-I", "-c", program],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
