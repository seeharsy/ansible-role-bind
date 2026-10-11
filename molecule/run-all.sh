#!/usr/bin/env bash
# Run the default Molecule scenario on every supported platform at once.
# Each run gets its own containers, network, subnet and Molecule state.
#
# Usage: molecule/run-all.sh [distro ...]     (default: all)
# Logs:  ${MOLECULE_LOG_DIR:-.molecule-logs}/<distro>.log
set -uo pipefail

declare -A IMAGES=(
  [rockylinux9]=docker.io/rockylinux/rockylinux:9
  [rockylinux10]=docker.io/rockylinux/rockylinux:10
  [debian12]=docker.io/library/debian:12
  [debian13]=docker.io/library/debian:13
  [ubuntu2404]=docker.io/library/ubuntu:24.04
)
declare -A NETS=([rockylinux9]=61 [rockylinux10]=62 [debian12]=63 [debian13]=64 [ubuntu2404]=65)

cd "$(dirname "$0")/.."
distros=("$@")
[ ${#distros[@]} -eq 0 ] && distros=(rockylinux9 rockylinux10 debian12 debian13 ubuntu2404)
logs=${MOLECULE_LOG_DIR:-.molecule-logs}
mkdir -p "$logs"

declare -A pids
for d in "${distros[@]}"; do
  [ -n "${IMAGES[$d]:-}" ] || { echo "unknown distro: $d" >&2; exit 2; }
  state=$(mktemp -d "${TMPDIR:-/tmp}/molecule-bind-$d.XXXXXX")
  MOLECULE_DISTRO=$d MOLECULE_IMAGE=${IMAGES[$d]} MOLECULE_NET=${NETS[$d]} \
    MOLECULE_EPHEMERAL_DIRECTORY=$state \
    molecule test >"$logs/$d.log" 2>&1 &
  pids[$d]=$!
  echo "started $d (pid ${pids[$d]})"
done

status=0
for d in "${distros[@]}"; do
  if wait "${pids[$d]}"; then
    echo "PASS $d"
  else
    echo "FAIL $d  (see $logs/$d.log)"
    status=1
  fi
done
exit $status
