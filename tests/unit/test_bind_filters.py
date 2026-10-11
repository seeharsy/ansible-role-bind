"""Unit tests for filter_plugins/bind.py."""

import importlib.util
from pathlib import Path

import pytest
from ansible.errors import AnsibleFilterError

_PLUGIN = Path(__file__).resolve().parents[2] / "filter_plugins" / "bind.py"
_spec = importlib.util.spec_from_file_location("bind_filters", _PLUGIN)
bind = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bind)

DEFAULTS = {
    "ttl": "1h",
    "refresh": "1d",
    "retry": "1h",
    "expire": "1w",
    "minimum": "1h",
    "hostmaster": "hostmaster",
}


# --- reverse zone names -----------------------------------------------------

class TestReverseZoneNames:
    def test_ipv4_slash24(self):
        assert bind.reverse_zone_names("10.20.0.0/24") == ["0.20.10.in-addr.arpa"]

    def test_ipv4_slash16(self):
        assert bind.reverse_zone_names("10.20.0.0/16") == ["20.10.in-addr.arpa"]

    def test_ipv4_slash8(self):
        assert bind.reverse_zone_names("10.0.0.0/8") == ["10.in-addr.arpa"]

    def test_ipv4_slash20_expands_to_sixteen_slash24(self):
        names = bind.reverse_zone_names("10.21.16.0/20")
        assert len(names) == 16
        assert names[0] == "16.21.10.in-addr.arpa"
        assert names[-1] == "31.21.10.in-addr.arpa"

    def test_ipv4_longer_than_slash24_is_rejected(self):
        with pytest.raises(AnsibleFilterError, match="RFC 2317"):
            bind.reverse_zone_names("192.0.2.0/26")

    def test_host_bits_set_is_rejected(self):
        with pytest.raises(AnsibleFilterError, match="host bits"):
            bind.reverse_zone_names("10.20.0.5/24")

    def test_garbage_is_rejected(self):
        with pytest.raises(AnsibleFilterError, match="not a valid network"):
            bind.reverse_zone_names("10.20.0")

    def test_ipv6_nibble_aligned(self):
        assert bind.reverse_zone_names("2001:db8::/32") == ["8.b.d.0.1.0.0.2.ip6.arpa"]

    def test_ipv6_slash38_expands_to_four_slash40_without_leading_dot(self):
        names = bind.reverse_zone_names("fc00::/38")
        assert names == [
            "0.0.0.0.0.0.0.0.c.f.ip6.arpa",
            "1.0.0.0.0.0.0.0.c.f.ip6.arpa",
            "2.0.0.0.0.0.0.0.c.f.ip6.arpa",
            "3.0.0.0.0.0.0.0.c.f.ip6.arpa",
        ]
        assert not any(n.startswith(".") for n in names)


# --- zone role ----------------------------------------------------------------

class TestZoneRole:
    def test_explicit_type_wins(self):
        zone = {"name": "a.test", "type": "secondary", "primary": "ns1"}
        assert bind.zone_role(zone, "ns1") == "secondary"

    def test_primary_host_is_primary(self):
        assert bind.zone_role({"name": "a.test", "primary": "ns1"}, "ns1") == "primary"

    def test_other_host_is_secondary(self):
        assert bind.zone_role({"name": "a.test", "primary": "ns1"}, "ns2") == "secondary"

    def test_forwarders_without_primary_is_forward(self):
        zone = {"name": "a.test", "forwarders": ["192.0.2.1"]}
        assert bind.zone_role(zone, "ns1") == "forward"

    def test_undeterminable_role_is_an_error(self):
        with pytest.raises(AnsibleFilterError, match="a.test"):
            bind.zone_role({"name": "a.test"}, "ns1")


# --- serial -------------------------------------------------------------------

class TestNextSerial:
    def test_new_zone_starts_at_today_00(self):
        assert bind.next_serial(None, "20261011") == 2026101100

    def test_same_day_increments(self):
        assert bind.next_serial(2026101104, "20261011") == 2026101105

    def test_older_day_jumps_to_today(self):
        assert bind.next_serial(2026100907, "20261011") == 2026101100

    def test_v5_unix_timestamp_serial_moves_forward(self):
        assert bind.next_serial(1791680000, "20261011") == 2026101100

    def test_more_than_99_changes_a_day_still_increases(self):
        assert bind.next_serial(2026101199, "20261011") == 2026101200


