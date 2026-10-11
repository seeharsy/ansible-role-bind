# Ansible role BIND

[![CI](https://github.com/seeharsy/ansible-role-bind/actions/workflows/ci.yml/badge.svg)](https://github.com/seeharsy/ansible-role-bind/actions/workflows/ci.yml)

> **Maintained continuation.** This repository continues
> [bertvv/ansible-role-bind](https://github.com/bertvv/ansible-role-bind), which
> has had no merges since 2021. Many thanks to Bert Van Vreckem and every
> upstream contributor; the full upstream history is preserved here.
> Issues and pull requests belong here, not upstream.
>
> **v6 is a breaking release.** Variable names and structure changed; upgrading
> from v5 is covered in [MIGRATION.md](MIGRATION.md).

Sets up ISC BIND 9.18 or later as an authoritative server, a recursive
resolver, or both:

- installs BIND and validates the installed version
- writes `named.conf` and checks it with `named-checkconf` before it goes live
- generates forward zones from `hosts` and `records`, and reverse zones with
  PTRs for IPv4 and IPv6, each checked with `named-checkzone`
- configures primary, secondary and forward zones from **one shared zone
  list**, with TSIG-signed transfers and NOTIFY
- only raises a zone's serial when its content actually changes

## Requirements

- ansible-core 2.16 or later, with facts gathered
- One of these platforms:

| Platform | BIND package | Version |
|---|---|---|
| Rocky / Alma / RHEL 10 | `bind` | 9.18 |
| Rocky / Alma / RHEL 9 | `bind9.18` (AppStream) | 9.18 |
| Debian 12 | `bind9` | 9.18 |
| Debian 13 | `bind9` | 9.20 |
| Ubuntu 24.04 | `bind9` | 9.18 |

On EL9 the role replaces the default `bind` 9.16 package with `bind9.18`.

No extra Python libraries or collections are needed on the controller or the
managed hosts.

## Example

Define zones once, for example in `group_vars/dns_servers.yml`. The host named
in `primary` becomes the primary for that zone; every other host running the
role becomes a secondary.

```yaml
bind_listen:
  ipv4: [any]
  ipv6: []

bind_acls:
  - name: clients
    match: [192.0.2.0/24, 198.51.100.0/24, localhost]

bind_options:
  recursion: true
  allow_query: [clients]
  allow_recursion: [clients]
  forwarders: [192.0.2.53]
  forward: only

bind_tsig_keys:
  - name: xfer
    algorithm: hmac-sha256
    secret: "{{ vault_bind_xfer_secret }}"   # tsig-keygen -a hmac-sha256 xfer

bind_zones:
  - name: example.internal
    primary: ns1.example.internal            # inventory hostname
    primaries:
      - address: 192.0.2.11
        key: xfer
    name_servers: [ns1, ns2]
    allow_transfer: ["key xfer"]
    reverse_zones: [192.0.2.0/24, 198.51.100.0/24, "2001:db8::/64"]
    hosts:
      - {name: ns1, ip: 192.0.2.11}
      - {name: ns2, ip: 192.0.2.12}
      - {name: www, ip: 192.0.2.20, ipv6: "2001:db8::20", aliases: [web]}
      - {name: gitlab, ip: 192.0.2.28, aliases: [git]}
      - {name: "*.pages.gitlab", ip: 192.0.2.28}
    records:
      - {name: _ntp._udp, type: SRV, value: "0 100 123 time1"}
      - {name: "@", type: MX, value: "10 mail"}
```

```yaml
- hosts: dns_servers
  roles:
    - seeharsy.bind
```

## Role variables

Dict variables (`bind_listen`, `bind_options`, `bind_zone_defaults`,
`bind_statistics`) are merged over their defaults, so set only the keys you
change. The role validates every variable against
`meta/argument_specs.yml` before it does anything, so a misspelt key fails the
run with a list of valid keys.

### Server

| Variable | Default | Meaning |
|---|---|---|
| `bind_listen.ipv4` | `[127.0.0.1]` | Addresses for `listen-on`. `[]` disables IPv4. |
| `bind_listen.ipv6` | `["::1"]` | Addresses for `listen-on-v6`. `[]` disables IPv6. |
| `bind_listen.port` | `53` | Port for plain DNS. |
| `bind_acls` | `[]` | `{name, match}` entries. An ACL grants nothing until you use its name. |
| `bind_options.recursion` | `false` | Enable recursion. |
| `bind_options.allow_query` | `[localhost]` | Who may query. |
| `bind_options.allow_recursion` | `[localhost, localnets]` | Who may recurse. |
| `bind_options.allow_transfer` | `[none]` | Server-wide transfer policy; zones can override it. |
| `bind_options.forwarders` | `[]` | Global forwarders. |
| `bind_options.forward` | `first` | `first` or `only`. |
| `bind_options.dnssec_validation` | `auto` | `auto`, `yes` (bring your own `trust-anchors`) or `no`. |
| `bind_options.validate_except` | `[]` | Names exempt from DNSSEC validation. |
| `bind_options.query_log` | `false` | Log queries to `data/query.log`. |
| `bind_options.zone_statistics` | unset (BIND: `terse`) | Statistics level for every zone: `full`, `terse` or `none`. See [Statistics](#statistics). |
| `bind_options.rate_limit` | off | Response rate limiting. See [Response rate limiting](#response-rate-limiting). |
| `bind_options_extra` | `""` | Raw statements appended inside `options { }`. |
| `bind_tsig_keys` | `[]` | `{name, algorithm, secret}`. Written to a separate `0640 root:named` file, never logged. |
| `bind_tls` | `{}` | Certificate, key and CA for encrypted DNS. See [Encrypted DNS](#encrypted-dns). |
| `bind_dot` | disabled | `{enabled, port}` for DNS over TLS (853). |
| `bind_doh` | disabled | `{enabled, port, endpoints}` for DNS over HTTPS (443, `/dns-query`). |
| `bind_local_zones` | `[]` | Zones every server answers by itself. See [Local zones](#local-zones). |
| `bind_statistics` | disabled | `{enabled, host, port, allow}` for the statistics channel. |
| `bind_extra_include_files` | `[]` | Files included at the end of `named.conf`. |
| `bind_package_state` | `present` | `latest` upgrades BIND on every run. |
| `bind_remove_stale_files` | `true` | Remove zone files the role wrote for zones that are gone, and v5's leftovers. See [Stale files](#stale-files). |

Paths, package and service names (`bind_packages`, `bind_service`,
`bind_config`, `bind_conf_dir`, `bind_dir`, `bind_zone_dir`,
`bind_secondary_dir`, `bind_dynamic_dir`, `bind_user`, `bind_group`) default to the platform's
values and can be overridden.

### SOA defaults

`bind_zone_defaults` sets `ttl` (1h), `refresh` (1d), `retry` (1h), `expire`
(1w), `minimum` (1h) and `hostmaster` (`hostmaster`). Every zone can override
any of them. `hostmaster` is only the SOA mailbox field; nothing sends mail to
it. It accepts a name (`hostmaster` becomes `hostmaster.<zone>.`), an FQDN, or
`user@domain`.

### Zones

| Key | Meaning |
|---|---|
| `name` | Zone name. Required. |
| `primary` | Inventory hostname of the primary. That host is primary; all others are secondaries. |
| `type` | `primary`, `secondary` or `forward`. Overrides the role worked out from `primary`. |
| `primaries` | Addresses secondaries transfer from: `"192.0.2.11"` or `{address, key, port, tls_name}`. Required on secondaries. |
| `name_servers` | NS records. Relative names are inside the zone. Required on the primary. |
| `allow_transfer` | Who may transfer this zone, e.g. `["key xfer"]` or an ACL name. |
| `also_notify` | Extra NOTIFY targets, as strings or `{address, key, port}`. |
| `notify` | `yes` (default), `no` or `explicit`. |
| `reverse_zones` | CIDRs to build reverse zones for (see below). |
| `hosts` | `{name, ip, ipv6, aliases, ptr, ttl}`. `ip` and `ipv6` take one address or a list. |
| `records` | `{name, type, value, ttl}` for any other record type. `value` is written exactly as given. |
| `forwarders`, `forward` | Forward zone targets and mode (`only` by default). |
| `validate` | `false` exempts a forward zone from DNSSEC validation. |
| `allow_query` | Who may query this zone. Overrides `bind_options.allow_query`. Not for forward zones. |
| `statistics` | `full`, `terse` or `none` (or `true`/`false`). Not for forward zones. |
| `update_policy` | Makes the zone dynamic. See [Dynamic zones](#dynamic-zones). |
| `xot` | `true` accepts transfers over TLS only. See [Zone transfers over TLS](#zone-transfers-over-tls). |
| `ttl`, `refresh`, `retry`, `expire`, `minimum`, `hostmaster` | Per-zone overrides. |

### Reverse zones and PTRs

- Reverse zones exist only for the CIDRs you list. Nothing is created
  implicitly.
- `/8`, `/16` and `/24` map to one zone each. Other IPv4 prefixes are split, so
  a `/20` becomes sixteen `/24` zones. Prefixes longer than `/24` need RFC 2317
  delegation, which the role does not generate.
- IPv6 prefixes on a nibble boundary map to one zone; others are split, so a
  `/38` becomes four `/40` zones.
- Several forward zones may list the same CIDR. The reverse zone is generated
  once, with PTRs for the hosts of every zone. Because one reverse zone has one
  set of access rules, zones that share a CIDR must use the same `primary`,
  `type`, `primaries`, `allow_transfer`, `allow_query` and `xot`; otherwise the run
  stops with an error naming both zones.
- A reverse zone takes the most detailed `statistics` level of the zones that
  share it.
- Every address of every host gets a PTR, except wildcard names and hosts with
  `ptr: false`. When several hosts share an address, the first one wins unless
  another sets `ptr: true`.

### Forward zones and DNSSEC

With `dnssec_validation: auto`, BIND checks forwarded answers against the
signed root. A forward zone for a private name that does not exist in public
DNS (`corp.internal`, `*.test`, an AD domain) then fails with SERVFAIL and
"broken trust chain" in the log. Set `validate: false` on that forward zone,
or list the name in `bind_options.validate_except`. Validation stays on for
everything else.

On a network with no route to the internet, recursion cannot reach the root
servers to validate anything. Set `bind_options.dnssec_validation: "no"` there,
or exempt the names your forwarders serve.

### Local zones

`bind_local_zones` lists zones that every server running the role answers by
itself, with no transfers, no NOTIFY and no updates. Use them to make lookups
for names that can never resolve fail at once, for example on a network with
no internet access, where clients would otherwise wait for timeouts.

```yaml
bind_local_zones:
  - name: example.com              # every name inside is NXDOMAIN
  - name: telemetry.example
    statistics: full               # count who still asks for it
  - name: sinkhole.example
    records:
      - {name: "*", type: A, value: 192.0.2.1}
```

- Zones without `records` share one empty zone file, so nothing inside them
  exists. Zones with `records` get their own file.
- `allow_query` and `statistics` work as they do in `bind_zones`.
- A name may appear in only one of `bind_zones`, their generated reverse zones
  and `bind_local_zones`.
- Recursive clients get the local answer, not the real one. A client that
  validates DNSSEC itself will treat answers for a signed public name as
  bogus, which still fails the lookup but with SERVFAIL instead of NXDOMAIN.

### Statistics

`zone_statistics` (server-wide) and a zone's `statistics` set how much BIND
records per zone:

| Level | What is recorded |
|---|---|
| `none` | Nothing. The zone does not appear in the statistics. |
| `terse` | Serial and load time. BIND's default. |
| `full` | Per-zone query counters by response code and record type, transfers and NOTIFY. |

`full` keeps extra counters in memory for each zone. Read the counters from the
statistics channel (`bind_statistics`), for example
`curl http://127.0.0.1:8053/json/v1/zones`. The channel is plain HTTP without
authentication and shows your zone names, so keep it on `127.0.0.1`. To send
the numbers to Prometheus, run `bind_exporter` on each server against the
local channel and expose only the exporter.

### Response rate limiting

`bind_options.rate_limit` turns on RRL, which limits identical responses to one
client network. It protects authoritative servers on the internet from being
used to amplify attacks. Keys are BIND's `rate-limit` options with
underscores:

```yaml
bind_options:
  rate_limit:
    responses_per_second: 10
    window: 5
    exempt_clients: [clients]      # your own resolvers and clients
    log_only: true                 # watch the logs before enforcing
```

Valid keys: `responses_per_second`, `referrals_per_second`,
`nodata_per_second`, `nxdomains_per_second`, `errors_per_second`,
`all_per_second`, `window`, `slip`, `qps_scale`, `ipv4_prefix_length`,
`ipv6_prefix_length`, `exempt_clients`, `log_only`, `max_table_size` and
`min_table_size`. A server that only answers your own network rarely needs
RRL; if you enable it, exempt your clients, because many clients behind one
NAT address look like one client.

### Dynamic zones

A zone with `update_policy` accepts updates signed with a TSIG key, for
example from a DHCP server or an ACME client:

```yaml
bind_tsig_keys:
  - {name: dhcp, algorithm: hmac-sha256, secret: "{{ vault_bind_dhcp_secret }}"}
  - {name: acme, algorithm: hmac-sha256, secret: "{{ vault_bind_acme_secret }}"}

bind_zones:
  - name: dyn.example.internal
    primary: ns1
    name_servers: [ns1.example.internal., ns2.example.internal.]
    primaries: [{address: 192.0.2.11, key: xfer}]
    allow_transfer: ["key xfer"]
    update_policy:
      - {key: dhcp, match: zonesub, types: [A, AAAA, TXT, DHCID]}
      - {key: acme, match: name, name: _acme-challenge.dyn.example.internal, types: [TXT]}
```

- Each rule needs a `key` from `bind_tsig_keys`, a `match` (`name`,
  `subdomain`, `wildcard` or `zonesub`), a `name` unless the match is
  `zonesub`, and the record `types` the key may change. `action: deny` adds a
  deny rule. There is no default type list, so each key gets only the types
  you name; avoid `ANY`, which would let the key change NS records.
- Updates by IP address (`allow-update`) are not supported, because a forged
  source address would pass.
- The role writes the zone file once, from `hosts` and `records`, and never
  again: BIND keeps updates in a journal and rewrites the file itself. After
  the first run, change the zone with `nsupdate`, not with the role.
- The file lives in `bind_dynamic_dir` (`/var/named/dynamic` on EL,
  `/var/lib/bind/dynamic` on Debian), where named may write. On EL, a
  different directory needs the `named_cache_t` SELinux type.
- Reverse zones generated from a dynamic zone are static. To let a key update
  PTRs, define the reverse zone in `bind_zones` by name, with its own
  `update_policy`, instead of listing it in `reverse_zones`.
- Secondaries transfer dynamic zones like any other zone.
- Create keys with `tsig-keygen -a hmac-sha256 <name>` and store the secret in
  Ansible Vault.

## Encrypted DNS

BIND serves DNS over TLS (DoT, RFC 7858) and DNS over HTTPS (DoH, RFC 8484)
next to plain DNS, on the `bind_listen` addresses:

```yaml
bind_tls:
  cert: "{{ lookup('file', 'pki/' ~ inventory_hostname ~ '.pem') }}"   # leaf + intermediates
  key: "{{ vault_bind_tls_key }}"
  ca: "{{ lookup('file', 'pki/internal-ca.pem') }}"                     # for XoT on secondaries

bind_dot:
  enabled: true            # port 853

bind_doh:
  enabled: true            # port 443, path /dns-query
```

- Give `cert`, `key` and `ca` as PEM content, or `cert_file`, `key_file` and
  `ca_file` as paths to files already on the server, for example ones another
  tool renews. Keep the key in Ansible Vault. The role writes content to
  `<conf dir>/tls/` as `0640 root:named`, and never logs or diffs the key.
- The certificate needs a `subjectAltName` for every name clients use to reach
  the server, and an IP entry if clients connect by address.
- TLS 1.2 and 1.3 only. TLS 1.2 is limited to ECDHE with AES-GCM or
  ChaCha20, and session tickets are off. Change `bind_tls.protocols` and
  `bind_tls.ciphers` to tighten this further, for example to TLS 1.3 only.
- A new certificate takes effect when the role runs again: BIND loads it on
  reload, without a restart.
- Encryption does not change who may use the server. `allow_query` and
  `allow_recursion` apply to DoT and DoH as they do to port 53.
- The role does not manage the firewall. Open 853/tcp for DoT and 443/tcp for
  DoH.
- On EL with SELinux enforcing, 443 is an HTTP port, so the role turns on the
  `named_tcp_bind_http_port` boolean when DoH is enabled. Port 853 is already
  a DNS port. Another DoH port must be labelled `http_port_t` or
  `dns_port_t` first.

### Zone transfers over TLS

With `xot: true`, the primary accepts transfers of that zone only over TLS,
on its DoT listener, and secondaries check the primary's certificate before
transferring:

```yaml
bind_zones:
  - name: example.internal
    primary: ns1
    xot: true
    allow_transfer: ["key xfer"]
    primaries:
      - {address: 192.0.2.11, key: xfer, tls_name: ns1.example.internal}
```

- The primary needs `bind_dot.enabled`. Each secondary needs `bind_tls.ca` (or
  `ca_file`), the CA that signed the primary's certificate.
- `tls_name` must match a name in the primary's certificate. A secondary
  refuses to transfer from a server that cannot prove it.
- Keep the TSIG key. TLS hides the zone and proves which server is the
  primary; the key proves which servers may transfer.
- NOTIFY stays on plain DNS, signed with the key as before.
- When a secondary cannot connect to a primary, BIND remembers that primary
  as unreachable and skips transfers from it, NOTIFY included, for minutes
  (the log says `is unreachable (cached)`). This happens when the secondary
  is configured before the primary's DoT listener exists, so configure the
  primary first. Restarting named on the secondary clears it at once.

### Clients

What each client needs to use DoT or DoH with certificates from an internal
CA:

| Client | How | Notes |
|---|---|---|
| Ubuntu, Debian with systemd-resolved | `DNS=192.0.2.11#ns1.example.internal` and `DNSOverTLS=yes` in `resolved.conf` | The `#name` is checked against the certificate. Add the internal CA to the system store (`update-ca-certificates`). `opportunistic` encrypts without checking and falls back to plain DNS. |
| Rocky Linux | Install `systemd-resolved` and configure it as above | NetworkManager uses plain DNS by default. Add the CA with `update-ca-trust`. |
| Proxmox VE | As for Debian, or keep plain DNS on a trusted management network | Proxmox writes `/etc/resolv.conf` itself; check that it points at resolved before relying on it. |
| macOS, iOS, iPadOS | A configuration profile with a DNS Settings payload (`DNSProtocol` `TLS` or `HTTPS`, `ServerName`, `ServerAddresses` or `ServerURL`) | Install the internal CA in the same profile. On iOS, also turn on full trust for it under Settings > General > About > Certificate Trust Settings. |
| Android | See below | |

**Android** cannot use an internal CA for Private DNS. In strict mode
(Settings > Private DNS > hostname), Android's resolver trusts only the
system CA store, `/system/etc/security/cacerts`, and ignores CAs the user
installs. A certificate from an internal CA fails, and Android then sends no
DNS at all. The choices are:

- a certificate from a public CA for a hostname in a public domain you
  control, which every client then trusts;
- Automatic mode, where Android tries DoT to the DNS servers it received from
  DHCP without checking the certificate. This encrypts the traffic but does
  not prove which server answered;
- plain DNS on networks you trust.

Ubuntu 26.04 confines `dig` with AppArmor: `+tls-ca` can only read files
under your home directory or the system certificate directories.

To test from a client:

```sh
dig +tls +tls-ca=$HOME/internal-ca.pem +tls-hostname=ns1.example.internal @192.0.2.11 example.internal SOA
dig +https +tls-ca=$HOME/internal-ca.pem +tls-hostname=ns1.example.internal @192.0.2.11 example.internal SOA
```

## Security defaults

- Zone transfers are refused unless you allow them (`allow_transfer: [none]`).
- ACLs do not grant anything by themselves.
- Recursion is off. When you enable it, only `localhost` and `localnets` may
  recurse until you set `allow_recursion`.
- TSIG secrets live in their own `0640` file. Tasks that touch them use
  `no_log` and never show a diff.
- Every key is attached to the primary or NOTIFY target it belongs to, never
  to the server's own address.
- Zone names are checked before they are used as file names.
- Zones sharing a reverse zone must agree on who may query and transfer it.
- Dynamic updates need a TSIG key and an explicit list of record types; every
  key a zone refers to must exist in `bind_tsig_keys`.
- Local zones refuse transfers and updates.
- DoT and DoH are off by default. When on, they allow only TLS 1.2 and 1.3 with
  forward-secret AEAD ciphers and no session tickets, and the private key is
  `0640` and never logged.
- `xot` zones refuse transfers over plain TCP, and secondaries verify the
  primary's certificate and name.
- `named.conf` and every zone file are validated before they replace the live
  files.

## Stale files

Every zone file the role writes starts with a `; Hash:` line, in v5 and v6
alike. On each run the role removes:

- files with that header in `bind_zone_dir` and `bind_dir` whose zone is no
  longer configured, including v5's zone files in `/var/cache/bind` on
  Debian;
- v5's key file, `<conf dir>/auth_transfer.conf`;
- v5's secondary directory, `<bind_dir>/secondary`, where it differs from
  `bind_secondary_dir` (on EL).

Files without the header, such as the package's `named.ca` or files you
manage yourself, are never touched, and nothing outside those paths is
removed. Run with `--check --diff` to see what would go, or set
`bind_remove_stale_files: false` to keep everything.

## Serial numbers

Each zone file starts with a hash of its content. The role reads that hash on
each host and keeps the serial when nothing changed. When the content changes,
the serial becomes `YYYYMMDD00`, or the old serial plus one if that is higher.
Serials from v5, which used Unix time, are lower than the first v6 serial, so
secondaries see the change as an increase.

## Testing

```sh
python3 -m venv .venv && .venv/bin/pip install ansible-core ansible-lint yamllint pytest molecule "molecule-plugins[docker]"
.venv/bin/ansible-galaxy collection install community.docker -p .ansible/collections
export PATH=$PWD/.venv/bin:$PATH ANSIBLE_COLLECTIONS_PATH=$PWD/.ansible/collections

pytest -q tests/unit                     # filter plugin
yamllint . && ansible-lint               # lint
molecule/run-all.sh                      # every platform at once
molecule/run-all.sh debian13             # or just some
```

The Molecule scenario runs a primary and a secondary and checks every record
and PTR on both servers, matching serials and authoritative answers, TSIG and
ACL transfer controls, a forward zone, NOTIFY propagation of a change, and
that the secondary keeps answering while the primary is stopped. CI runs it
on all five platforms.

## License

BSD. See [LICENSE.md](LICENSE.md).

## Contributors

v6 adopts fixes and ideas from upstream issues and pull requests by
[brlin-tw](https://github.com/brlin-tw),
[imp1sh](https://github.com/imp1sh),
[jesusmc](https://github.com/jesusmc),
[jof](https://github.com/jof),
[mikelsid](https://github.com/mikelsid),
[oisteink](https://github.com/oisteink),
[patrickstump](https://github.com/patrickstump),
[roumano](https://github.com/roumano),
[rowanthorpe](https://github.com/rowanthorpe) and
[sangdrax8](https://github.com/sangdrax8). Thank you.

Upstream maintainers:

- [Bert Van Vreckem](https://github.com/bertvv/)
- [Gregory Shulov](https://github.com/GR360RY)
- [Stuart Knight](https://github.com/blofeldthefish)

Contributors:

- [Aido](https://github.com/aido)
- [Angel Barrera](https://github.com/angelbarrera92)
- [B. Verschueren](https://github.com/bverschueren)
- [Boris Momčilović](https://github.com/kornrunner)
- [Brad Durrow](https://github.com/bdurrow)
- [Christopher Hicks](http://www.chicks.net/)
- [David J. Haines](https://github.com/dhaines)
- [Fabio Rocha](https://github.com/frock81)
- [Fazle Arefin](https://github.com/fazlearefin)
- [flora-five](https://github.com/flora-five)
- [Greg Cockburn](https://github.com/gergnz)
- [Guillaume Darmont](https://github.com/gdarmont)
- [itbane](https://github.com/itbane)
- [jadjay](https://github.com/jadjay)
- [Jascha Sticher](https://github.com/itbane)
- [Joanna Delaporte](https://github.com/jdelaporte)
- [Jörg Eichhorn](https://github.com/jeichhorn)
- [Jose Taas](https://github.com/josetaas)
- [Lennart Weller](https://github.com/lhw)
- [Loic Dachary](http://dachary.org)
- [Mario Ciccarelli](https://github.com/kartone)
- [Miroslav Hudec](https://github.com/mihudec)
- [Otto Sabart](https://github.com/seberm)
- [Paulius Mazeika](https://github.com/pauliusm)
- [Paulo E. Castro](https://github.com/pecastro)
- [Peter Janes](https://github.com/peterjanes)
- [psa](https://github.com/psa)
- [Rafael Bodill](https://github.com/rafi)
- [Rayford Johnson](https://github.com/rayfordj)
- [Robin Ophalvens](https://github.com/RobinOphalvens)
- [Romuald](https://github.com/rds13)
- [roumano](https://github.com/roumano)
- [Shawn Wilsher](https://github.com/sdwilsh)
- [Tom Meinlschmidt](https://github.com/tmeinlschmidt)
- [Jascha Sticher](https://github.com/itbane)
- [Zephyr82](https://github.com/Zephyr82)
