#!/usr/bin/env bash
# Install khs and its timers on a Debian/Ubuntu home server; optionally harden the host.
#
#   sudo provision/setup.sh                 install khs, config (if absent), systemd timers
#   sudo provision/setup.sh --harden CIDR   also: ufw (LAN only), key-only SSH, automatic security updates
#   provision/setup.sh --dry-run ...        print what would change, change nothing
#
# Safe to run again: existing config is never overwritten and unchanged files are left alone.
# KHS_ROOT (tests only) installs into a scratch directory instead of /.
set -euo pipefail

ROOT="${KHS_ROOT:-}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DRY=0
HARDEN=0
LAN_CIDR=""

usage() { sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }
log() { printf '%s\n' "$*"; }
die() { printf 'setup: %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --harden) HARDEN=1; LAN_CIDR="${2:-}"; shift ;;
    -h|--help) usage ;;
    *) usage 2 >&2 ;;
  esac
  shift
done

if [[ $HARDEN -eq 1 && ! "$LAN_CIDR" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}/([0-9]|[12][0-9]|3[0-2])$ ]]; then
  die "--harden needs your LAN as an IPv4 CIDR, e.g. --harden 192.168.1.0/24"
fi
if [[ -z "$ROOT" ]]; then
  [[ $EUID -eq 0 || $DRY -eq 1 ]] || die "run as root (sudo), or with --dry-run"
  grep -qiE '^(ID|ID_LIKE)=.*debian' /etc/os-release 2>/dev/null || die "only Debian and Ubuntu are supported"
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || die "Python 3.11 or newer is required (tomllib)"

run() { # run a command, or print it in dry-run mode
  if [[ $DRY -eq 1 ]]; then log "would run: $*"; else "$@"; fi
}

# install_file SRC DEST MODE: copy only when content or mode differs; report what happened.
install_file() {
  local src="$1" dest="$ROOT$2" mode="$3"
  if [[ -f "$dest" ]] && cmp -s "$src" "$dest" && [[ "$(stat -c %a "$dest")" == "$mode" ]]; then
    log "unchanged  $2"; return 0
  fi
  if [[ $DRY -eq 1 ]]; then log "would install $2"; return 0; fi
  install -D -m "$mode" "$src" "$dest"
  log "installed  $2"
}

changed_units=0

# --- khs itself -------------------------------------------------------------
for f in "$SRC"/khs/*.py; do
  install_file "$f" "/opt/khs/khs/$(basename "$f")" 644
done
wrapper="$(mktemp)"
trap 'rm -f "$wrapper"' EXIT
cat >"$wrapper" <<'EOF'
#!/bin/sh
# khs launcher: isolated from PYTHON* variables and user site-packages.
exec /usr/bin/python3 -B -s -E -c 'import sys; sys.path.insert(0, "/opt/khs"); from khs.cli import main; sys.exit(main())' "$@"
EOF
install_file "$wrapper" /usr/local/bin/khs 755

if [[ -e "$ROOT/etc/khs/khs.toml" ]]; then
  log "kept       /etc/khs/khs.toml (existing config is never overwritten)"
else
  install_file "$SRC/etc/khs.example.toml" /etc/khs/khs.toml 600
  log "           edit /etc/khs/khs.toml, then run: khs config-check && khs init <repository>"
fi
[[ $DRY -eq 1 ]] || install -d -m 700 "$ROOT/var/lib/khs"

for u in "$SRC"/systemd/*; do
  before="$(cat "$ROOT/etc/systemd/system/$(basename "$u")" 2>/dev/null || true)"
  install_file "$u" "/etc/systemd/system/$(basename "$u")" 644
  [[ "$before" == "$(cat "$u")" ]] || changed_units=1
done
if [[ $changed_units -eq 1 ]]; then run systemctl daemon-reload; fi
run systemctl enable --now khs-backup.timer khs-health.timer

# --- optional host hardening --------------------------------------------------
if [[ $HARDEN -eq 1 ]]; then
  run apt-get install -y --no-install-recommends ufw unattended-upgrades

  # Automatic security updates.
  auto="$(mktemp)"
  printf 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n' >"$auto"
  install_file "$auto" /etc/apt/apt.conf.d/20auto-upgrades 644
  rm -f "$auto"

  # Firewall: everything closed except SSH, web and Syncthing from the LAN.
  run ufw default deny incoming
  run ufw default allow outgoing
  for rule in "22 proto tcp" "80 proto tcp" "443 proto tcp" "443 proto udp" "22000 proto tcp" "22000 proto udp" "21027 proto udp"; do
    # shellcheck disable=SC2086 # rule is intentionally split into words
    run ufw allow from "$LAN_CIDR" to any port $rule
  done
  run ufw --force enable

  # Key-only SSH, but only when the admin can already log in with a key: otherwise this would lock them out.
  admin="${SUDO_USER:-}"
  keys=""
  if [[ -n "$admin" ]]; then keys="$ROOT$(getent passwd "$admin" | cut -d: -f6)/.ssh/authorized_keys"; fi
  if [[ -n "$admin" && -s "$keys" ]]; then
    dropin="$(mktemp)"
    printf '# Managed by kassar-house-server provision/setup.sh\nPasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\n' >"$dropin"
    install_file "$dropin" /etc/ssh/sshd_config.d/50-house.conf 644
    rm -f "$dropin"
    if [[ $DRY -eq 0 ]]; then
      if sshd -t; then
        run systemctl reload ssh
      else
        rm -f "$ROOT/etc/ssh/sshd_config.d/50-house.conf"
        die "sshd rejected the new settings; removed them again, SSH is unchanged"
      fi
    fi
  else
    log "skipped    key-only SSH: no authorized_keys for '${admin:-unknown}' (add a key first, or you would be locked out)"
  fi
fi

log "done"
