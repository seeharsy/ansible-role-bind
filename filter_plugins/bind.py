"""Filters for the bind role.

All address arithmetic uses the standard library ``ipaddress`` module, so the
role needs neither ``netaddr`` nor the ``ansible.utils.ipaddr`` filter.
"""

import hashlib
import ipaddress
import json
import math
import os
import re

from ansible.errors import AnsibleFilterError

ROLES = ("primary", "secondary", "forward")

# Zone names become file names, so allow DNS label characters only.
_SAFE_ZONE_NAME = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*\.?$")

# Settings a reverse zone takes over from the forward zone that declares it.
_INHERITED = ("type", "primary", "primaries", "allow_transfer", "also_notify", "notify",
              "ttl", "refresh", "retry", "expire", "minimum")

_FALLBACK_DEFAULTS = {"hostmaster": "hostmaster"}


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _origin(name):
    return str(name).rstrip(".") + "."


def _fqdn(name, origin):
    name = str(name)
    if name == "@":
        return origin
    if name.endswith("."):
        return name
    return f"{name}.{origin}"


def _rname(hostmaster, origin):
    """Turn a hostmaster setting into the SOA RNAME field."""
    hostmaster = str(hostmaster)
    if "@" in hostmaster:
        local, domain = hostmaster.split("@", 1)
        return f"{local.replace('.', chr(92) + '.')}.{_origin(domain)}"
    if hostmaster.endswith("."):
        return hostmaster
    if "." in hostmaster:
        return hostmaster + "."
    return f"{hostmaster}.{origin}"


def _address(entry):
    """Primaries and also_notify entries are plain addresses or dicts."""
    return entry["address"] if isinstance(entry, dict) else entry


# --- reverse zone names --------------------------------------------------------

def _parse_network(cidr):
    try:
        return ipaddress.ip_network(str(cidr), strict=True)
    except ValueError as exc:
        if "host bits set" in str(exc):
            fixed = ipaddress.ip_network(str(cidr), strict=False)
            raise AnsibleFilterError(
                f"reverse zone {cidr!r} has host bits set; did you mean {fixed}?") from exc
        raise AnsibleFilterError(f"reverse zone {cidr!r} is not a valid network in CIDR notation") from exc


def _reverse_networks(cidr):
    """Split a network into networks that each map onto exactly one reverse zone."""
    net = _parse_network(cidr)
    if net.version == 4:
        if net.prefixlen > 24:
            raise AnsibleFilterError(
                f"reverse zone {cidr!r}: prefixes longer than /24 need RFC 2317 classless "
                f"delegation, which this role does not generate; declare the enclosing /24")
        step = 8
    else:
        step = 4
    target = max(step, math.ceil(net.prefixlen / step) * step)
    return list(net.subnets(new_prefix=target))


