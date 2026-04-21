#!/usr/bin/env python3
"""
Zeroconf-based fleet discovery for the gRPC approach (Full Mesh gRPC).

Uses mDNS / DNS-SD to advertise and discover robot gRPC services on
the local network — no hard-coded port lists required.

Each gRPC service is registered as its own DNS-SD service type:

  ``_robot-cmd._tcp.local.``     — RobotCommand   (unary RPCs)
  ``_robot-stream._tcp.local.``  — RobotStreaming  (server-streaming RPCs)
  ``_chg-station._tcp.local.``   — ChargingStation (slot negotiation)

A robot registers **both** ``_robot-cmd`` and ``_robot-stream`` (same
host:port — gRPC multiplexes on one HTTP/2 connection).  Consumers
browse only the service types they need:

  • robot_node  →  browses ``_robot-stream`` (subscribes to peer state)
  • robot_ui    →  browses ``_robot-cmd`` + ``_robot-stream``
                   (sends commands AND reads state/video)

When both types resolve to the same address, the callback fires only
once (address-level deduplication).

Usage in robot_node.py
----------------------
    disc = FleetDiscovery(
        robot_id="robot1", grpc_port=50051,
        register_types=[CMD_SERVICE_TYPE, STREAM_SERVICE_TYPE],
        browse_types=[STREAM_SERVICE_TYPE],
        on_peer_added=robot.register_peer,
    )
    disc.start()

Usage in robot_ui.py
--------------------
    disc = FleetDiscovery(
        robot_id="ui",
        browse_types=[CMD_SERVICE_TYPE, STREAM_SERVICE_TYPE],
        on_peer_added=_on_peer_discovered,
    )
    disc.start()

**Why this exists (webinar talking point):**
gRPC has no built-in discovery.  We had to bolt on an entirely separate
protocol stack (mDNS multicast, RFC 6762/6763) and ~150 lines of glue
code just to get per-service-type registration and browsing.
DDS does all of this natively — zero extra code.
"""

import socket
import threading
import time
from datetime import datetime
from typing import Callable, Optional

import ifaddr
from zeroconf import (
    IPVersion,
    ServiceBrowser,
    ServiceInfo,
    ServiceStateChange,
    Zeroconf,
)

# DNS-SD service types — one per gRPC service.
# Robots register CMD + STREAM (same host:port); consumers browse what they need.
CMD_SERVICE_TYPE = "_robot-cmd._tcp.local."
STREAM_SERVICE_TYPE = "_robot-stream._tcp.local."
STATION_SERVICE_TYPE = "_chg-station._tcp.local."


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


