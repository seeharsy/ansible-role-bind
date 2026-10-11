# Ansible role BIND

[![CI](https://github.com/seeharsy/ansible-role-bind/actions/workflows/ci.yml/badge.svg)](https://github.com/seeharsy/ansible-role-bind/actions/workflows/ci.yml)

> **Maintained continuation.** This repository continues
> [bertvv/ansible-role-bind](https://github.com/bertvv/ansible-role-bind), which
> has had no merges since 2021. Many thanks to Bert Van Vreckem and every
> upstream contributor; the full upstream history is preserved here.
> Issues and pull requests belong here, not upstream.
>
> **v6 is a breaking release** and is still in development. Variable names and
> structure have changed; a migration guide will ship with v6.0.0.

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
| `bind_options_extra` | `""` | Raw statements appended inside `options { }`. |
| `bind_tsig_keys` | `[]` | `{name, algorithm, secret}`. Written to a separate `0640 root:named` file, never logged. |
| `bind_statistics` | disabled | `{enabled, host, port, allow}` for the statistics channel. |
| `bind_extra_include_files` | `[]` | Files included at the end of `named.conf`. |
| `bind_package_state` | `present` | `latest` upgrades BIND on every run. |

Paths, package and service names (`bind_packages`, `bind_service`,
`bind_config`, `bind_conf_dir`, `bind_dir`, `bind_zone_dir`,
`bind_secondary_dir`, `bind_user`, `bind_group`) default to the platform's
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
| `primaries` | Addresses secondaries transfer from: `"192.0.2.11"` or `{address, key, port}`. Required on secondaries. |
| `name_servers` | NS records. Relative names are inside the zone. Required on the primary. |
| `allow_transfer` | Who may transfer this zone, e.g. `["key xfer"]` or an ACL name. |
| `also_notify` | Extra NOTIFY targets, as strings or `{address, key, port}`. |
| `notify` | `yes` (default), `no` or `explicit`. |
| `reverse_zones` | CIDRs to build reverse zones for (see below). |
| `hosts` | `{name, ip, ipv6, aliases, ptr, ttl}`. `ip` and `ipv6` take one address or a list. |
| `records` | `{name, type, value, ttl}` for any other record type. `value` is written exactly as given. |
| `forwarders`, `forward` | Forward zone targets and mode (`only` by default). |
| `validate` | `false` exempts a forward zone from DNSSEC validation. |
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
  transfer policy, zones that share a CIDR must use the same `primary`,
  `type`, `primaries` and `allow_transfer`; otherwise the run stops with an
  error naming both zones.
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
- `named.conf` and every zone file are validated before they replace the live
  files.

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