def _zone_name(net):
    if net.version == 4:
        octets = str(net.network_address).split(".")[: net.prefixlen // 8]
        return ".".join(reversed(octets)) + ".in-addr.arpa"
    nibbles = net.network_address.exploded.replace(":", "")[: net.prefixlen // 4]
    return ".".join(reversed(nibbles)) + ".ip6.arpa"


def reverse_zone_names(cidr):
    return [_zone_name(n) for n in _reverse_networks(cidr)]


# --- zone role -------------------------------------------------------------------

def zone_role(zone, inventory_hostname):
    name = zone.get("name")
    ztype = zone.get("type")
    if ztype is not None:
        if ztype not in ROLES:
            raise AnsibleFilterError(f"zone {name}: type must be one of {', '.join(ROLES)}, not {ztype!r}")
        return ztype
    if zone.get("primary") is not None:
        return "primary" if zone["primary"] == inventory_hostname else "secondary"
    if zone.get("forwarders"):
        return "forward"
    raise AnsibleFilterError(
        f"zone {name}: cannot tell this host's role; set 'primary' to the inventory "
        f"hostname of its primary server, or set 'type'")


# --- serial ------------------------------------------------------------------------

def next_serial(current, today):
    base = int(today) * 100
    if current in (None, ""):
        return base
    current = int(current)
    return current + 1 if current >= base else base


# --- zone content --------------------------------------------------------------

def _host_records(zone_name, host):
    name = host.get("name")
    extra = {"ttl": host["ttl"]} if "ttl" in host else {}
    records = []
    for field, rtype, version in (("ip", "A", 4), ("ipv6", "AAAA", 6)):
        for ip in _as_list(host.get(field)):
            try:
                addr = ipaddress.ip_address(str(ip))
            except ValueError as exc:
                raise AnsibleFilterError(
                    f"zone {zone_name}, host {name!r}: {ip!r} is not a valid IP address") from exc
            if addr.version != version:
                raise AnsibleFilterError(
                    f"zone {zone_name}, host {name!r}: {ip!r} belongs in "
                    f"'{'ipv6' if addr.version == 6 else 'ip'}', not '{field}'")
            records.append({"name": name, "type": rtype, "value": str(addr), **extra})
    for alias in _as_list(host.get("aliases")):
        records.append({"name": alias, "type": "CNAME", "value": name, **extra})
    return records


def zone_content(zone, defaults):
    origin = _origin(zone["name"])
    ns = [_fqdn(n, origin) for n in _as_list(zone.get("name_servers"))]
    records = []
    for host in _as_list(zone.get("hosts")):
        records.extend(_host_records(zone["name"], host))
    for rec in _as_list(zone.get("records")):
        out = {"name": rec["name"], "type": str(rec["type"]).upper(), "value": rec["value"]}
        if "ttl" in rec:
            out["ttl"] = rec["ttl"]
        records.append(out)
    return {
        "origin": origin,
        "ttl": zone.get("ttl", defaults["ttl"]),
        "soa": {
            "mname": ns[0] if ns else origin,
            "rname": _rname(zone.get("hostmaster", defaults["hostmaster"]), origin),
            "refresh": zone.get("refresh", defaults["refresh"]),
            "retry": zone.get("retry", defaults["retry"]),
            "expire": zone.get("expire", defaults["expire"]),
            "minimum": zone.get("minimum", defaults["minimum"]),
        },
        "ns": ns,
        "records": records,
    }


# --- reverse zones ---------------------------------------------------------------

def reverse_zones(zones, defaults=None):
    defaults = defaults or _FALLBACK_DEFAULTS
    found = {}   # zone name -> {"net": network, "zone": dict, "source": forward zone name}
    for fwd in _as_list(zones):
        origin = _origin(fwd["name"])
        for cidr in _as_list(fwd.get("reverse_zones")):
            for net in _reverse_networks(cidr):
                rname = _zone_name(net)
                if rname in found:
                    first = found[rname]
                    for key in ("primary", "type"):
                        if first["zone"].get(key) != fwd.get(key):
                            raise AnsibleFilterError(
                                f"reverse zone {rname} is declared by zones {first['source']} and "
                                f"{fwd['name']} with different '{key}' settings")
                    continue
                rz = {k: fwd[k] for k in _INHERITED if k in fwd}
                rz["name"] = rname
                rz["name_servers"] = [_fqdn(n, origin) for n in _as_list(fwd.get("name_servers"))]
                rz["hostmaster"] = _rname(fwd.get("hostmaster", defaults["hostmaster"]), origin)
                found[rname] = {"net": net, "zone": rz, "source": fwd["name"]}

    candidates = {}  # ip -> list of (explicit, target)
    for fwd in _as_list(zones):
        origin = _origin(fwd["name"])
        for host in _as_list(fwd.get("hosts")):
            ptr = host.get("ptr")
            if ptr is False or "*" in str(host.get("name")):
                continue
            for ip in _as_list(host.get("ip")) + _as_list(host.get("ipv6")):
                try:
                    addr = ipaddress.ip_address(str(ip))
                except ValueError:
                    continue  # zone_content reports invalid addresses
                candidates.setdefault(addr, []).append((ptr is True, _fqdn(host["name"], origin)))

    for entry in found.values():
        entry["zone"]["records"] = []
    for addr in sorted(candidates, key=lambda a: (a.version, int(a))):
        home = next((e for e in found.values()
                     if e["net"].version == addr.version and addr in e["net"]), None)
        if home is None:
            continue
        options = candidates[addr]
        explicit = [t for is_explicit, t in options if is_explicit]
        if len(explicit) > 1:
            raise AnsibleFilterError(
                f"{addr} has 'ptr: true' on more than one host ({', '.join(explicit)}); keep it on one")
        target = explicit[0] if explicit else options[0][1]
        owner = addr.reverse_pointer[: -(len(home["zone"]["name"]) + 1)]
        home["zone"]["records"].append({"name": owner, "type": "PTR", "value": target})

    return [e["zone"] for e in found.values()]


# --- validation --------------------------------------------------------------------

def validate_zones(zones, inventory_hostname, defaults=None):
    defaults = {**_FALLBACK_DEFAULTS, "ttl": 0, "refresh": 0, "retry": 0, "expire": 0,
                "minimum": 0, **(defaults or {})}
    seen = set()
    for zone in _as_list(zones):
        if not zone.get("name"):
            raise AnsibleFilterError("every entry in bind_zones needs a 'name'")
        if not _SAFE_ZONE_NAME.match(str(zone["name"])):
            raise AnsibleFilterError(
                f"zone {zone['name']!r}: zone names may contain only letters, digits, '-', '_' "
                f"and single dots between labels")
        key = _origin(zone["name"]).lower()
        if key in seen:
            raise AnsibleFilterError(f"zone {zone['name']}: duplicate zone name in bind_zones")
        seen.add(key)
    for zone in _as_list(zones):
        role = zone_role(zone, inventory_hostname)
        if role == "secondary" and not _as_list(zone.get("primaries")):
            raise AnsibleFilterError(
                f"zone {zone['name']}: this host is a secondary for it, so 'primaries' must "
                f"list the addresses to transfer from")
        if role == "primary":
            if not _as_list(zone.get("name_servers")):
                raise AnsibleFilterError(f"zone {zone['name']}: a primary zone needs 'name_servers'")
            zone_content(zone, defaults)
        if role == "forward" and not _as_list(zone.get("forwarders")):
            raise AnsibleFilterError(f"zone {zone['name']}: a forward zone needs 'forwarders'")
    reverse_zones(zones, defaults)
    return True


# --- server key clauses ------------------------------------------------------------

def server_keys(zones, inventory_hostname):
    result = {}
    for zone in _as_list(zones):
        role = zone_role(zone, inventory_hostname)
        peers = _as_list(zone.get("primaries")) if role == "secondary" else []
        if role == "primary":
            peers = _as_list(zone.get("also_notify"))
        for peer in peers:
            if not isinstance(peer, dict) or not peer.get("key"):
                continue
            addr, key = peer["address"], peer["key"]
            if result.get(addr, key) != key:
                raise AnsibleFilterError(
                    f"BIND allows one TSIG key per server; {addr} is given both "
                    f"{result[addr]!r} and {key!r}")
            result[addr] = key
    return [{"address": a, "key": k} for a, k in result.items()]


# --- hashing -----------------------------------------------------------------------

def content_hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def parse_hashes(lines):
    """Parse `grep -H '^; Hash:'` output into {zone file name: {hash, serial}}."""
    result = {}
    for line in _as_list(lines):
        path, sep, header = str(line).partition(":")
        fields = header.split()
        if not sep or len(fields) != 4 or fields[:2] != [";", "Hash:"] or not fields[3].isdigit():
            continue
        result[os.path.basename(path)] = {"hash": fields[2], "serial": int(fields[3])}
    return result


def zones_with_role(zones, inventory_hostname, role):
    """Forward and derived reverse zones for which this host has the given role."""
    candidates = _as_list(zones) + reverse_zones(zones)
    return [z for z in candidates if zone_role(z, inventory_hostname) == role]


def addresses(entries):
    """Plain addresses from a primaries / also_notify list."""
    return [_address(e) for e in _as_list(entries)]


class FilterModule:
    def filters(self):
        return {
            "bind_reverse_zone_names": reverse_zone_names,
            "bind_zone_role": zone_role,
            "bind_next_serial": next_serial,
            "bind_zone_content": zone_content,
            "bind_reverse_zones": reverse_zones,
            "bind_validate_zones": validate_zones,
            "bind_server_keys": server_keys,
            "bind_content_hash": content_hash,
            "bind_addresses": addresses,
            "bind_parse_hashes": parse_hashes,
            "bind_zones_with_role": zones_with_role,
        }
