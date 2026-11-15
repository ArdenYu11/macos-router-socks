#!/usr/bin/env python3
"""Loopback SOCKS5 CONNECT server with router DNS and macOS interface binding."""

import argparse
from dataclasses import dataclass
import errno
import fcntl
import ipaddress
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import select
import signal
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time


# Darwin's <netinet/in.h>. These options constrain route selection to the
# chosen physical interface; binding only a source IP does not bypass TUN.
IP_BOUND_IF = 25
IPV6_BOUND_IF = 125
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
LOG = logging.getLogger("router-socks")
VIRTUAL_PREFIXES = ("lo", "utun", "tun", "tap", "bridge", "awdl", "llw", "gif", "stf", "ipsec", "vlan")


def network_command(arguments):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=3)
    if result.returncode:
        raise OSError(errno.ENETUNREACH, "Network query failed: %s" % " ".join(arguments))
    return result.stdout


def physical_devices(output):
    return {
        match.group(1) for match in re.finditer(r"^Device:\s*(\S+)", output, re.MULTILINE)
        if not match.group(1).startswith(VIRTUAL_PREFIXES)
    }


def active_ipv4_interfaces(output):
    interfaces = {}
    blocks = re.split(r"(?=^\S+: flags=)", output, flags=re.MULTILINE)
    for block in blocks:
        header = re.match(r"(\S+): flags=[0-9a-fA-F]+<([^>]*)>", block)
        if not header or "UP" not in header.group(2).split(","):
            continue
        if re.search(r"^\s*status:\s*inactive", block, re.MULTILINE):
            continue
        addresses = []
        for address, mask in re.findall(r"^\s*inet\s+([\d.]+)\s+netmask\s+(\S+)", block, re.MULTILINE):
            try:
                if mask.startswith("0x"):
                    mask = str(ipaddress.IPv4Address(int(mask, 16)))
                ip = ipaddress.IPv4Interface("%s/%s" % (address, mask))
                if (ip.ip.is_unspecified or ip.ip.is_loopback or ip.ip.is_link_local
                        or ip.ip.is_multicast or ip.ip in FAKE_IP_NETWORK):
                    continue
                addresses.append(ip)
            except ValueError:
                continue
        if addresses:
            interfaces[header.group(1)] = addresses
    return interfaces


def route_interface(destination):
    try:
        output = network_command(["/sbin/route", "-n", "get", destination])
        match = re.search(r"^\s*interface:\s*(\S+)", output, re.MULTILINE)
        return match.group(1) if match else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def detect_physical_interface(router, requested="auto"):
    devices = physical_devices(network_command(["/usr/sbin/networksetup", "-listallhardwareports"]))
    active = active_ipv4_interfaces(network_command(["/sbin/ifconfig"]))
    candidates = {name: addresses for name, addresses in active.items() if name in devices}
    if requested != "auto":
        if requested not in candidates:
            raise OSError(errno.ENETUNREACH, "Physical interface %s is unavailable or has no usable IPv4 address" % requested)
        return requested
    if not candidates:
        raise OSError(errno.ENETUNREACH, "No active physical IPv4 interface; waiting for network")

    # The route to the LAN router remains useful even when default routes
    # have been replaced by TUN. Only names in the hardware inventory qualify.
    router_route = route_interface(router)
    if router_route in candidates:
        return router_route
    router_ip = ipaddress.IPv4Address(router)
    same_lan = [name for name, addresses in candidates.items()
                if any(router_ip in address.network for address in addresses)]
    if len(same_lan) == 1:
        return same_lan[0]
    default_route = route_interface("default")
    if default_route in (same_lan or candidates):
        return default_route
    if len(candidates) == 1:
        return next(iter(candidates))
    raise OSError(errno.ENETUNREACH, "Ambiguous physical interfaces (%s); specify --interface" % ", ".join(sorted(candidates)))


@dataclass(frozen=True)
class PhysicalInterface:
    name: str
    index: int


class InterfaceSelector:
    def __init__(self, router, requested="auto", refresh_interval=5):
        self.router = router
        self.requested = requested
        self.refresh_interval = refresh_interval
        self.current = None
        self.next_refresh = 0
        self.lock = threading.Lock()

    def select(self):
        with self.lock:
            now = time.monotonic()
            if self.current is not None and now < self.next_refresh:
                return self.current
            try:
                name = detect_physical_interface(self.router, self.requested)
                selected = PhysicalInterface(name, socket.if_nametoindex(name))
            except subprocess.TimeoutExpired as exc:
                self.current = None
                raise OSError(errno.ENETUNREACH, "Physical interface detection timed out") from exc
            except OSError:
                self.current = None
                raise
            if selected != self.current:
                LOG.info("Physical interface selected: %s (%d), router DNS=%s", selected.name, selected.index, self.router)
            self.current = selected
            self.next_refresh = now + self.refresh_interval
            return selected