# --- forward zone content -----------------------------------------------------

def _rr(content, rtype):
    return [r for r in content["records"] if r["type"] == rtype]


class TestZoneContent:
    def zone(self, **extra):
        base = {"name": "example.test", "primary": "ns1", "name_servers": ["ns1", "ns2"]}
        base.update(extra)
        return base

    def test_soa_defaults(self):
        c = bind.zone_content(self.zone(), DEFAULTS)
        assert c["origin"] == "example.test."
        assert c["soa"]["mname"] == "ns1.example.test."
        assert c["soa"]["rname"] == "hostmaster.example.test."
        assert c["ttl"] == "1h"

    def test_per_zone_overrides_defaults(self):
        c = bind.zone_content(self.zone(ttl=300, refresh="2d"), DEFAULTS)
        assert c["ttl"] == 300
        assert c["soa"]["refresh"] == "2d"
        assert c["soa"]["retry"] == "1h"

    def test_hostmaster_email_form(self):
        c = bind.zone_content(self.zone(hostmaster="dns.admin@example.org"), DEFAULTS)
        assert c["soa"]["rname"] == "dns\\.admin.example.org."

    def test_name_servers_become_fqdns(self):
        c = bind.zone_content(self.zone(name_servers=["ns1", "ns.other.test."]), DEFAULTS)
        assert c["ns"] == ["ns1.example.test.", "ns.other.test."]

    def test_host_single_ip(self):
        c = bind.zone_content(self.zone(hosts=[{"name": "www", "ip": "192.0.2.10"}]), DEFAULTS)
        assert {"name": "www", "type": "A", "value": "192.0.2.10"}.items() <= _rr(c, "A")[0].items()

    def test_host_multiple_ips_and_ipv6(self):
        c = bind.zone_content(
            self.zone(hosts=[{"name": "srv", "ip": ["192.0.2.1", "192.0.2.2"], "ipv6": "2001:db8::1"}]),
            DEFAULTS,
        )
        assert [r["value"] for r in _rr(c, "A")] == ["192.0.2.1", "192.0.2.2"]
        assert [r["value"] for r in _rr(c, "AAAA")] == ["2001:db8::1"]

    def test_aliases_become_cnames(self):
        c = bind.zone_content(
            self.zone(hosts=[{"name": "srv", "ip": "192.0.2.1", "aliases": ["www", "git"]}]), DEFAULTS
        )
        assert [(r["name"], r["value"]) for r in _rr(c, "CNAME")] == [("www", "srv"), ("git", "srv")]

    def test_generic_records_pass_through(self):
        recs = [
            {"name": "_ntp._udp", "type": "SRV", "value": "0 100 123 time1"},
            {"name": "sub", "type": "mx", "value": "10 mail", "ttl": 60},
        ]
        c = bind.zone_content(self.zone(records=recs), DEFAULTS)
        assert _rr(c, "SRV")[0]["value"] == "0 100 123 time1"
        assert _rr(c, "MX")[0] == {"name": "sub", "type": "MX", "value": "10 mail", "ttl": 60}

    def test_invalid_ip_is_an_error(self):
        with pytest.raises(AnsibleFilterError, match="srv"):
            bind.zone_content(self.zone(hosts=[{"name": "srv", "ip": "192.0.2.300"}]), DEFAULTS)

    def test_ipv4_in_ipv6_field_is_an_error(self):
        with pytest.raises(AnsibleFilterError, match="srv"):
            bind.zone_content(self.zone(hosts=[{"name": "srv", "ipv6": "192.0.2.1"}]), DEFAULTS)


# --- reverse zones ------------------------------------------------------------

