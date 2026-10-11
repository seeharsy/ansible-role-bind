# Migrating from v5 to v6

v6 is a rewrite. Most variables were renamed or restructured, and the role
validates every variable before it does anything, so a v5 inventory fails
fast with a list of valid keys instead of producing a broken `named.conf`.
Work through this guide, run the role in check mode, then roll it out to
secondaries before the primary.

Requirements changed too: ansible-core 2.16+, BIND 9.18+, and EL 9/10,
Debian 12/13 or Ubuntu 24.04. EL 9 moves from the `bind` package (9.16) to
`bind9.18`. Arch Linux, FreeBSD, CentOS 7/8, Debian 8-11 and older Ubuntu
releases are no longer supported.

## The main change: define each zone once

In v5 every server had its own copy of each zone, and the role guessed
whether a server was primary or secondary by matching its IP addresses
against `primaries`. In v6 you define each zone once, usually in
`group_vars`, and name its primary by inventory hostname. That host is the
primary; every other host running the role is a secondary.

v5, in `host_vars/ns1.yml` and again in `host_vars/ns2.yml`:

```yaml
bind_zones:
  - name: example.internal
    primaries: [192.0.2.11]
    networks: ["192.0.2"]
    name_servers: [ns1, ns2]
    hosts:
      - {name: ns1, ip: 192.0.2.11}
      - {name: ns2, ip: 192.0.2.12}
      - {name: www, ip: 192.0.2.20, aliases: [web]}
    mail_servers:
      - {name: mail, preference: 10}
```

v6, once, in `group_vars/dns.yml`:

```yaml
bind_zones:
  - name: example.internal
    primary: ns1                      # inventory hostname
    primaries: [{address: 192.0.2.11, key: xfer}]
    allow_transfer: ["key xfer"]
    name_servers: [ns1, ns2]
    reverse_zones: [192.0.2.0/24]
    hosts:
      - {name: ns1, ip: 192.0.2.11}
      - {name: ns2, ip: 192.0.2.12}
      - {name: www, ip: 192.0.2.20, aliases: [web]}
    records:
      - {name: "@", type: MX, value: "10 mail"}
```

`primaries` is what secondaries transfer from; the primary ignores it. Set
`type` on a zone only when you need to override this, for example a zone
this server forwards.

## Server variables

| v5 | v6 |
|---|---|
| `bind_listen_ipv4` | `bind_listen.ipv4` |
| `bind_listen_ipv6` | `bind_listen.ipv6` |
| `bind_listen_ipv4_port`, `bind_listen_ipv6_port` (lists) | `bind_listen.port` (one port for both) |
| `bind_acls[].match_list` | `bind_acls[].match` |
| `bind_allow_query` | `bind_options.allow_query` |
| `bind_recursion` | `bind_options.recursion` |
| `bind_allow_recursion` (default `any`) | `bind_options.allow_recursion` (default `localhost; localnets`) |
| `bind_forwarders` | `bind_options.forwarders` |
| `bind_forward_only: true` | `bind_options.forward: only` |
| `bind_dnssec_validation: true` | `bind_options.dnssec_validation: auto` (the default) |
| `bind_dnssec_enable` | Removed. BIND 9.18 always enables DNSSEC. |
| `bind_query_log` (mapping) | `bind_options.query_log: true`, logged to `data/query.log` |
| `bind_dns_keys` | `bind_tsig_keys` (same `name`, `algorithm`, `secret`) |
| `bind_key_mapping` | `key:` on each `primaries` and `also_notify` entry |
| `bind_statistics_channels`, `_host`, `_port`, `_allow` | `bind_statistics.enabled`, `.host`, `.port`, `.allow` |
| `bind_zone_ttl`, `bind_zone_time_to_refresh`, `_retry`, `_expire`, `bind_zone_minimum_ttl` | `bind_zone_defaults.ttl`, `.refresh`, `.retry`, `.expire`, `.minimum` |
| `bind_extra_include_files` | Unchanged |
| `bind_zone_dir` | Unchanged; also `bind_secondary_dir`, `bind_dynamic_dir` |
| `bind_rrset_order`, `bind_check_names`, `bind_dns64`, `bind_dns64_clients` | Removed. Put the statements in `bind_options_extra` if you need them. |
| `bind_log`, `bind_other_logs` | Removed. The role logs to `data/named.run`. |
| `bind_zone_file_mode`, `bind_python_version` | Removed |

The SOA defaults changed from TTL `1W` and negative TTL `1D` to `1h` for
both. Set `bind_zone_defaults` to keep the old values.

## Zone keys

