# transmission-dashboard

A self-hosted web dashboard for [`transmission-daemon`](https://transmissionbt.com/).
It shows your torrents, live speeds, disk and system stats, an event/history
log, and can copy finished downloads to a media server over SSH/rsync
(with Plex/Jellyfin library refresh). An optional topbar indicator surfaces
WireGuard tunnel health so you notice a dropped VPN before traffic leaks.

It's a single-user Flask app: one login, polling APIs, SQLite for history.
Runs comfortably on a Raspberry Pi or small VPS behind gunicorn + systemd.

---

## Requirements

- **Python 3.9+**
- **transmission-daemon** running somewhere reachable (or use mock mode for
  development — see [Development](#development--mock-mode))
- For the media-copy feature: `ssh` and `rsync` on this host, and key-based
  SSH access to the media server
- For the tunnel indicator (optional): `wireguard-tools` (`wg`) on this host
- For the media-bitrate readout (optional): `ffprobe` (from `ffmpeg`) on this
  host, which must also be able to read the download directory. Without it
  the bitrate is simply omitted from the cards.

System packages (Debian/Ubuntu example):

```bash
sudo apt install python3 python3-venv rsync openssh-client ffmpeg
```

---

## Set up Transmission first

The dashboard is a **front-end for an existing `transmission-daemon`** — it does
not install or manage the daemon itself. Get Transmission running with RPC
enabled before you set up the dashboard. (Skip this section entirely if you only
want to try the UI with `USE_MOCK=true`.)

On Debian/Ubuntu:

```bash
sudo apt install transmission-daemon
```

The RPC settings live in `settings.json` (on the Debian/Ubuntu package,
`/etc/transmission-daemon/settings.json`). **Important: stop the daemon before
editing it** — Transmission rewrites this file on exit and will overwrite your
changes otherwise:

```bash
sudo systemctl stop transmission-daemon
sudo $EDITOR /etc/transmission-daemon/settings.json
```

Set at least these keys so the dashboard can reach the RPC:

```jsonc
{
  "rpc-enabled": true,
  "rpc-authentication-required": true,
  "rpc-username": "your-rpc-user",
  "rpc-password": "your-rpc-password",   // plaintext here; Transmission
                                          // hashes it on next start
  "rpc-whitelist-enabled": true,
  "rpc-whitelist": "127.0.0.1",           // widen if the dashboard runs on a
                                          // different host than the daemon
  "download-dir": "/var/lib/transmission-daemon/downloads"
}
```

Then start it again:

```bash
sudo systemctl start transmission-daemon
sudo systemctl enable transmission-daemon    # start on boot
```

These map directly onto the dashboard's `.env` (next section):

| `settings.json` | `.env` |
|-----------------|--------|
| host the daemon runs on | `TR_HOST` (default `127.0.0.1`) |
| `rpc-port` (default 9091) | `TR_PORT` |
| `rpc-username` | `TR_USER` |
| `rpc-password` | `TR_PASS` |
| `download-dir` | `DOWNLOAD_DIR` |

Quick sanity check that RPC is up and your credentials work:

```bash
transmission-remote 127.0.0.1:9091 -n 'your-rpc-user:your-rpc-password' -l
```

That should list torrents (or an empty list) rather than an auth error.

---

## Install

```bash
git clone <your-fork-url> transmission-dashboard
cd transmission-dashboard

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# Edit .env and fill in every field — see the comments in the file.
$EDITOR .env
```

At minimum you must set:

- `DASHBOARD_USER` / `DASHBOARD_PASS` — your login. **Login is disabled and the
  whole dashboard is inaccessible until both are set** (there are no default
  credentials).
- `TR_USER` / `TR_PASS` — Transmission RPC credentials (skip these only if you
  set `USE_MOCK=true`).

You do **not** need to set `FLASK_SECRET_KEY`: on first run the app generates a
random one and saves it to a gitignored `.flask_secret` file (chmod 600),
reusing it on later runs so your sessions survive restarts.

If anything required is missing, the app prints a clear warning listing the
gaps on startup rather than failing cryptically later.

---

## Run

### Local (development)

```bash
.venv/bin/python app.py
```

Serves on `http://127.0.0.1:5000` with Flask's dev server (localhost only).

### Production (systemd + gunicorn)

Running under systemd is the recommended way to run the dashboard in
production: it keeps the app alive across crashes and **starts it
automatically on boot**, with no terminal or manual step after a reboot.

A unit template is provided at
`systemd/transmission-dashboard.service.example`. Install it, then enable it:

```bash
# 1. Copy the template into place and fill in User/Group/paths
sudo cp systemd/transmission-dashboard.service.example \
  /etc/systemd/system/transmission-dashboard.service
sudoedit /etc/systemd/system/transmission-dashboard.service   # replace every CHANGE_ME

# 2. Load the new unit and start it at boot + right now
sudo systemctl daemon-reload
sudo systemctl enable --now transmission-dashboard
```

#### What to change in the unit file

The template ships with `CHANGE_ME` placeholders — **5 of them, across 4
values, all in the `[Service]` block.** Replace each:

| Field in the template | Set it to | Example (user `bob`) |
|-----------------------|-----------|----------------------|
| `User=CHANGE_ME` | the account that owns the checkout | `User=bob` |
| `Group=CHANGE_ME` | that account's group (same name on Debian/Ubuntu) | `Group=bob` |
| `WorkingDirectory=/home/CHANGE_ME/transmission-dashboard` | absolute path to the repo | `/home/bob/transmission-dashboard` |
| `EnvironmentFile=/home/CHANGE_ME/transmission-dashboard/.env` | absolute path to `.env` | `/home/bob/transmission-dashboard/.env` |
| `ExecStart=/home/CHANGE_ME/…/.venv/bin/gunicorn` | absolute path to the venv's gunicorn | `/home/bob/transmission-dashboard/.venv/bin/gunicorn` |

Since all three paths sit under the same home directory, the quickest edit is
a find-and-replace of `CHANGE_ME` → your username throughout the file. Leave
`--bind 127.0.0.1:5000` alone unless you deliberately want a different port
(localhost-only is intended — front it with a reverse proxy for external
access), and leave the `wg`/`CAP_NET_ADMIN` lines as they are (harmless if you
don't use the tunnel indicator, required if you do).

Confirm no placeholder survived before enabling:

```bash
grep CHANGE_ME /etc/systemd/system/transmission-dashboard.service   # should print nothing
```

`enable` is the part that makes it start on boot — it links the unit into
`multi-user.target` (see the `[Install]` section of the file), so systemd
launches it every time the machine comes up. `--now` also starts it
immediately so you don't have to reboot to try it. (Use plain
`systemctl start` / `stop` for a one-off run that does **not** touch the
boot setting.)

Before you edit the unit, make sure a virtualenv exists at `<repo>/.venv` with
the dependencies installed (from the [Install](#install) step above) — the
`ExecStart` line runs `.venv/bin/gunicorn`, so the service won't start without
it.

Verify it's running and confirm the boot hook took:

```bash
systemctl status transmission-dashboard      # should say "active (running)"
systemctl is-enabled transmission-dashboard  # should print "enabled"
journalctl -u transmission-dashboard -f      # follow the logs (Ctrl-C to stop)
```

The surest test is to `sudo reboot` and check `systemctl status` again once the
box is back — the dashboard should already be up with no intervention.

The unit runs a single gunicorn worker with 8 threads (intentional — see the
comments in the file), binding to `127.0.0.1:5000`. It starts after
`network-online.target` so the network is up first. If `transmission-daemon`
runs on the same host, you don't need to add an ordering dependency — the
dashboard tolerates the daemon not being ready yet and simply shows a "stale"
tick until RPC answers. Put the dashboard behind a reverse proxy (nginx/Caddy)
with TLS if you expose it beyond localhost.

To stop it starting on boot later, `sudo systemctl disable --now
transmission-dashboard` (the `--now` also stops the running instance).

---

## Access over Tailscale

The dashboard binds to `127.0.0.1:5000` — localhost only, by design. The
cleanest way to reach it from your other devices is [Tailscale](https://tailscale.com):
its `serve` command reverse-proxies the local port across your tailnet with
automatic HTTPS, and it's reachable **only** by devices signed into your
tailnet (never the public internet). You leave the gunicorn bind on localhost —
that stays the security boundary; Tailscale sits in front of it.

**One-time tailnet setup:**

1. Install Tailscale on the server and bring it up: `sudo tailscale up`.
2. In the [admin console](https://login.tailscale.com/admin/dns) → **DNS**,
   enable **MagicDNS** and **HTTPS Certificates** — `tailscale serve` needs
   these to issue the TLS cert.

**Then, on the server:**

```bash
tailscale serve --bg 5000
```

That forwards `https://<host>.<tailnet>.ts.net` (port 443) →
`http://127.0.0.1:5000`. `--bg` runs it in the background persistently — the
mapping lives in `tailscaled` state and **survives reboots on its own**, so it
needs no systemd unit of its own.

Check it and find the URL:

```bash
tailscale serve status      # shows the mapping and the public tailnet URL
tailscale status | head -1  # this machine's tailnet name
```

Open `https://<host>.<tailnet>.ts.net` from any device on the tailnet. The
dashboard's own login (`DASHBOARD_USER` / `DASHBOARD_PASS`) still applies on
top. To remove the mapping later: `tailscale serve reset`.

> **Don't use `tailscale funnel`** for this — funnel exposes the app to the
> *public* internet, which you almost certainly don't want for a personal
> torrent dashboard. `serve` keeps it tailnet-only.

If you'd rather not enable HTTPS certs, you can instead bind gunicorn to all
interfaces (change `--bind 127.0.0.1:5000` to `--bind 0.0.0.0:5000` in the
systemd unit) and reach it at `http://<host>:5000` — but that's plaintext HTTP
and also listens on every other interface the box has (LAN included), so it's a
wider exposure. Prefer `tailscale serve`.

---

## Updating

When new changes are pushed to the repo, an **"N updates" badge** appears in
the dashboard topbar. To apply them, on the server:

```bash
./update.sh
```

`update.sh` runs `git pull --ff-only`, reinstalls dependencies into `.venv`,
and restarts the systemd service if the unit is installed (otherwise it tells
you to restart manually). Your config, database and runtime state are all
gitignored, so a pull **never** clobbers them.

This is the recommended workflow for anyone running a clone: the server pulls
published commits straight from GitHub.

---

## Deploying from a separate dev box (optional)

`update.sh` above is all most people need. `deploy.sh` covers a different
workflow: you **edit the code on one machine (a laptop/dev box) and run the app
on another (a server/VPS)**, and you want to ship your working tree directly
over SSH instead of committing and pushing through GitHub first.

Use `deploy.sh` if:

- You develop locally and test on a separate always-on box, **and**
- You want to push *uncommitted* work to that box quickly, without a
  round-trip through GitHub.

Stick with `update.sh` (and skip `deploy.sh` entirely) if:

- You develop and run on the **same** machine, **or**
- Your server pulls from GitHub — i.e. you commit, push, then run `./update.sh`
  on the server. This is simpler and keeps the server's history clean.

### Setup

`deploy.sh` is gitignored because it holds your personal server address. A
template ships as `deploy.sh.example`:

```bash
cp deploy.sh.example deploy.sh
chmod +x deploy.sh
$EDITOR deploy.sh          # set VPS_HOST and REMOTE_DIR
```

Set up key-based SSH to the server first so it runs non-interactively
(`ssh-copy-id user@your-server`), then from your dev box:

```bash
./deploy.sh
```

It `rsync`s the repo to the server — **excluding** `.env`, `.flask_secret`, the
database, and all runtime state, so it never overwrites the server's config or
data — then reinstalls dependencies and restarts the service remotely. The
template also includes an optional VPN pre-check you can delete if you don't
want it.

Because it copies your working tree directly (not git commits), the server's
checkout can drift from GitHub. If you rely on the update badge, remember it
compares the server against `origin/main`, so commit and push periodically too.

---

## Tunnel setup

The topbar can show a live WireGuard tunnel-health indicator so you can tell
at a glance that Transmission's traffic is actually flowing through your VPN.

### `TUNNEL_IFACE`

This is the one setting that turns the indicator on. Set it in `.env` to the
**name of your WireGuard interface** — just the interface name, not an IP
address or a config path. On most setups that's whatever `wg-quick up <name>`
brought up (e.g. `mullvad`, `wg0`).

Find the exact name with either of these:

```bash
sudo wg show          # lists each interface by name (the "interface:" line)
ip link show          # WireGuard devices show up here too
```

Then put that name in `.env`:

```
TUNNEL_IFACE=mullvad
```

Behavior:

- **Set to a valid interface** → the topbar shows the live tunnel indicator
  (green when healthy, red when down).
- **Left blank** (the default) → the topbar shows a neutral, muted
  **"Tunnel off"** chip and runs no live checks. It's just a hint that the
  feature exists and isn't set up — nothing is wrong and nothing leaks. Set
  `TUNNEL_IFACE` to turn it into a live health check.

The tunnel's IP is **not** configured here — it's read live off the interface
on every check, so it keeps working after your VPN hands you a new address.
Only the interface *name* is pinned in `.env`.

The indicator is designed so that **green means traffic is actually confined
to the tunnel — never a false all-clear.** It goes green only when *all* of
these hold, each read from live state (nothing hardcoded):

1. the interface exists, is up, and has an IPv4;
2. a peer handshake within `WG_HANDSHAKE_STALE_SEC`;
3. Transmission's `bind-address-ipv4` equals the interface's current IPv4;
4. **packets from that tunnel IPv4 actually egress the tunnel interface** —
   verified with `ip route get`, so a bind with a missing/broken policy route
   (which would leak or black-hole) can't show green;
5. **no IPv6 leak** — either the host has no globally-routable IPv6 on a bare
   interface, or Transmission's `bind-address-ipv6` is the tunnel's IPv6 *and*
   that traffic egresses the tunnel too.

Checks 4 and 5 close the two leaks a naïve bind check misses: a correct
`bind-address-ipv4` with no matching route still leaks, and an IPv4-only check
is blind to BitTorrent leaking your real address over IPv6. A leak shows a red
**"IPv6 leak"** / **"Route leak"** label (not just "down"), and if the check
genuinely can't confirm IPv6 is confined it shows amber rather than green.
Everything is derived live, so it keeps working after the tunnel IP changes.

Two optional knobs tune the checks (both commented in `.env.example`):
`WG_HANDSHAKE_STALE_SEC` (default 180 — how old a handshake may be before the
peer counts as down) and `TUNNEL_CHECK_CACHE_TTL` (default 30 — how long a
result is cached before the next probe). Both `wg show` and `ip route get`
back the checks; `ip` (iproute2) ships alongside `wg-quick`, so it's always
present where the tunnel is.

Reading the interface state uses `wg show`, which makes a netlink call that
needs `CAP_NET_ADMIN`. The systemd unit grants exactly that capability (see its
comments); without it the check sees no handshake and always reports "down". If
you run the app outside systemd, either grant the capability to the `wg` binary
(`sudo setcap cap_net_admin+ep "$(command -v wg)"`) or run it as root.

### Tunnel auto-recovery

A WireGuard session can wedge permanently: the kernel retries handshakes from
one fixed source port, so if the path dies underneath it (VPN relay reboot,
stale NAT mapping) every retry lands in a black hole — the tunnel stays down
indefinitely until someone bounces the interface, which picks a fresh port.

Set `TUNNEL_RECOVERY_CMD` in `.env` to have the dashboard bounce it for you:

```
TUNNEL_RECOVERY_CMD=sudo -n wg-quick down mullvad; sudo -n wg-quick up mullvad
```

The watchdog only fires when the tunnel has been continuously down with a
stale or missing handshake for `TUNNEL_RECOVERY_AFTER_SEC` (default 5 min) —
reasons a bounce can't fix (Transmission unbound, interface deliberately
removed with `wg-quick down`) never trigger it. It waits
`TUNNEL_RECOVERY_COOLDOWN_SEC` (default 10 min) between attempts and gives up
after `TUNNEL_RECOVERY_MAX_ATTEMPTS` (default 3) so an expired VPN account
doesn't flap the interface all night. Every attempt is logged to the event
history and shown in the tunnel indicator's tooltip.

`wg-quick` needs root, and the dashboard runs unprivileged — grant just those
two commands via sudoers (adjust user, path, and interface name):

```
# /etc/sudoers.d/transmission-dashboard-tunnel  (chmod 440, edit with visudo -f)
user ALL=(root) NOPASSWD: /usr/bin/wg-quick down mullvad, /usr/bin/wg-quick up mullvad
```

The `-n` in the command makes sudo fail fast instead of hanging on a password
prompt if the rule is missing.

<!-- TODO (maintainer): document your specific VPN/WireGuard setup here —
     how you generate the WG config, how Transmission's bind-address-ipv4 is
     pinned to the tunnel IP, and any provider-specific notes (e.g. Mullvad).
     Until this is filled in, the indicator is optional and safe to ignore. -->

---

## Media copy setup

Finished downloads can be copied to a media server over SSH/rsync straight
from the torrents list. Configure it in the **Settings** page (stored in a
gitignored `media_config.json` — nothing to edit by hand):

- **Host / user / port** of the media server. Set up key-based SSH first so
  copies run non-interactively:
  `ssh-copy-id user@media-host`.
- One or more **destination folders** (e.g. Movies, TV). Multiple same-named
  folders act as fallbacks — a copy picks the first with enough free space.
- Optional **bandwidth limit** so copies don't starve Transmission.
- Optional **library refresh**: point it at your Plex or Jellyfin server
  (URL + token) to auto-refresh the library after a successful copy.

Copies show live progress, handle season detection for TV, and record every
run in the History page.

---

## Development / mock mode

To work on the UI without a real Transmission daemon, set in `.env`:

```
USE_MOCK=true
```

This swaps in a built-in mock client (`mock_transmission.py`) with sample
torrents, so you can run `.venv/bin/python app.py` with no daemon and no RPC
credentials. You still need `DASHBOARD_USER`/`DASHBOARD_PASS` to log in.

Run the tests with:

```bash
.venv/bin/python -m pytest tests/
```

---

## Files & privacy (what's gitignored and why)

These files are created at runtime and are **never** committed — they hold
secrets or machine-specific state:

| File | What it is | Why it's ignored |
|------|-----------|------------------|
| `.env` | Your credentials and config | Secrets |
| `.flask_secret` | Auto-generated session-signing key | Secret |
| `dashboard.db`, `.db-wal`, `.db-shm` | SQLite history/events database | Local data, auto-created |
| `media_config.json` | Media-server copy settings (set via Settings UI) | Machine-specific + host details |
| `copy_state.json` | In-progress/last copy state | Runtime state, auto-created |
| `scan_state.json` | Media-scan progress state | Runtime state, auto-created |
| `deploy.sh` | Your personal rsync-deploy script | Contains your server host |
| `.venv/`, `__pycache__/`, `*.pyc` | Python virtualenv / bytecode | Build artifacts |

Templates ship for the personal ones: `.env.example`,
`deploy.sh.example`, and `systemd/transmission-dashboard.service.example`.
`media_config.json` is created for you the first time you save the Settings
page — there's no template to copy.