class TestReverseZones:
    def zones(self):
        return [
            {
                "name": "example.test",
                "primary": "ns1",
                "primaries": ["198.51.100.1"],
                "name_servers": ["ns1", "ns2"],
                "allow_transfer": ["198.51.100.2"],
                "reverse_zones": ["192.0.2.0/24", "2001:db8::/64"],
                "hosts": [
                    {"name": "gitlab", "ip": "192.0.2.28"},
                    {"name": "*.pages", "ip": "192.0.2.28"},
                    {"name": "srv", "ip": ["192.0.2.1", "192.0.2.2"], "ipv6": "2001:db8::1"},
                    {"name": "outside", "ip": "203.0.113.9"},
                ],
            },
            {
                "name": "other.test",
                "primary": "ns1",
                "primaries": ["198.51.100.1"],
                "name_servers": ["ns1"],
                "allow_transfer": ["198.51.100.2"],
                "reverse_zones": ["192.0.2.0/24"],
                "hosts": [{"name": "box", "ip": "192.0.2.50"}],
            },
        ]

    def by_name(self, zones):
        return {z["name"]: z for z in bind.reverse_zones(zones)}

    def test_shared_cidr_produces_one_zone(self):
        names = [z["name"] for z in bind.reverse_zones(self.zones())]
        assert names.count("2.0.192.in-addr.arpa") == 1

    def test_ptrs_from_all_forward_zones_are_merged(self):
        rz = self.by_name(self.zones())["2.0.192.in-addr.arpa"]
        ptrs = {r["name"]: r["value"] for r in rz["records"]}
        assert ptrs["50"] == "box.other.test."
        assert ptrs["1"] == "srv.example.test."
        assert ptrs["2"] == "srv.example.test."

    def test_wildcards_get_no_ptr_and_duplicate_ip_keeps_first(self):
        rz = self.by_name(self.zones())["2.0.192.in-addr.arpa"]
        assert [r["value"] for r in rz["records"] if r["name"] == "28"] == ["gitlab.example.test."]

    def test_explicit_ptr_true_wins(self):
        zones = self.zones()
        zones[0]["hosts"][1]["name"] = "pages"
        zones[0]["hosts"][1]["ptr"] = True
        rz = self.by_name(zones)["2.0.192.in-addr.arpa"]
        assert [r["value"] for r in rz["records"] if r["name"] == "28"] == ["pages.example.test."]

    def test_two_explicit_ptrs_for_one_ip_is_an_error(self):
        zones = self.zones()
        zones[0]["hosts"][0]["ptr"] = True
        zones[0]["hosts"][1].update(name="pages", ptr=True)
        with pytest.raises(AnsibleFilterError, match="192.0.2.28"):
            bind.reverse_zones(zones)

    def test_ptr_false_suppresses(self):
        zones = self.zones()
        zones[1]["hosts"][0]["ptr"] = False
        rz = self.by_name(zones)["2.0.192.in-addr.arpa"]
        assert "50" not in {r["name"] for r in rz["records"]}

    def test_ipv6_ptr_owner_is_relative_nibbles(self):
        rz = self.by_name(self.zones())["0.0.0.0.0.0.0.0.8.b.d.0.1.0.0.2.ip6.arpa"]
        assert rz["records"][0]["name"] == "1.0.0.0.0.0.0.0.0.0.0.0.0.0.0.0"
        assert rz["records"][0]["value"] == "srv.example.test."

    def test_reverse_zone_inherits_settings_and_fqdn_name_servers(self):
        rz = self.by_name(self.zones())["2.0.192.in-addr.arpa"]
        assert rz["primary"] == "ns1"
        assert rz["primaries"] == ["198.51.100.1"]
        assert rz["allow_transfer"] == ["198.51.100.2"]
        assert rz["name_servers"] == ["ns1.example.test.", "ns2.example.test."]

    def test_conflicting_primary_for_shared_cidr_is_an_error(self):
        zones = self.zones()
        zones[1]["primary"] = "ns9"
        with pytest.raises(AnsibleFilterError, match="2.0.192.in-addr.arpa"):
            bind.reverse_zones(zones)

    def test_shared_cidr_with_different_allow_transfer_is_an_error(self):
        zones = self.zones()
        zones[1]["allow_transfer"] = ["key other"]
        with pytest.raises(AnsibleFilterError, match="allow_transfer"):
            bind.reverse_zones(zones)

    def test_shared_cidr_with_different_primaries_is_an_error(self):
        zones = self.zones()
        zones[1]["primaries"] = [{"address": "198.51.100.1", "key": "other"}]
        with pytest.raises(AnsibleFilterError, match="primaries"):
            bind.reverse_zones(zones)

    def test_shared_cidr_with_matching_policy_is_fine(self):
        zones = self.zones()
        zones[1]["allow_transfer"] = ["198.51.100.2"]
        zones[1]["primaries"] = ["198.51.100.1"]
        assert "2.0.192.in-addr.arpa" in [z["name"] for z in bind.reverse_zones(zones)]

    def test_hosts_outside_declared_reverse_zones_are_ignored(self):
        names = [z["name"] for z in bind.reverse_zones(self.zones())]
        assert "113.0.203.in-addr.arpa" not in names


