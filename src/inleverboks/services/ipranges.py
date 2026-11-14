"""CIDR lists for the per-exam IP whitelist and the admin's named presets."""

from __future__ import annotations

import ipaddress
import re

from inleverboks.services.errors import DomainError

MAX_RANGES = 50


def parse_cidrs(raw: str | list[str]) -> list[str]:
    """Parse a comma/newline separated list into normalized CIDR strings.

    A bare address becomes a /32 (or /128). Raises DomainError('ip_invalid').
    """
    items = raw if isinstance(raw, list) else [raw]
    parts = [part for item in items for part in re.split(r"[\s,;]+", item)]
    result: list[str] = []
    for part in parts:
        value = part.strip()
        if not value:
            continue
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise DomainError("ip_invalid", value=value) from exc
        text = str(network)
        if text not in result:
            result.append(text)
    if len(result) > MAX_RANGES:
        raise DomainError("ip_too_many")
    return result


def ip_allowed(ip: str | None, cidrs: list[str]) -> bool:
    if not ip:
        return False
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    for cidr in cidrs:
        network = ipaddress.ip_network(cidr, strict=False)
        if address.version == network.version and address in network:
            return True
    return False
