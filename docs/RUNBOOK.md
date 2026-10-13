# Runbook

Everything below assumes the repository is cloned to `~/kassar-house-server`, the stack runs from
`stack/`, service data lives in `DATA_DIR=/srv/house` and the backup disk is mounted at `/mnt/backup`.

## 1. First setup checklist

1. Install Debian 12 or Ubuntu 24.04 (server, no desktop), Docker Engine with the Compose plugin, and give the server a fixed LAN address (DHCP reservation in the router).
2. Add your SSH public key to `~/.ssh/authorized_keys` **before** hardening, then:
   `sudo provision/setup.sh --harden 192.168.1.0/24`
3. Mount the backup disk by UUID with `nofail`, so the server still boots without it:
   ```
   UUID=…  /mnt/backup  ext4  defaults,nofail,x-systemd.device-timeout=10s  0 2
   ```
4. Edit `/etc/khs/khs.toml`, then `sudo khs config-check && sudo khs init /mnt/backup/khs`.
5. `cd stack && cp .env.example .env`, fill it in, `docker compose up -d`.
6. DNS: in the router (or `/etc/hosts` on each device) map `home.arpa`, `vault.home.arpa`,
   `media.home.arpa` and `sync.home.arpa` to `LAN_IP`.
7. Trust Caddy's CA (next section), then open `https://home.arpa`.
8. Run the first backup by hand and a restore drill (section 4) before relying on it.

## 2. Trusting the local certificate authority

Caddy creates its own root certificate the first time it starts:

```bash
docker compose exec caddy cat /data/caddy/pki/authorities/local/root.crt > house-root.crt
```

- **Linux**: `sudo cp house-root.crt /usr/local/share/ca-certificates/ && sudo update-ca-certificates`
- **macOS**: open it in Keychain Access → *System* → set to *Always Trust*
- **Windows**: `certutil -addstore -f Root house-root.crt` (administrator)
- **Android / iOS**: install it as a CA certificate in Settings (iOS also needs *Certificate Trust Settings* → enable)

After that, remove `insecure = true` from the HTTP check in `khs.toml` and install the same file on the server.

## 3. Daily operation

| Task | Command |
| --- | --- |
| Is everything fine? | `khs health` and `khs report` |
| When did the last backup run? | `systemctl list-timers 'khs-*'` and `journalctl -u khs-backup -n 30` |
| What's in the repository? | `sudo khs snapshots` |
| Update the containers | bump the image tags in `compose.yaml`, read the release notes, `docker compose pull && docker compose up -d` |
| Monthly integrity check | `sudo khs check --read-data` |

Notifications arrive only when a check *changes* state. If you get nothing for weeks, run
`khs health --notify` after stopping a container to confirm alerts still reach your phone.

## 4. Restore drill (do it monthly)

A backup that has never been restored is a hope, not a backup.

```bash
sudo khs restore latest --target /tmp/drill --include srv/house/vaultwarden
sudo diff -r /srv/house/vaultwarden /tmp/drill/srv/house/vaultwarden && echo "identical"
sudo rm -rf /tmp/drill
```

Differences in files that changed since the snapshot (databases, logs) are expected; anything else is not.

## 5. Restoring one service

Example: Vaultwarden's data was damaged.

```bash
cd ~/kassar-house-server/stack
docker compose stop vaultwarden
sudo khs snapshots                         # pick a snapshot from before the problem
sudo khs restore 20261012T0331 --target / --include srv/house/vaultwarden --overwrite
docker compose start vaultwarden
```

Owners and modes come back as they were (restores run as root keep the original uid/gid).

## 6. The whole server died

1. Install the OS and Docker on the new machine, same LAN address if possible.
2. Clone this repository and run `sudo provision/setup.sh` (add `--harden` once your SSH key is in place).
3. Mount the backup disk at `/mnt/backup` and check it: `sudo khs check --repo /mnt/backup/khs`.
4. Restore the configuration and data:
   ```bash
   sudo khs restore latest --repo /mnt/backup/khs --target / --include etc/khs
   sudo khs restore latest --repo /mnt/backup/khs --target / --include srv/house
   ```
5. Copy `stack/.env` from the restore (keep a copy of it inside a backed-up path such as `/srv/house/.env`) and `docker compose up -d`.
6. Re-run section 1, steps 6–8.

## 7. Replacing the backup disk

1. Mount the new disk at `/mnt/backup-new`, `sudo khs init /mnt/backup-new/khs`.
2. Stop the timer: `sudo systemctl stop khs-backup.timer`.
3. Swap the mounts (fstab), `sudo khs backup`, `sudo khs check --read-data`.
4. Keep the old disk somewhere else for a while: it is now your off-site copy.
5. `sudo systemctl start khs-backup.timer`.

## 8. Access from outside the house

Don't forward ports from the router. Use a VPN that puts your phone on the home network:

- **WireGuard** on the server (or on the router if it supports it), allowing only UDP 51820 through ufw; or
- **Tailscale**, which needs no open ports at all; then add the tailnet range to Caddy's allowed
  addresses or use Tailscale's own HTTPS names.