# --- validation ---------------------------------------------------------------

class TestValidateZones:
    def test_valid_zones_pass(self):
        zones = [
            {"name": "a.test", "primary": "ns1", "primaries": ["192.0.2.1"], "name_servers": ["ns1"]},
        ]
        assert bind.validate_zones(zones, "ns2") is True

    def test_duplicate_zone_names(self):
        zones = [{"name": "a.test", "primary": "ns1"}, {"name": "a.test", "primary": "ns1"}]
        with pytest.raises(AnsibleFilterError, match="duplicate"):
            bind.validate_zones(zones, "ns1")

    def test_secondary_needs_primaries(self):
        with pytest.raises(AnsibleFilterError, match="primaries"):
            bind.validate_zones([{"name": "a.test", "primary": "ns1"}], "ns2")

    def test_primary_needs_name_servers(self):
        with pytest.raises(AnsibleFilterError, match="name_servers"):
            bind.validate_zones([{"name": "a.test", "primary": "ns1"}], "ns1")

    def test_forward_needs_forwarders(self):
        with pytest.raises(AnsibleFilterError, match="forwarders"):
            bind.validate_zones([{"name": "a.test", "type": "forward"}], "ns1")


# --- server key clauses -------------------------------------------------------

class TestServerKeys:
    def test_keys_from_primaries_on_secondary(self):
        zones = [{"name": "a.test", "primary": "ns1",
                  "primaries": [{"address": "192.0.2.1", "key": "xfer"}, "192.0.2.9"]}]
        assert bind.server_keys(zones, "ns2") == [{"address": "192.0.2.1", "key": "xfer"}]

    def test_keys_from_also_notify_on_primary(self):
        zones = [{"name": "a.test", "primary": "ns1",
                  "also_notify": [{"address": "192.0.2.2", "key": "xfer"}]}]
        assert bind.server_keys(zones, "ns1") == [{"address": "192.0.2.2", "key": "xfer"}]

    def test_same_address_same_key_is_deduplicated(self):
        zones = [
            {"name": "a.test", "primary": "ns1", "primaries": [{"address": "192.0.2.1", "key": "x"}]},
            {"name": "b.test", "primary": "ns1", "primaries": [{"address": "192.0.2.1", "key": "x"}]},
        ]
        assert len(bind.server_keys(zones, "ns2")) == 1

    def test_one_address_two_keys_is_an_error(self):
        zones = [
            {"name": "a.test", "primary": "ns1", "primaries": [{"address": "192.0.2.1", "key": "x"}]},
            {"name": "b.test", "primary": "ns1", "primaries": [{"address": "192.0.2.1", "key": "y"}]},
        ]
        with pytest.raises(AnsibleFilterError, match="one TSIG key per server"):
            bind.server_keys(zones, "ns2")


# --- hashing ------------------------------------------------------------------

class TestContentHash:
    def test_hash_is_stable_across_key_order(self):
        assert bind.content_hash({"a": 1, "b": [1, 2]}) == bind.content_hash({"b": [1, 2], "a": 1})

    def test_hash_changes_with_content(self):
        assert bind.content_hash({"a": 1}) != bind.content_hash({"a": 2})


