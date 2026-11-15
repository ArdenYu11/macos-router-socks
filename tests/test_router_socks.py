from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

import router_socks
import service_control


HARDWARE = """Hardware Port: Ethernet
Device: en0
Ethernet Address: 00:11:22:33:44:55

Hardware Port: Wi-Fi
Device: en1
Ethernet Address: 00:11:22:33:44:66

Hardware Port: Thunderbolt Bridge
Device: bridge0
"""

INTERFACES = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
    inet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,RUNNING> mtu 1500
    inet 10.20.0.10 netmask 0xffffff00 broadcast 10.20.0.255
    status: active
en1: flags=8863<UP,BROADCAST,RUNNING> mtu 1500
    inet 192.168.50.10 netmask 0xffffff00 broadcast 192.168.50.255
    status: active
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 9000
    inet 198.18.0.1 netmask 0xffff0000
bridge0: flags=8863<UP,BROADCAST,RUNNING> mtu 1500
    inet 192.168.50.11 netmask 0xffffff00 broadcast 192.168.50.255
    status: active
"""


class InterfaceTests(unittest.TestCase):
    def network_query(self, arguments):
        return HARDWARE if arguments[0].endswith("networksetup") else INTERFACES

    def test_router_route_beats_tun_default(self):
        with patch.object(router_socks, "network_command", side_effect=self.network_query), \
                patch.object(router_socks, "route_interface", side_effect=lambda target: "en1" if target != "default" else "utun4"):
            self.assertEqual(router_socks.detect_physical_interface("192.168.50.1"), "en1")

    def test_router_subnet_recovers_when_routes_point_to_tun(self):
        with patch.object(router_socks, "network_command", side_effect=self.network_query), \
                patch.object(router_socks, "route_interface", return_value="utun4"):
            self.assertEqual(router_socks.detect_physical_interface("192.168.50.1"), "en1")

    def test_no_physical_network_fails_instead_of_using_tun(self):
        offline = """en1: flags=8863<UP,BROADCAST> mtu 1500
    inet 192.168.50.10 netmask 0xffffff00
    status: inactive
utun4: flags=8051<UP,POINTOPOINT,RUNNING> mtu 9000
    inet 198.18.0.1 netmask 0xffff0000
"""
        with patch.object(router_socks, "network_command", side_effect=[HARDWARE, offline]):
            with self.assertRaises(OSError):
                router_socks.detect_physical_interface("192.168.50.1")

    def test_manual_virtual_interface_is_rejected(self):
        with patch.object(router_socks, "network_command", side_effect=self.network_query):
            with self.assertRaises(OSError):
                router_socks.detect_physical_interface("192.168.50.1", "utun4")

    def test_ambiguous_interfaces_require_explicit_choice(self):
        with patch.object(router_socks, "network_command", side_effect=self.network_query), \
                patch.object(router_socks, "route_interface", return_value="utun4"):
            with self.assertRaises(OSError):
                router_socks.detect_physical_interface("172.16.0.1")

    def test_network_switch_and_outage_discard_previous_interface(self):
        selector = router_socks.InterfaceSelector("192.168.50.1", refresh_interval=0)
        with patch.object(router_socks, "detect_physical_interface",
                          side_effect=["en1", "en0", OSError("offline"), "en1"]), \
                patch.object(router_socks.socket, "if_nametoindex", side_effect=lambda name: {"en1": 16, "en0": 4}[name]):
            self.assertEqual(selector.select().name, "en1")
            self.assertEqual(selector.select().name, "en0")
            with self.assertRaises(OSError):
                selector.select()
            self.assertIsNone(selector.current)
            self.assertEqual(selector.select().name, "en1")


class LifecycleTests(unittest.TestCase):
    def test_pid_lock_blocks_duplicates_and_can_be_reacquired(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "socks.pid"
            with router_socks.PidLock(pid_file):
                self.assertTrue(pid_file.exists())
                with self.assertRaises(OSError):
                    with router_socks.PidLock(pid_file):
                        pass
            self.assertFalse(pid_file.exists())
            with router_socks.PidLock(pid_file):
                self.assertTrue(pid_file.exists())

    def test_agent_starts_at_login_and_restarts_with_explicit_runtime_paths(self):
        config = {"interface": "auto", "router_dns": "192.168.50.1", "port": 10808}
        plist = service_control.agent_plist(config, "/opt/example/python3")
        decoded = plistlib.loads(plistlib.dumps(plist))
        self.assertTrue(decoded["RunAtLoad"])
        self.assertTrue(decoded["KeepAlive"])
        self.assertGreaterEqual(decoded["ThrottleInterval"], 5)
        self.assertTrue(Path(decoded["ProgramArguments"][2]).is_absolute())
        self.assertIn("--log-file", decoded["ProgramArguments"])
        self.assertEqual(decoded["ProgramArguments"][0], "/opt/example/python3")


if __name__ == "__main__":
    unittest.main()