class PidLock:
    def __init__(self, pid_file):
        self.pid_file = Path(pid_file)
        self.handle = None

    def __enter__(self):
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.pid_file.with_suffix(".lock").open("a+")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise OSError(errno.EALREADY, "A SOCKS process already holds %s" % self.pid_file)
        try:
            self.pid_file.write_text(str(os.getpid()) + "\n")
        except Exception:
            self.handle.close()
            self.handle = None
            raise
        return self

    def __exit__(self, *_):
        try:
            if self.pid_file.exists() and self.pid_file.read_text().strip() == str(os.getpid()):
                self.pid_file.unlink()
        finally:
            if self.handle is not None:
                self.handle.close()


def read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise EOFError("Connection closed")
        data.extend(chunk)
    return bytes(data)


def bound_socket(family, sock_type, interface_index, timeout):
    sock = socket.socket(family, sock_type)
    try:
        if family == socket.AF_INET:
            sock.setsockopt(socket.IPPROTO_IP, IP_BOUND_IF, interface_index)
        else:
            sock.setsockopt(socket.IPPROTO_IPV6, IPV6_BOUND_IF, interface_index)
        sock.settimeout(timeout)
        return sock
    except Exception:
        sock.close()
        raise


def skip_dns_name(packet, offset):
    while True:
        if offset >= len(packet):
            raise ValueError("Truncated DNS name")
        length = packet[offset]
        offset += 1
        if length == 0:
            return offset
        if length & 0xC0 == 0xC0:
            if offset >= len(packet):
                raise ValueError("Truncated DNS pointer")
            return offset + 1
        if length & 0xC0 or offset + length > len(packet):
            raise ValueError("Invalid DNS name")
        offset += length


def dns_packet(host, query_id):
    labels = host.rstrip(".").encode("idna").split(b".")
    if any(not label or len(label) > 63 for label in labels):
        raise ValueError("Invalid domain name")
    name = b"".join(bytes([len(label)]) + label for label in labels) + b"\x00"
    if len(name) > 255:
        raise ValueError("Domain name too long")
    # RD=1, one A/IN question. This test service resolves domains to IPv4.
    return struct.pack("!6H", query_id, 0x0100, 1, 0, 0, 0) + name + struct.pack("!HH", 1, 1)


class RouterResolver:
    def __init__(self, router, timeout):
        self.router = str(ipaddress.IPv4Address(router))
        self.timeout = timeout

    def resolve(self, host, interface):
        query_id = int.from_bytes(os.urandom(2), "big")
        query = dns_packet(host, query_id)
        with bound_socket(socket.AF_INET, socket.SOCK_DGRAM,
                          interface.index, self.timeout) as sock:
            sock.connect((self.router, 53))
            sock.send(query)
            packet = sock.recv(65535)

        if len(packet) < 12:
            raise ValueError("Truncated DNS response")
        response_id, flags, questions, answers, _, _ = struct.unpack("!6H", packet[:12])
        if response_id != query_id or not flags & 0x8000:
            raise ValueError("Invalid DNS response")
        if flags & 0x0200:
            with bound_socket(socket.AF_INET, socket.SOCK_STREAM,
                              interface.index, self.timeout) as sock:
                sock.connect((self.router, 53))
                sock.sendall(struct.pack("!H", len(query)) + query)
                packet = read_exact(sock, struct.unpack("!H", read_exact(sock, 2))[0])
            if len(packet) < 12:
                raise ValueError("Truncated TCP DNS response")
            response_id, flags, questions, answers, _, _ = struct.unpack("!6H", packet[:12])
            if response_id != query_id or not flags & 0x8000:
                raise ValueError("Invalid TCP DNS response")
        if flags & 0x000F:
            raise ValueError("Router DNS returned rcode=%d" % (flags & 0x000F))

        offset = 12
        for _ in range(questions):
            offset = skip_dns_name(packet, offset) + 4
            if offset > len(packet):
                raise ValueError("Truncated DNS question")

        addresses = []
        for _ in range(answers):
            offset = skip_dns_name(packet, offset)
            if offset + 10 > len(packet):
                raise ValueError("Truncated DNS record")
            record_type, record_class, _, length = struct.unpack("!HHIH", packet[offset:offset + 10])
            offset += 10
            if offset + length > len(packet):
                raise ValueError("Truncated DNS address")
            if record_type == 1 and record_class == 1 and length == 4:
                address = socket.inet_ntop(socket.AF_INET, packet[offset:offset + length])
                if address not in addresses:
                    addresses.append(address)
            offset += length
        if not addresses:
            raise ValueError("Router DNS returned no IPv4 address for %s" % host)
        LOG.info("DNS %s via %s on %s -> %s", host, self.router, interface.name, ", ".join(addresses))
        return addresses


def connect_target(server, host, port, is_domain):
    interface = server.interface_selector.select()
    addresses = server.resolver.resolve(host, interface) if is_domain else [host]
    error = None
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.version == 4 and ip in FAKE_IP_NETWORK:
            error = ValueError("Received Fake-IP %s; the client must forward the original domain" % ip)
            continue
        family = socket.AF_INET if ip.version == 4 else socket.AF_INET6
        sock = bound_socket(family, socket.SOCK_STREAM, interface.index, server.timeout)
        try:
            sock.connect((str(ip), port))
            LOG.info("CONNECT %s:%d -> %s:%d via %s, local=%s",
                     host, port, ip, port, interface.name, sock.getsockname()[0])
            return sock
        except OSError as exc:
            error = exc
            sock.close()
    raise error or ConnectionError("No usable target address")