def test_filters_are_registered():
    names = bind.FilterModule().filters()
    for n in ("bind_reverse_zone_names", "bind_zone_role", "bind_next_serial", "bind_zone_content",
              "bind_reverse_zones", "bind_validate_zones", "bind_server_keys", "bind_content_hash"):
        assert n in names


# --- hash headers -------------------------------------------------------------

class TestParseHashes:
    def test_parses_grep_output(self):
        lines = [
            "/var/named/example.test:; Hash: abc123 2026101101",
            "/var/named/2.0.192.in-addr.arpa:; Hash: def456 1791680000",
        ]
        assert bind.parse_hashes(lines) == {
            "example.test": {"hash": "abc123", "serial": 2026101101},
            "2.0.192.in-addr.arpa": {"hash": "def456", "serial": 1791680000},
        }

    def test_ignores_malformed_lines(self):
        assert bind.parse_hashes(["/var/named/x:; Hash:", "garbage", ""]) == {}


# --- zones per role -----------------------------------------------------------

class TestZonesWithRole:
    zones = [
        {"name": "example.test", "primary": "ns1", "primaries": ["192.0.2.1"],
         "name_servers": ["ns1"], "reverse_zones": ["192.0.2.0/24"],
         "hosts": [{"name": "a", "ip": "192.0.2.5"}]},
        {"name": "fwd.test", "type": "forward", "forwarders": ["192.0.2.53"]},
    ]

    def test_primary_includes_reverse_zones(self):
        names = [z["name"] for z in bind.zones_with_role(self.zones, "ns1", "primary")]
        assert names == ["example.test", "2.0.192.in-addr.arpa"]

    def test_secondary_on_other_host(self):
        names = [z["name"] for z in bind.zones_with_role(self.zones, "ns2", "secondary")]
        assert names == ["example.test", "2.0.192.in-addr.arpa"]

    def test_forward(self):
        names = [z["name"] for z in bind.zones_with_role(self.zones, "ns1", "forward")]
        assert names == ["fwd.test"]


# --- zone name safety ---------------------------------------------------------

class TestZoneNameSafety:
    @pytest.mark.parametrize("name", ["../../etc/cron.d/x", "a/b.test", "bad name.test", "a..test", ""])
    def test_unsafe_names_are_rejected(self, name):
        with pytest.raises(AnsibleFilterError):
            bind.validate_zones([{"name": name, "type": "forward", "forwarders": ["192.0.2.1"]}], "ns1")

    @pytest.mark.parametrize("name", ["example.test", "example.test.", "_msdcs.corp.test", "xn--bcher-kva.test", "com"])
    def test_safe_names_pass(self, name):
        assert bind.validate_zones([{"name": name, "type": "forward", "forwarders": ["192.0.2.1"]}], "ns1")


# --- local zones --------------------------------------------------------------

class TestLocalZones:
    zones = [
        {"name": "example.test", "primary": "ns1", "name_servers": ["ns1"],
         "reverse_zones": ["192.0.2.0/24"]},
    ]

    def test_valid_local_zones_pass(self):
        assert bind.validate_zones(self.zones, "ns1", local_zones=[{"name": "blocked.test"}])

    def test_local_zone_clashing_with_a_zone_is_an_error(self):
        with pytest.raises(AnsibleFilterError, match="example.test.*more than once"):
            bind.validate_zones(self.zones, "ns1", local_zones=[{"name": "example.test."}])

    def test_local_zone_clashing_with_a_reverse_zone_is_an_error(self):
        with pytest.raises(AnsibleFilterError, match="2.0.192.in-addr.arpa.*more than once"):
            bind.validate_zones(self.zones, "ns1", local_zones=[{"name": "2.0.192.in-addr.arpa"}])

    def test_duplicate_local_zones_are_an_error(self):
        with pytest.raises(AnsibleFilterError, match="more than once"):
            bind.validate_zones([], "ns1", local_zones=[{"name": "a.test"}, {"name": "A.test"}])

    def test_unsafe_local_zone_name_is_rejected(self):
        with pytest.raises(AnsibleFilterError, match="zone names may contain only"):
            bind.validate_zones([], "ns1", local_zones=[{"name": "../x"}])

    def test_local_zone_content_points_ns_at_localhost(self):
        c = bind.local_zone_content(
            {"name": "sink.test", "records": [{"name": "*", "type": "a", "value": "192.0.2.1"}]}, DEFAULTS)
        assert c["ns"] == ["localhost."]
        assert c["soa"]["mname"] == "localhost."
        assert c["records"] == [{"name": "*", "type": "A", "value": "192.0.2.1"}]

    def test_local_zone_records_are_validated(self):
        with pytest.raises(AnsibleFilterError, match="sink.test"):
            bind.validate_zones([], "ns1", local_zones=[{"name": "sink.test", "records": [{"name": "x"}]}])