| v5 | v6 |
|---|---|
| `networks: ["192.0.2"]` | `reverse_zones: [192.0.2.0/24]` |
| `ipv6_networks: ["2001:db8::/48"]` | `reverse_zones: ["2001:db8::/48"]` |
| `create_reverse_zones: false` | Leave out `reverse_zones` |
| `create_forward_zones: false` | Name the reverse zone itself in `bind_zones` |
| `hostmaster_email` | `hostmaster` (also accepts `user@domain`) |
| `mail_servers: [{name, preference}]` | `records: [{name: "@", type: MX, value: "<preference> <name>"}]` |
| `services: [{name, priority, weight, port, target}]` | `records: [{name, type: SRV, value: "<priority> <weight> <port> <target>"}]` |
| `text: [{name, text}]` | `records: [{name, type: TXT, value: '"<text>"'}]`, one per string |
| `caa`, `naptr` | `records` with `type: CAA` or `type: NAPTR` |
| `other_name_servers` | Fully qualified names in `name_servers` |
| `delegate: [{zone, dns}]` | `records` with `type: NS` for the child, plus glue `A` records |
| `hosts[].aliases` with `type: DNAME` | `records` with `type: DNAME` |
| `hosts[].sshfp` | `records` with `type: SSHFP` |
| `allow_update` (by IP) | `update_policy` with a TSIG key. See "Dynamic zones" in the README. |
| `also_notify`, `forwarders`, `name_servers`, `primaries` | Unchanged, but entries may now be `{address, key, port}` |

`hosts` keeps `name`, `ip`, `ipv6`, `aliases` and `ttl`; `ip` and `ipv6`
accept a list. A host can set `ptr: false` to skip its PTR, or `ptr: true`
to win when several hosts share an address.

`records` values are written to the zone file exactly as given, so quote TXT
data yourself.

## Behaviour that changed

- **No transfers by default.** v5 allowed transfers to anyone listed in an
  ACL and, by default, to `any`. v6 refuses transfers unless a zone or
  `bind_options.allow_transfer` allows them. Secondaries need
  `allow_transfer` on the zone, ideally `["key <name>"]`.
- **No automatic records.** v5 added an A record for the server itself to
  zones without hosts. Add it to `hosts` if you relied on it.
- **Shared reverse zones.** When several zones list the same network, v6
  builds one reverse zone with PTRs for all of them. They must then agree
  on `primary`, `type`, `primaries`, `allow_transfer`, `allow_query` and
  `xot`.
- **Private names behind forwarders.** With DNSSEC validation on, BIND
  rejects forwarded answers for names that do not exist in public DNS
  ("broken trust chain"). Set `validate: false` on such forward zones.
- **Zones that only block lookups.** Zones you defined just to stop lookups
  from leaving an offline network can become `bind_local_zones`, which every
  server answers by itself without transfers.

## Upgrading the servers

1. **Serials move forward by themselves.** v5 zone files carry a
   `; Hash: <md5> <serial>` header with a Unix-time serial. v6 reads it,
   sees a different hash, and writes a `YYYYMMDDnn` serial, which is
   higher. Secondaries see an increase and transfer the new zone. Nothing to
   do.
2. **Some files move.**
   - Debian/Ubuntu primaries: zone files move from `/var/cache/bind` to
     `/var/lib/bind`.
   - EL secondaries: transferred zones move from `/var/named/secondary` to
     `/var/named/slaves`, the directory SELinux lets named write to. Each
     zone is transferred in full once.
   - Debian/Ubuntu secondaries keep `/var/cache/bind/secondary`.
3. **v5's files are removed.** From 6.1.0 the role deletes v5's zone files
   in their old location, `<conf dir>/auth_transfer.conf` (v5's TSIG keys;
   v6 uses `tsig-keys.conf`, `0640 root:named`) and, on EL,
   `/var/named/secondary`. See "Stale files" in the README; set
   `bind_remove_stale_files: false` to keep them. On 6.0.0, delete them by
   hand.
4. **Upgrade the primary first, then the secondaries.**
   1. Run the role with `--check --diff` against everything.
   2. Upgrade the primary. Until they are upgraded, v5 secondaries may fail
      to refresh (for example when you add TSIG keys or tighten
      `allow_transfer`), but they keep answering from the copy they hold
      until the zone's SOA expire time.
   3. Upgrade the secondaries, well within that expire time. Use a separate
      play or run for them, after the primary's has finished: a secondary
      that tries the primary while named there is still starting or has no
      DoT listener yet stops transferring from it for minutes (see "Zone
      transfers over TLS" in the README).
   4. Check each secondary with `dig @<secondary> <zone> SOA`: the `aa` flag
      set and the primary's serial.

   Do not upgrade a secondary first. Its v6 transfer settings may not match
   what the v5 primary allows, and on EL it starts from the new, empty
   `/var/named/slaves`, so it would have nothing to serve until the primary
   is upgraded.
5. **Check transfers.** On the primary, `dig @127.0.0.1 <zone> AXFR` should
   now fail unless 127.0.0.1 is allowed, and a transfer signed with the key
   from a secondary should succeed.