def socks_reply(sock, code, upstream=None):
    if upstream is not None:
        address, port = upstream.getsockname()[:2]
        ip = ipaddress.ip_address(address)
        address_type = 1 if ip.version == 4 else 4
        sock.sendall(bytes([5, code, 0, address_type]) + ip.packed + struct.pack("!H", port))
    else:
        sock.sendall(bytes([5, code, 0, 1]) + b"\x00" * 6)


def relay(client, upstream, idle_timeout):
    peers = {client: upstream, upstream: client}
    readers = set(peers)
    while readers:
        ready, _, _ = select.select(list(readers), [], [], idle_timeout)
        if not ready:
            return
        for source in ready:
            data = source.recv(65536)
            if data:
                peers[source].sendall(data)
            else:
                readers.remove(source)
                try:
                    peers[source].shutdown(socket.SHUT_WR)
                except OSError:
                    pass


class SocksHandler(socketserver.BaseRequestHandler):
    def handle(self):
        client = self.request
        client.settimeout(self.server.timeout)
        host, port = "?", 0
        try:
            version, count = read_exact(client, 2)
            if version != 5:
                return
            methods = read_exact(client, count)
            if 0 not in methods:
                client.sendall(b"\x05\xff")
                return
            client.sendall(b"\x05\x00")

            version, command, reserved, address_type = read_exact(client, 4)
            if version != 5 or reserved != 0:
                socks_reply(client, 1)
                return
            if command != 1:
                socks_reply(client, 7)
                return
            is_domain = address_type == 3
            if address_type == 1:
                host = socket.inet_ntop(socket.AF_INET, read_exact(client, 4))
            elif address_type == 3:
                host = read_exact(client, read_exact(client, 1)[0]).decode("idna")
            elif address_type == 4:
                host = socket.inet_ntop(socket.AF_INET6, read_exact(client, 16))
            else:
                socks_reply(client, 8)
                return
            port = struct.unpack("!H", read_exact(client, 2))[0]

            try:
                upstream = connect_target(self.server, host, port, is_domain)
            except (OSError, ValueError) as exc:
                LOG.warning("FAILED %s:%d: %s", host, port, exc)
                socks_reply(client, 5 if isinstance(exc, ConnectionRefusedError) else 4)
                return
            with upstream:
                socks_reply(client, 0, upstream)
                client.settimeout(30)
                upstream.settimeout(30)
                relay(client, upstream, self.server.idle_timeout)
        except (OSError, EOFError, ValueError) as exc:
            LOG.debug("Connection ended %s:%d: %s", host, port, exc)


class SocksServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, args):
        self.interface_selector = InterfaceSelector(args.router_dns, args.interface)
        self.timeout = args.timeout
        self.idle_timeout = args.idle_timeout
        self.resolver = RouterResolver(args.router_dns, args.timeout)
        super().__init__(("127.0.0.1", args.port), SocksHandler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="auto", help="Physical network interface, or auto (default)")
    parser.add_argument("--router-dns", default="192.168.1.1")
    parser.add_argument("--port", type=int, default=10808)
    parser.add_argument("--timeout", type=float, default=6)
    parser.add_argument("--idle-timeout", type=float, default=300)
    parser.add_argument("--pid-file", type=Path, default=Path(__file__).with_name("socks.pid"))
    parser.add_argument("--log-file", type=Path, help="Rotated file log (2 MiB, three backups)")
    parser.add_argument("--check-interface", action="store_true", help="Detect the physical interface and exit")
    args = parser.parse_args()
    handlers = []
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(args.log_file, maxBytes=2 * 1024 * 1024, backupCount=3))
    else:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    if sys.platform != "darwin":
        parser.error("This test uses macOS IP_BOUND_IF; use it on macOS")
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    try:
        args.router_dns = str(ipaddress.IPv4Address(args.router_dns))
    except ValueError:
        parser.error("Router DNS must be an IPv4 address")
    if args.timeout <= 0 or args.idle_timeout <= 0:
        parser.error("Timeout values must be positive")

    def stop_process(signum, _frame):
        LOG.info("Received signal %d", signum)
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_process)
    try:
        if args.check_interface:
            interface = InterfaceSelector(args.router_dns, args.interface).select()
            print("%s (%d)" % (interface.name, interface.index))
            return 0
        with PidLock(args.pid_file), SocksServer(args) as server:
            LOG.info("Listening at socks5://127.0.0.1:%d; DNS=%s; interface=%s; PID=%d",
                     args.port, args.router_dns, args.interface, os.getpid())
            try:
                server.interface_selector.select()
            except OSError as exc:
                LOG.warning("%s; listening and retrying detection on new connections", exc)
            server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        LOG.info("Stopped")
    except (OSError, ValueError) as exc:
        LOG.error("Cannot start: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