class TestExplicitZoneClashes:
    def test_explicit_zone_clashing_with_a_derived_reverse_zone_is_an_error(self):
        zones = [
            {"name": "example.test", "primary": "ns1", "name_servers": ["ns1"],
             "reverse_zones": ["192.0.2.0/24"]},
            {"name": "2.0.192.in-addr.arpa", "primary": "ns1", "name_servers": ["ns1.example.test."]},
        ]
        with pytest.raises(AnsibleFilterError, match="more than once"):
            bind.validate_zones(zones, "ns1")


# --- per-zone allow_query -----------------------------------------------------

class TestAllowQuery:
    def zones(self):
        return [
            {"name": "a.test", "primary": "ns1", "name_servers": ["ns1"],
             "allow_query": ["clients"], "reverse_zones": ["192.0.2.0/24"]},
            {"name": "b.test", "primary": "ns1", "name_servers": ["ns1"],
             "allow_query": ["clients"], "reverse_zones": ["192.0.2.0/24"]},
        ]

    def test_reverse_zone_inherits_allow_query(self):
        assert bind.reverse_zones(self.zones())[0]["allow_query"] == ["clients"]

    def test_shared_cidr_with_different_allow_query_is_an_error(self):
        zones = self.zones()
        zones[1]["allow_query"] = ["any"]
        with pytest.raises(AnsibleFilterError, match="allow_query"):
            bind.reverse_zones(zones)

    def test_forward_zone_with_allow_query_is_an_error(self):
        zone = {"name": "f.test", "type": "forward", "forwarders": ["192.0.2.1"], "allow_query": ["any"]}
        with pytest.raises(AnsibleFilterError, match="allow_recursion"):
            bind.validate_zones([zone], "ns1")


# --- statistics ---------------------------------------------------------------

class TestZoneStatistics:
    @pytest.mark.parametrize("value,level", [
        (True, "full"), (False, "none"), ("full", "full"), ("terse", "terse"),
        ("none", "none"), ("yes", "full"), ("no", "none"), (None, None),
    ])
    def test_levels(self, value, level):
        assert bind.zone_statistics(value) == level

    def test_invalid_level_is_an_error(self):
        with pytest.raises(AnsibleFilterError, match="full, terse or none"):
            bind.zone_statistics("verbose")

    def test_forward_zone_with_statistics_is_an_error(self):
        zone = {"name": "f.test", "type": "forward", "forwarders": ["192.0.2.1"], "statistics": True}
        with pytest.raises(AnsibleFilterError, match="forward zones do not take statistics"):
            bind.validate_zones([zone], "ns1")

    def test_invalid_zone_level_is_reported_by_validation(self):
        zone = {"name": "a.test", "primary": "ns1", "name_servers": ["ns1"], "statistics": "loud"}
        with pytest.raises(AnsibleFilterError, match="a.test"):
            bind.validate_zones([zone], "ns1")

    def test_reverse_zone_takes_the_most_detailed_level(self):
        zones = [
            {"name": "a.test", "primary": "ns1", "statistics": "terse", "reverse_zones": ["192.0.2.0/24"]},
            {"name": "b.test", "primary": "ns1", "statistics": True, "reverse_zones": ["192.0.2.0/24"]},
            {"name": "c.test", "primary": "ns1", "reverse_zones": ["192.0.2.0/24"]},
        ]
        assert bind.reverse_zones(zones)[0]["statistics"] == "full"

    def test_reverse_zone_without_statistics_has_none_set(self):
        zones = [{"name": "a.test", "primary": "ns1", "reverse_zones": ["192.0.2.0/24"]}]
        assert "statistics" not in bind.reverse_zones(zones)[0]


