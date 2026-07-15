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

System packages (Debian/Ubuntu example):

```bash
sudo apt install python3 python3-venv rsync openssh-client
```

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

A unit template is provided at
`systemd/transmission-dashboard.service.example`. Copy it, replace every
`CHANGE_ME`, and enable it:

```bash
sudo cp systemd/transmission-dashboard.service.example \
  /etc/systemd/system/transmission-dashboard.service
sudoedit /etc/systemd/system/transmission-dashboard.service   # set User/Group/paths

sudo systemctl daemon-reload
sudo systemctl enable --now transmission-dashboard
journalctl -u transmission-dashboard -f
```

The unit runs a single gunicorn worker with 8 threads (intentional — see the
comments in the file), binding to `127.0.0.1:5000`. Put it behind a reverse
proxy (nginx/Caddy) with TLS if you expose it beyond localhost.

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

---

## Tunnel setup

The topbar can show a live WireGuard tunnel-health indicator so you can tell
at a glance that Transmission's traffic is actually flowing through your VPN.
Set `TUNNEL_IFACE` in `.env` to your WireGuard interface name (e.g. the device
`wg show` lists). Leave it blank to hide the indicator entirely.

The indicator goes green only when the interface is up, a peer handshake is
recent, **and** Transmission's `bind-address-ipv4` matches the interface's
current IP — so it catches both a dropped tunnel and a leak out the bare link.
`wg show` needs `CAP_NET_ADMIN`; the systemd unit grants it (see its comments).

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
