#!/usr/bin/env python3
"""Loopback SOCKS5 CONNECT server with router DNS and macOS interface binding."""

import argparse
import ipaddress
import logging
import os
import select
import socket
import socketserver
import struct
import sys


# Darwin's <netinet/in.h>. These options constrain route selection to the
# chosen physical interface; binding only a source IP does not bypass TUN.
IP_BOUND_IF = 25
IPV6_BOUND_IF = 125
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
LOG = logging.getLogger("router-socks")


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
    def __init__(self, router, interface_index, timeout):
        self.router = str(ipaddress.IPv4Address(router))
        self.interface_index = interface_index
        self.timeout = timeout

    def resolve(self, host):
        query_id = int.from_bytes(os.urandom(2), "big")
        query = dns_packet(host, query_id)
        with bound_socket(socket.AF_INET, socket.SOCK_DGRAM,
                          self.interface_index, self.timeout) as sock:
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
                              self.interface_index, self.timeout) as sock:
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
        LOG.info("DNS %s via %s on physical interface -> %s", host, self.router, ", ".join(addresses))
        return addresses


def connect_target(server, host, port, is_domain):
    addresses = server.resolver.resolve(host) if is_domain else [host]
    error = None
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.version == 4 and ip in FAKE_IP_NETWORK:
            error = ValueError("Received Fake-IP %s; the client must forward the original domain" % ip)
            continue
        family = socket.AF_INET if ip.version == 4 else socket.AF_INET6
        sock = bound_socket(family, socket.SOCK_STREAM, server.interface_index, server.timeout)
        try:
            sock.connect((str(ip), port))
            LOG.info("CONNECT %s:%d -> %s:%d via %s, local=%s",
                     host, port, ip, port, server.interface, sock.getsockname()[0])
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
        self.interface = args.interface
        self.interface_index = socket.if_nametoindex(args.interface)
        self.timeout = args.timeout
        self.idle_timeout = args.idle_timeout
        self.resolver = RouterResolver(args.router_dns, self.interface_index, args.timeout)
        super().__init__(("127.0.0.1", args.port), SocksHandler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interface", default="en1", help="Physical network interface (default: en1)")
    parser.add_argument("--router-dns", default="192.168.1.1")
    parser.add_argument("--port", type=int, default=10808)
    parser.add_argument("--timeout", type=float, default=6)
    parser.add_argument("--idle-timeout", type=float, default=300)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if sys.platform != "darwin":
        parser.error("This test uses macOS IP_BOUND_IF; use it on macOS")
    if not 1 <= args.port <= 65535:
        parser.error("Port must be between 1 and 65535")
    try:
        with SocksServer(args) as server:
            LOG.info("Listening at socks5://127.0.0.1:%d; DNS=%s; physical interface=%s (%d)",
                     args.port, args.router_dns, args.interface, server.interface_index)
            server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        LOG.info("Stopped")
    except (OSError, ValueError) as exc:
        LOG.error("Cannot start: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