# --- dynamic zones ------------------------------------------------------------

class TestUpdatePolicy:
    def zone(self, policy):
        return {"name": "dyn.test", "primary": "ns1", "name_servers": ["ns1"], "update_policy": policy}

    def test_rules_render(self):
        policy = [
            {"key": "ddns", "match": "zonesub", "types": ["A", "aaaa", "TXT"]},
            {"key": "acme", "match": "name", "name": "_acme-challenge.dyn.test", "types": ["TXT"]},
            {"action": "deny", "key": "ddns", "match": "subdomain", "name": "infra.dyn.test.", "types": ["ANY"]},
        ]
        assert bind.update_policy(policy) == [
            'grant "ddns" zonesub A AAAA TXT',
            'grant "acme" name _acme-challenge.dyn.test. TXT',
            'deny "ddns" subdomain infra.dyn.test. ANY',
        ]

    def test_dynamic_zone_passes_validation(self):
        policy = [{"key": "ddns", "match": "zonesub", "types": ["A"]}]
        assert bind.validate_zones([self.zone(policy)], "ns1", key_names=["ddns"])

    @pytest.mark.parametrize("rule,message", [
        ({"key": "ddns", "match": "zonesub"}, "types"),
        ({"key": "ddns", "match": "zonesub", "types": []}, "types"),
        ({"key": "ddns", "match": "zonesub", "types": ["A;"]}, "record type"),
        ({"match": "zonesub", "types": ["A"]}, "key"),
        ({"key": "ddns", "match": "krb5-self", "types": ["A"]}, "match"),
        ({"key": "ddns", "match": "name", "types": ["A"]}, "needs a 'name'"),
        ({"key": "ddns", "match": "zonesub", "name": "x.dyn.test", "types": ["A"]}, "takes no 'name'"),
        ({"key": "ddns", "match": "name", "name": "x; }; include", "types": ["A"]}, "not a valid name"),
        ({"action": "allow", "key": "ddns", "match": "zonesub", "types": ["A"]}, "grant or deny"),
    ])
    def test_invalid_rules_are_rejected(self, rule, message):
        with pytest.raises(AnsibleFilterError, match=message):
            bind.validate_zones([self.zone([rule])], "ns1", key_names=["ddns"])

    def test_unknown_key_is_rejected(self):
        policy = [{"key": "nope", "match": "zonesub", "types": ["A"]}]
        with pytest.raises(AnsibleFilterError, match="nope.*bind_tsig_keys"):
            bind.validate_zones([self.zone(policy)], "ns1", key_names=["ddns"])

    def test_reverse_zones_of_a_dynamic_zone_are_static(self):
        zone = self.zone([{"key": "ddns", "match": "zonesub", "types": ["A"]}])
        zone["reverse_zones"] = ["192.0.2.0/24"]
        assert "update_policy" not in bind.reverse_zones([zone])[0]


class TestKeyReferences:
    def test_unknown_key_in_primaries_is_rejected(self):
        zone = {"name": "a.test", "primary": "ns1", "primaries": [{"address": "192.0.2.1", "key": "nope"}]}
        with pytest.raises(AnsibleFilterError, match="nope"):
            bind.validate_zones([zone], "ns2", key_names=["xfer"])

    def test_key_checks_are_skipped_without_key_names(self):
        zone = {"name": "a.test", "primary": "ns1", "primaries": [{"address": "192.0.2.1", "key": "nope"}]}
        assert bind.validate_zones([zone], "ns2")


# --- zone transfers over TLS --------------------------------------------------

def _xot_zone(**extra):
    zone = {
        "name": "a.test", "primary": "ns1", "name_servers": ["ns1"], "xot": True,
        "allow_transfer": ["key xfer"],
        "primaries": [{"address": "192.0.2.1", "key": "xfer", "tls_name": "ns1.a.test"}],
    }
    zone.update(extra)
    return zone


