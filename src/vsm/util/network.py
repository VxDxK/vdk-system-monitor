"""Typed network probe and traffic configuration."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address, ip_address


class TrafficRole(StrEnum):
    PRODUCER = "producer"
    CONSUMER = "consumer"


class TrafficProtocol(StrEnum):
    TCP = "tcp"
    UDP = "udp"


class ProbeRole(StrEnum):
    CLIENT = "client"
    SERVER = "server"


class ProbeProtocol(StrEnum):
    TCP = "tcp"
    UDP = "udp"
    ICMP = "icmp"


@dataclass(frozen=True)
class Endpoint:
    address: IPv4Address | IPv6Address
    port: int

    def __post_init__(self) -> None:
        if not 0 < self.port < 65536:
            raise ValueError("port must be in 1..65535")

    def __str__(self) -> str:
        host = f"[{self.address}]" if isinstance(self.address, IPv6Address) else str(self.address)
        return f"{host}:{self.port}"

    @classmethod
    def parse(cls, value: str) -> Endpoint:
        host, port = value.rsplit(":", 1)
        return cls(ip_address(host.strip("[]")), int(port))


@dataclass(frozen=True)
class BindEndpoint:
    """Local bind address; port zero asks the OS to choose an ephemeral port."""

    address: IPv4Address | IPv6Address
    port: int = 0

    def __post_init__(self) -> None:
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError("bind port must be in 0..65535")

    def __str__(self) -> str:
        host = f"[{self.address}]" if isinstance(self.address, IPv6Address) else str(self.address)
        return f"{host}:{self.port}"

    @classmethod
    def parse(cls, value: str) -> BindEndpoint:
        host, port = value.rsplit(":", 1)
        return cls(ip_address(host.strip("[]")), int(port))
