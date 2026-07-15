import os
import secrets

from dotenv import load_dotenv

load_dotenv()

TR_HOST = os.getenv("TR_HOST", "127.0.0.1")
TR_PORT = int(os.getenv("TR_PORT", "9091"))
TR_USER = os.getenv("TR_USER")
TR_PASS = os.getenv("TR_PASS")
# Transmission 4.x no longer surfaces bind-address-ipv4 via session-get, so
# the dashboard reads it from settings.json when the RPC omits it. Default
# matches the Debian/Ubuntu transmission-daemon package layout.
TR_SETTINGS_FILE = os.getenv(
    "TR_SETTINGS_FILE", "/etc/transmission-daemon/settings.json"
)
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "/var/lib/transmission-daemon/downloads")
USE_MOCK = os.getenv("USE_MOCK", "false").lower() == "true"

# WireGuard interface that Transmission outbound traffic should be bound to.
# Surfaced in the UI as a tunnel status indicator so a dropped tunnel is
# visible before traffic leaks out the bare ISP link. TUNNEL_IFACE is the
# canonical env var; WG_INTERFACE is accepted for back-compat. No default —
# the interface name is site-specific, so it must come from .env (leaving it
# unset simply hides the tunnel indicator).
TUNNEL_IFACE = os.getenv("TUNNEL_IFACE") or os.getenv("WG_INTERFACE") or ""
# A peer is considered stale if the kernel hasn't seen a handshake in this
# many seconds. WireGuard rekeys roughly every 2 minutes when traffic flows;
# 180s is the conventional dead-peer threshold.
WG_HANDSHAKE_STALE_SEC = int(os.getenv("WG_HANDSHAKE_STALE_SEC", "180"))
# How long a tunnel-status result stays cached before a fresh probe runs.
TUNNEL_CHECK_CACHE_TTL = float(os.getenv("TUNNEL_CHECK_CACHE_TTL", "30"))

# Mullvad VPN account number (16 digits). When set, the torrents page shows
# a days-remaining countdown fetched from the Mullvad API.
MULLVAD_ACCOUNT = os.getenv("MULLVAD_ACCOUNT")

# "Update available" indicator. When the running checkout is behind its git
# upstream the topbar shows a badge (see static/updates.js). Both settings
# are optional; the check degrades to no badge on any error.
UPDATE_CHECK_ENABLED = os.getenv("UPDATE_CHECK_ENABLED", "true").lower() == "true"
# How long an update-check result stays cached before another `git fetch`
# runs. Defaults to 15 minutes so the network hit is rare.
UPDATE_CHECK_CACHE_TTL = float(os.getenv("UPDATE_CHECK_CACHE_TTL", "900"))


# ---------- Flask session secret ----------
#
# Sessions must survive a restart, so the key has to be stable. Priority:
#   1. FLASK_SECRET_KEY from the environment/.env (explicit override)
#   2. a persisted, gitignored .flask_secret file next to this module
#   3. a freshly generated key, written to that file (chmod 600) for reuse
# This means a clean install needs no manual secret — it self-provisions one
# on first run and reuses it thereafter — while an operator who wants to pin
# the key (e.g. to share sessions across hosts) still can via .env.
_FLASK_SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".flask_secret")


def _load_or_create_flask_secret():
    env_key = os.getenv("FLASK_SECRET_KEY")
    if env_key:
        return env_key
    try:
        with open(_FLASK_SECRET_FILE, "r") as f:
            existing = f.read().strip()
        if existing:
            return existing
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        # 0o600 so the secret isn't world-readable. Create exclusively where
        # possible; fall back to a plain write if the file appeared meanwhile.
        fd = os.open(_FLASK_SECRET_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
        os.chmod(_FLASK_SECRET_FILE, 0o600)
    except OSError:
        # Read-only filesystem or similar — fall back to an ephemeral key.
        # Sessions won't survive a restart, but the app still boots.
        pass
    return key


FLASK_SECRET_KEY = _load_or_create_flask_secret()