class TestXot:
    def test_valid_xot_zone_passes(self):
        assert bind.validate_zones([_xot_zone()], "ns2")

    def test_xot_primaries_need_tls_name(self):
        zone = _xot_zone(primaries=[{"address": "192.0.2.1", "key": "xfer"}])
        with pytest.raises(AnsibleFilterError, match="tls_name"):
            bind.validate_zones([zone], "ns2")

    def test_xot_plain_string_primary_is_rejected(self):
        with pytest.raises(AnsibleFilterError, match="tls_name"):
            bind.validate_zones([_xot_zone(primaries=["192.0.2.1"])], "ns2")

    def test_xot_primary_needs_allow_transfer(self):
        zone = _xot_zone()
        del zone["allow_transfer"]
        with pytest.raises(AnsibleFilterError, match="allow_transfer"):
            bind.validate_zones([zone], "ns1")

    def test_invalid_tls_name_is_rejected(self):
        zone = _xot_zone(primaries=[{"address": "192.0.2.1", "tls_name": 'ns1"; }; x'}])
        with pytest.raises(AnsibleFilterError, match="not a valid name"):
            bind.validate_zones([zone], "ns2")

    def test_reverse_zone_inherits_xot(self):
        zone = _xot_zone(reverse_zones=["192.0.2.0/24"])
        assert bind.reverse_zones([zone])[0]["xot"] is True

    def test_shared_cidr_with_different_xot_is_an_error(self):
        a = _xot_zone(reverse_zones=["192.0.2.0/24"])
        b = _xot_zone(name="b.test", xot=False, reverse_zones=["192.0.2.0/24"])
        with pytest.raises(AnsibleFilterError, match="xot"):
            bind.reverse_zones([a, b])

    def test_tls_clients_on_secondary(self):
        zones = [_xot_zone(), _xot_zone(name="b.test")]
        assert bind.tls_clients(zones, "ns2") == ["ns1.a.test"]

    def test_tls_clients_on_primary_is_empty(self):
        assert bind.tls_clients([_xot_zone()], "ns1") == []


# --- TLS listeners ------------------------------------------------------------

class TestValidateTls:
    TLS = {"cert": "-----BEGIN CERTIFICATE-----", "key": "-----BEGIN PRIVATE KEY-----"}

    def check(self, zones=(), host="ns1", tls=None, dot=None, doh=None):
        return bind.validate_tls(list(zones), host, tls if tls is not None else self.TLS,
                                 dot or {"enabled": True}, doh or {"enabled": False})

    def test_valid_settings_pass(self):
        assert self.check()

    def test_nothing_enabled_needs_nothing(self):
        assert bind.validate_tls([], "ns1", {}, {"enabled": False}, {"enabled": False})

    def test_listener_needs_a_certificate(self):
        with pytest.raises(AnsibleFilterError, match="certificate"):
            self.check(tls={})

    def test_content_and_file_together_is_an_error(self):
        with pytest.raises(AnsibleFilterError, match="cert or cert_file"):
            self.check(tls={**self.TLS, "cert_file": "/etc/pki/x.pem"})

    def test_relative_file_path_is_an_error(self):
        with pytest.raises(AnsibleFilterError, match="absolute"):
            self.check(tls={"cert_file": "x.pem", "key_file": "/k.pem"})

    @pytest.mark.parametrize("endpoint", ["dns-query", "/dns query", '/q";', "/../x"])
    def test_bad_doh_endpoint_is_rejected(self, endpoint):
        with pytest.raises(AnsibleFilterError, match="endpoint"):
            self.check(doh={"enabled": True, "endpoints": [endpoint]})

    def test_xot_primary_needs_dot(self):
        with pytest.raises(AnsibleFilterError, match="bind_dot"):
            self.check(zones=[_xot_zone()], dot={"enabled": False})

    def test_xot_secondary_needs_a_ca(self):
        with pytest.raises(AnsibleFilterError, match="ca"):
            self.check(zones=[_xot_zone()], host="ns2", tls={}, dot={"enabled": False})

    def test_xot_secondary_with_ca_passes(self):
        assert self.check(zones=[_xot_zone()], host="ns2", tls={"ca": "-----BEGIN CERTIFICATE-----"},
                          dot={"enabled": False})