class FleetDiscovery:
    """Advertise this node's gRPC endpoints and discover peers via mDNS.

    Each gRPC service is its own DNS-SD service type.  A robot registers
    both ``_robot-cmd`` and ``_robot-stream``; consumers browse only what
    they need.  When both types resolve to the same ``host:port``, the
    callback fires only once (address-level deduplication).

    Parameters
    ----------
    robot_id : str
        Unique name for this participant (e.g. "robot1", "ui").
    grpc_port : int | None
        If set, register mDNS services so others can find us.
        The UI node typically does *not* register (it only browses).
    register_types : list[str] | None
        DNS-SD service types to register.  Defaults to both
        ``CMD_SERVICE_TYPE`` and ``STREAM_SERVICE_TYPE``.
        Ignored when ``grpc_port`` is None.
    browse_types : list[str] | None
        DNS-SD service types to browse for robot peers.
        Defaults to both ``CMD_SERVICE_TYPE`` and ``STREAM_SERVICE_TYPE``.
    on_peer_added : callable(address: str)
        Called with ``"host:port"`` when a new robot peer is discovered.
        Deduplicated by address — fires at most once per host:port even
        if multiple service types resolve to the same endpoint.
    on_peer_removed : callable(address: str) | None
        Called with ``"host:port"`` when a robot peer disappears.
    on_station_added : callable(address: str) | None
        Called with ``"host:port"`` when a charging station is discovered.
    """

    def __init__(
        self,
        robot_id: str,
        grpc_port: Optional[int] = None,
        register_types: Optional[list] = None,
        browse_types: Optional[list] = None,
        on_peer_added: Optional[Callable[[str], None]] = None,
        on_peer_removed: Optional[Callable[[str], None]] = None,
        on_station_added: Optional[Callable[[str], None]] = None,
        advertise_ip: Optional[str] = None,
    ):
        self.robot_id = robot_id
        self.grpc_port = grpc_port
        self._register_types = register_types or [CMD_SERVICE_TYPE, STREAM_SERVICE_TYPE]
        self._browse_types = browse_types or [CMD_SERVICE_TYPE, STREAM_SERVICE_TYPE]
        self._on_peer_added = on_peer_added
        self._on_peer_removed = on_peer_removed
        self._on_station_added = on_station_added
        self._advertise_ip = advertise_ip

        self._zc: Optional[Zeroconf] = None
        self._browsers: list = []
        self._station_browser: Optional[ServiceBrowser] = None
        self._infos: list = []   # one ServiceInfo per registered type

        # Track known peers so we don't fire duplicate callbacks.
        self._known: dict[str, str] = {}       # service_name → "host:port"
        self._known_addrs: set[str] = set()    # deduplicate by address
        self._lock = threading.Lock()

    # ── public API ────────────────────────────────────────────────────────

    def start(self):
        """Start the Zeroconf instance, register (if port given) and browse."""
        self._zc = Zeroconf(ip_version=IPVersion.V4Only)

        # Register one mDNS service per gRPC service type (robots only)
        if self.grpc_port is not None:
            hostname = socket.gethostname()
            local_ip = self._advertise_ip or self._get_lan_ip()
            for stype in self._register_types:
                info = ServiceInfo(
                    type_=stype,
                    name=f"{self.robot_id}.{stype}",
                    addresses=[socket.inet_aton(local_ip)],
                    port=self.grpc_port,
                    properties={
                        b"robot_id": self.robot_id.encode(),
                        b"hostname": hostname.encode(),
                    },
                    server=f"{hostname}.local.",
                )
                self._zc.register_service(info)
                self._infos.append(info)
                print(f"[{self.robot_id} {_ts()}] mDNS: registered "
                      f"{stype} → {local_ip}:{self.grpc_port}")

        # Browse for each robot service type
        for stype in self._browse_types:
            browser = ServiceBrowser(
                self._zc, stype, handlers=[self._on_state_change],
            )
            self._browsers.append(browser)
            print(f"[{self.robot_id} {_ts()}] mDNS: browsing for {stype}")

        # Also browse for charging stations
        if self._on_station_added:
            self._station_browser = ServiceBrowser(
                self._zc, STATION_SERVICE_TYPE,
                handlers=[self._on_station_state_change],
            )
            print(f"[{self.robot_id} {_ts()}] mDNS: browsing for {STATION_SERVICE_TYPE}")

    def close(self):
        """Unregister and shut down."""
        if self._zc:
            for info in self._infos:
                self._zc.unregister_service(info)
        for browser in self._browsers:
            browser.cancel()
        if self._station_browser:
            self._station_browser.cancel()
        if self._zc:
            self._zc.close()

    # ── internal ──────────────────────────────────────────────────────────

    def _on_state_change(
        self,
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        """Called by ``ServiceBrowser`` on a background thread."""
        if state_change == ServiceStateChange.Added:
            # Request full service info (async-safe)
            threading.Thread(
                target=self._resolve_and_add,
                args=(zeroconf, service_type, name),
                daemon=True,
            ).start()

        elif state_change == ServiceStateChange.Removed:
            with self._lock:
                address = self._known.pop(name, None)
            if address and self._on_peer_removed:
                self._on_peer_removed(address)
                print(f"[{self.robot_id} {_ts()}] mDNS: peer removed → {address}")

    def _resolve_and_add(self, zeroconf, service_type, name):
        """Resolve a newly discovered service and notify the callback.

        Multiple service types (e.g. _robot-cmd and _robot-stream) may
        resolve to the same host:port.  We deduplicate by address so the
        ``on_peer_added`` callback fires at most once per endpoint.
        """
        info = zeroconf.get_service_info(service_type, name, timeout=4000)
        if info is None:
            return

        # Extract the robot_id from the TXT record
        peer_id = (info.properties.get(b"robot_id") or b"").decode()

        # Skip ourselves
        if peer_id == self.robot_id:
            return

        # Build the address from the resolved record
        addresses = info.parsed_scoped_addresses(version=IPVersion.V4Only)
        if not addresses:
            return
        host = addresses[0]
        port = info.port
        address = f"{host}:{port}"

        with self._lock:
            self._known[name] = address
            # Deduplicate by address — only fire callback once per host:port
            if address in self._known_addrs:
                return
            self._known_addrs.add(address)

        print(f"[{self.robot_id} {_ts()}] mDNS: discovered {peer_id} → {address} "
              f"(via {service_type.split('.')[0]})")
        if self._on_peer_added:
            self._on_peer_added(address)

    # ── charging station discovery ────────────────────────────────────────

    def _on_station_state_change(self, zeroconf, service_type, name, state_change):
        """Called by the station ServiceBrowser on a background thread."""
        if state_change == ServiceStateChange.Added:
            threading.Thread(
                target=self._resolve_and_add_station,
                args=(zeroconf, service_type, name),
                daemon=True,
            ).start()

    def _resolve_and_add_station(self, zeroconf, service_type, name):
        """Resolve a discovered charging station and notify the callback."""
        info = zeroconf.get_service_info(service_type, name, timeout=4000)
        if info is None:
            return

        station_id = (info.properties.get(b"station_id") or b"").decode()
        addresses = info.parsed_scoped_addresses(version=IPVersion.V4Only)
        if not addresses:
            return
        host = addresses[0]
        port = info.port
        address = f"{host}:{port}"

        with self._lock:
            if name in self._known:
                return
            self._known[name] = address

        print(f"[{self.robot_id} {_ts()}] mDNS: discovered station {station_id} → {address}")
        if self._on_station_added:
            self._on_station_added(address)

    @staticmethod
    def register_station(station_id: str, grpc_port: int,
                         advertise_ip: Optional[str] = None) -> tuple:
        """Register a charging station on mDNS and return (Zeroconf, ServiceInfo).

        The caller should keep these references alive and call
        ``zc.unregister_service(info); zc.close()`` on shutdown.
        """
        zc = Zeroconf(ip_version=IPVersion.V4Only)
        hostname = socket.gethostname()
        local_ip = advertise_ip or FleetDiscovery._get_lan_ip()
        info = ServiceInfo(
            type_=STATION_SERVICE_TYPE,
            name=f"{station_id}.{STATION_SERVICE_TYPE}",
            addresses=[socket.inet_aton(local_ip)],
            port=grpc_port,
            properties={
                b"station_id": station_id.encode(),
                b"hostname": hostname.encode(),
            },
            server=f"{hostname}.local.",
        )
        zc.register_service(info)
        print(f"[{station_id} {_ts()}] mDNS: registered station → {local_ip}:{grpc_port}")
        return zc, info

    @staticmethod
    def _get_lan_ip() -> str:
        """Best-effort local LAN IPv4 address.

        Iterates all network interfaces and picks the first private
        RFC-1918 address (192.168.x.x, 10.x.x.x, 172.16–31.x.x),
        skipping loopback, link-local (169.254.x.x), and common VPN
        ranges (100.64–127.x.x used by Tailscale/CGNAT).

        Falls back to the UDP-connect trick if nothing better is found.
        """
        # Candidate scoring: lower = better
        def _score(ip: str) -> int:
            parts = ip.split(".")
            a, b = int(parts[0]), int(parts[1])
            if a == 127:
                return 99                          # loopback
            if a == 169 and b == 254:
                return 90                          # link-local
            if a == 100 and 64 <= b <= 127:
                return 80                          # CGNAT / Tailscale
            if a == 192 and b == 168:
                return 10                          # typical home/office LAN
            if a == 10:
                return 11                          # corporate / class-A private
            if a == 172 and 16 <= b <= 31:
                return 12                          # class-B private
            return 50                              # public or unknown

        best_ip = "127.0.0.1"
        best_score = 100
        try:
            for adapter in ifaddr.get_adapters():
                for ip_info in adapter.ips:
                    if not isinstance(ip_info.ip, str):
                        continue                   # skip IPv6 tuples
                    s = _score(ip_info.ip)
                    if s < best_score:
                        best_score = s
                        best_ip = ip_info.ip
        except Exception:
            pass

        if best_score >= 50:
            # Fallback: UDP connect trick
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(("8.8.8.8", 80))
                best_ip = s.getsockname()[0]
                s.close()
            except Exception:
                pass

        return best_ip
