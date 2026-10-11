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

# Forward zones sharing a reverse zone must agree on these, because the
# reverse zone holds PTRs for all of them under a single policy.
_SHARED_POLICY = ("primary", "type", "allow_transfer", "allow_query", "primaries", "xot")

# Settings a reverse zone takes over from the forward zone that declares it.
# update_policy is deliberately absent: reverse zones are always static.
_INHERITED = ("type", "primary", "primaries", "allow_transfer", "allow_query", "also_notify",
              "notify", "ttl", "refresh", "retry", "expire", "minimum", "xot")

_STATISTICS = ("none", "terse", "full")

# update-policy rules: match types that work with TSIG keys, and the ones that
# apply to the whole zone and so take no name.
_UPDATE_MATCH = ("name", "subdomain", "wildcard", "zonesub")
_UPDATE_NAMELESS = ("zonesub",)
_RR_TYPE = re.compile(r"^[A-Za-z][A-Za-z0-9]*$")
# DoH endpoints are URL paths; keep them to plain path characters.
_DOH_ENDPOINT = re.compile(r"^/[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$")
_DNS_NAME = re.compile(r"^(\*\.)?[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*\.?$")

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


# --- statistics -----------------------------------------------------------------

def zone_statistics(value):
    """Map a statistics setting to BIND's zone-statistics level, or None if unset."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "full" if value else "none"
    value = {"yes": "full", "true": "full", "no": "none", "false": "none"}.get(str(value).lower(), value)
    if value not in _STATISTICS:
        raise AnsibleFilterError(f"statistics must be full, terse or none (or true/false), not {value!r}")
    return value


# --- dynamic updates ---------------------------------------------------------------

def update_policy(rules, key_names=None):
    """Render update-policy rules as BIND grant/deny statements."""
    lines = []
    for rule in _as_list(rules):
        action = rule.get("action", "grant")
        if action not in ("grant", "deny"):
            raise AnsibleFilterError(f"update_policy action must be grant or deny, not {action!r}")
        key = rule.get("key")
        if not key:
            raise AnsibleFilterError("every update_policy rule needs the 'key' it applies to")
        if key_names is not None and key not in key_names:
            raise AnsibleFilterError(f"update_policy key {key!r} is not defined in bind_tsig_keys")
        match = rule.get("match")
        if match not in _UPDATE_MATCH:
            raise AnsibleFilterError(
                f"update_policy match must be one of {', '.join(_UPDATE_MATCH)}, not {match!r}")
        parts = [action, f'"{key}"', match]
        name = rule.get("name")
        if match in _UPDATE_NAMELESS:
            if name is not None:
                raise AnsibleFilterError(f"update_policy match {match} takes no 'name'")
        else:
            if not name:
                raise AnsibleFilterError(f"update_policy match {match} needs a 'name'")
            if not _DNS_NAME.match(str(name)):
                raise AnsibleFilterError(f"update_policy name {name!r} is not a valid name")
            parts.append(_origin(name))
        types = _as_list(rule.get("types"))
        if not types:
            raise AnsibleFilterError(
                "every update_policy rule needs 'types', the record types the key may change")
        for rtype in types:
            if not _RR_TYPE.match(str(rtype)):
                raise AnsibleFilterError(f"update_policy: {rtype!r} is not a record type")
        parts.extend(str(t).upper() for t in types)
        lines.append(" ".join(parts))
    return lines


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


def local_zone_content(zone, defaults):
    """A local zone is served by every host with itself as the only name server."""
    return zone_content({**zone, "name_servers": ["localhost."], "hosts": []}, defaults)


def zone_content(zone, defaults):
    origin = _origin(zone["name"])
    ns = [_fqdn(n, origin) for n in _as_list(zone.get("name_servers"))]
    records = []
    for host in _as_list(zone.get("hosts")):
        records.extend(_host_records(zone["name"], host))
    for rec in _as_list(zone.get("records")):
        missing = [k for k in ("name", "type", "value") if k not in rec]
        if missing:
            raise AnsibleFilterError(
                f"zone {zone['name']}: record {rec.get('name', '?')!r} needs {', '.join(missing)}")
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

def _most_detailed(*levels):
    found = [zone_statistics(v) for v in levels if zone_statistics(v) is not None]
    return max(found, key=_STATISTICS.index) if found else None

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
                    for key in _SHARED_POLICY:
                        if first["policy"].get(key) != fwd.get(key):
                            raise AnsibleFilterError(
                                f"reverse zone {rname} is declared by zones {first['source']} and "
                                f"{fwd['name']} with different '{key}' settings; zones sharing a "
                                f"reverse zone must use the same {', '.join(_SHARED_POLICY)}")
                    level = _most_detailed(first["zone"].get("statistics"), fwd.get("statistics"))
                    if level is not None:
                        first["zone"]["statistics"] = level
                    continue
                rz = {k: fwd[k] for k in _INHERITED if k in fwd}
                if zone_statistics(fwd.get("statistics")) is not None:
                    rz["statistics"] = zone_statistics(fwd["statistics"])
                rz["name"] = rname
                rz["name_servers"] = [_fqdn(n, origin) for n in _as_list(fwd.get("name_servers"))]
                rz["hostmaster"] = _rname(fwd.get("hostmaster", defaults["hostmaster"]), origin)
                found[rname] = {"net": net, "zone": rz, "source": fwd["name"],
                                "policy": {k: fwd.get(k) for k in _SHARED_POLICY}}

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

def _check_name(name, where):
    if not name:
        raise AnsibleFilterError(f"every entry in {where} needs a 'name'")
    if not _SAFE_ZONE_NAME.match(str(name)):
        raise AnsibleFilterError(
            f"zone {name!r}: zone names may contain only letters, digits, '-', '_' "
            f"and single dots between labels")


def _check_keys(zone, key_names):
    for peer in _as_list(zone.get("primaries")) + _as_list(zone.get("also_notify")):
        if isinstance(peer, dict) and peer.get("key") and peer["key"] not in key_names:
            raise AnsibleFilterError(
                f"zone {zone['name']}: key {peer['key']!r} is not defined in bind_tsig_keys")


def _check_xot(zone, role):
    """Zones with xot: true transfer over TLS only."""
    if role == "primary" and not _as_list(zone.get("allow_transfer")):
        raise AnsibleFilterError(
            f"zone {zone['name']}: xot accepts transfers over TLS only, so set allow_transfer "
            f"to say who may transfer it")
    if role == "secondary":
        for peer in _as_list(zone.get("primaries")):
            if not isinstance(peer, dict) or not peer.get("tls_name"):
                raise AnsibleFilterError(
                    f"zone {zone['name']}: xot needs a tls_name on every primaries entry, the name "
                    f"in that primary's certificate")
    for peer in _as_list(zone.get("primaries")):
        if isinstance(peer, dict) and peer.get("tls_name") is not None \
                and not _DNS_NAME.match(str(peer["tls_name"])):
            raise AnsibleFilterError(
                f"zone {zone['name']}: tls_name {peer['tls_name']!r} is not a valid name")


def validate_zones(zones, inventory_hostname, defaults=None, local_zones=None, key_names=None):
    defaults = {**_FALLBACK_DEFAULTS, "ttl": 0, "refresh": 0, "retry": 0, "expire": 0,
                "minimum": 0, **(defaults or {})}
    seen = set()

    def claim(name):
        key = _origin(name).lower()
        if key in seen:
            raise AnsibleFilterError(
                f"zone {name}: defined more than once across bind_zones, their reverse zones "
                f"and bind_local_zones (duplicate zone)")
        seen.add(key)

    for where, entries in (("bind_zones", zones), ("bind_local_zones", local_zones)):
        for zone in _as_list(entries):
            _check_name(zone.get("name"), where)
            claim(zone["name"])

    for zone in _as_list(zones):
        try:
            zone_statistics(zone.get("statistics"))
        except AnsibleFilterError as exc:
            raise AnsibleFilterError(f"zone {zone['name']}: {exc}") from exc
        role = zone_role(zone, inventory_hostname)
        if role == "secondary" and not _as_list(zone.get("primaries")):
            raise AnsibleFilterError(
                f"zone {zone['name']}: this host is a secondary for it, so 'primaries' must "
                f"list the addresses to transfer from")
        if zone.get("xot"):
            _check_xot(zone, role)
        if role == "primary":
            if not _as_list(zone.get("name_servers")):
                raise AnsibleFilterError(f"zone {zone['name']}: a primary zone needs 'name_servers'")
            zone_content(zone, defaults)
        if role == "forward":
            if not _as_list(zone.get("forwarders")):
                raise AnsibleFilterError(f"zone {zone['name']}: a forward zone needs 'forwarders'")
            if "allow_query" in zone:
                raise AnsibleFilterError(
                    f"zone {zone['name']}: forward zones do not take allow_query; who may use "
                    f"them is set by bind_options.allow_recursion")
            if "statistics" in zone:
                raise AnsibleFilterError(
                    f"zone {zone['name']}: forward zones do not take statistics; their queries "
                    f"are counted in the server-wide resolver statistics")
        if "update_policy" in zone:
            try:
                update_policy(zone["update_policy"], key_names)
            except AnsibleFilterError as exc:
                raise AnsibleFilterError(f"zone {zone['name']}: {exc}") from exc
        if key_names is not None:
            _check_keys(zone, key_names)

    for zone in _as_list(local_zones):
        try:
            zone_statistics(zone.get("statistics"))
            local_zone_content(zone, defaults)
        except AnsibleFilterError as exc:
            raise AnsibleFilterError(f"local zone {zone['name']}: {exc}") from exc

    for zone in reverse_zones(zones, defaults):
        claim(zone["name"])
    return True


# --- TLS -------------------------------------------------------------------------

def tls_clients(zones, inventory_hostname):
    """Certificate names of the primaries this host transfers from over TLS."""
    names = []
    for zone in zones_with_role(zones, inventory_hostname, "secondary"):
        for peer in _as_list(zone.get("primaries")):
            if isinstance(peer, dict) and peer.get("tls_name") and peer["tls_name"] not in names:
                names.append(peer["tls_name"])
    return names


def _check_pem_source(tls, item):
    if tls.get(item) and tls.get(f"{item}_file"):
        raise AnsibleFilterError(f"bind_tls: set {item} or {item}_file, not both")
    path = tls.get(f"{item}_file")
    if path and not str(path).startswith("/"):
        raise AnsibleFilterError(f"bind_tls.{item}_file must be an absolute path, not {path!r}")
    return bool(tls.get(item) or path)


def validate_tls(zones, inventory_hostname, tls, dot, doh):
    """Check the TLS material this host needs for its listeners and transfers."""
    tls = tls or {}
    has_cert = _check_pem_source(tls, "cert")
    has_key = _check_pem_source(tls, "key")
    has_ca = _check_pem_source(tls, "ca")
    if (dot.get("enabled") or doh.get("enabled")) and not (has_cert and has_key):
        raise AnsibleFilterError(
            "bind_dot and bind_doh need a certificate and key: set bind_tls.cert and "
            "bind_tls.key (PEM content), or bind_tls.cert_file and bind_tls.key_file")
    if doh.get("enabled"):
        for endpoint in _as_list(doh.get("endpoints")):
            if not _DOH_ENDPOINT.match(str(endpoint)):
                raise AnsibleFilterError(
                    f"bind_doh endpoint {endpoint!r} must be a path such as /dns-query")
    if not dot.get("enabled"):
        for zone in zones_with_role(zones, inventory_hostname, "primary"):
            if zone.get("xot"):
                raise AnsibleFilterError(
                    f"zone {zone['name']}: xot transfers use the DoT listener, so enable bind_dot "
                    f"on {inventory_hostname}")
    if tls_clients(zones, inventory_hostname) and not has_ca:
        raise AnsibleFilterError(
            "this host transfers zones over TLS, so set bind_tls.ca or bind_tls.ca_file to the "
            "CA that signed its primaries' certificates")
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


def _hash_headers(lines, dirs):
    """Yield (path, hash, serial) from `grep -sHZ '^; Hash:'` output.

    grep -Z ends each file name with a NUL byte, which no file name can
    contain, so a crafted name (with newlines or colons) cannot forge a
    record. Records are accepted only for files directly inside one of
    dirs whose name is a valid zone file name.
    """
    dirs = {str(d).rstrip("/") for d in _as_list(dirs)}
    for line in _as_list(lines):
        path, sep, header = str(line).partition("\0")
        fields = header.split()
        if not sep or len(fields) != 4 or fields[:2] != [";", "Hash:"] or not fields[3].isdigit():
            continue
        directory, _, name = path.rpartition("/")
        if directory.rstrip("/") not in dirs or not _SAFE_ZONE_NAME.match(name):
            continue
        yield path, fields[2], int(fields[3])


def parse_hashes(lines, directory):
    """Map zone file name to {hash, serial} for files in directory."""
    return {os.path.basename(path): {"hash": h, "serial": serial}
            for path, h, serial in _hash_headers(lines, [directory])}


def stale_zone_files(lines, keep, dirs):
    """Zone files written by this role (v5 or v6) in dirs that it no longer manages."""
    keep = set(_as_list(keep))
    return sorted({path for path, _, _ in _hash_headers(lines, dirs) if path not in keep})


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
            "bind_stale_zone_files": stale_zone_files,
            "bind_zones_with_role": zones_with_role,
            "bind_zone_statistics": zone_statistics,
            "bind_update_policy": update_policy,
            "bind_local_zone_content": local_zone_content,
            "bind_tls_clients": tls_clients,
            "bind_validate_tls": validate_tls,
        }
