#!/usr/bin/env bash
# Install Ashvale Station onto a Raspberry Pi that is already running.
#
# Most people already have a working Pi and should not have to reflash a card to
# try this. The prebuilt image exists for a fresh board; this exists for
# everything else, and it is the same install the image performs.
#
#   curl -fsSL https://raw.githubusercontent.com/lynchaos/ashvale-station/main/deploy/install.sh | bash
#
# Idempotent: safe to re-run to upgrade. It never touches data/ or config.yaml
# on a machine that already has them, because those are your history and your
# coordinates and neither can be regenerated.
set -euo pipefail

REPO="${ASHVALE_REPO:-https://github.com/lynchaos/ashvale-station.git}"
DEST="${ASHVALE_DEST:-/opt/ashvale}"
BRANCH="${ASHVALE_BRANCH:-main}"
SERVICE="${ASHVALE_SERVICE:-ashvale}"
# Overridable so a second instance can be installed alongside a live one,
# which is also the only way to test this script without stopping the real
# station. Empty means "whatever config.yaml says".
PORT_OVERRIDE="${ASHVALE_PORT:-}"  # overridable so the installer can be tested without clobbering a live unit

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m x\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run with sudo: curl ... | sudo bash"

if ! grep -qi raspberry /proc/device-tree/model 2>/dev/null; then
  warn "this does not look like a Raspberry Pi; continuing anyway"
fi

RUN_USER="${SUDO_USER:-$(getent passwd 1000 | cut -d: -f1)}"
[ -n "$RUN_USER" ] || die "could not determine a non-root user to run the service as"

say "Installing system packages"
# Hardware libraries come from apt, never pip. sense-hat pulls in RTIMULib, and
# building that inside a clean venv on ARM is a genuine ordeal.
apt-get update -qq
apt-get install -y --no-install-recommends \
  git python3-venv python3-numpy python3-smbus2 sense-hat sqlite3

say "Fetching the application into $DEST"
if [ -d "$DEST/.git" ]; then
  git -C "$DEST" fetch --depth 1 origin "$BRANCH"
  git -C "$DEST" reset --hard "origin/$BRANCH"
else
  install -d "$DEST"
  git clone --depth 1 --branch "$BRANCH" "$REPO" "$DEST"
fi

say "Creating the virtual environment"
# --system-site-packages so numpy and the Sense HAT stack come from apt rather
# than being compiled here. Plain uvicorn, never uvicorn[standard]: that extra
# drags in watchfiles and uvloop, which compile Rust and C from source on ARM
# for features this does not use.
[ -d "$DEST/.venv" ] || python3 -m venv --system-site-packages "$DEST/.venv"
"$DEST/.venv/bin/pip" install -q --upgrade pip
"$DEST/.venv/bin/pip" install -q --no-cache-dir -r "$DEST/requirements.txt"

install -d -o "$RUN_USER" -g "$RUN_USER" "$DEST/data" "$DEST/data/state"
chown -R "$RUN_USER":"$RUN_USER" "$DEST"

say "Enabling I2C for the Sense HAT"
CFG=/boot/firmware/config.txt
[ -f "$CFG" ] || CFG=/boot/config.txt
if [ -f "$CFG" ] && ! grep -q '^dtparam=i2c_arm=on' "$CFG"; then
  printf '\n# --- Ashvale Station ---\ndtparam=i2c_arm=on\n' >> "$CFG"
  warn "I2C enabled: reboot before the Sense HAT is detected"
fi

say "Installing the service"
cat > "/etc/systemd/system/$SERVICE.service" <<UNIT
[Unit]
Description=Ashvale Station forecast service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$DEST
${PORT_OVERRIDE:+Environment=ASHVALE_SERVER__PORT=$PORT_OVERRIDE}
ExecStart=$DEST/.venv/bin/python run.py
Restart=always
RestartSec=10

# A Zero 2 W has 512 MB. Cap the service so a runaway allocation takes the
# service down instead of the whole board.
MemoryMax=280M
CPUWeight=70
Nice=5

StandardOutput=journal
StandardError=journal
SyslogIdentifier=ashvale

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"

# Poll the endpoint, not systemd. "active" is true for the instant between
# exec and the first failed bind, so a unit that is crash-looping on a port
# clash reports healthy and the installer congratulates you on a broken install.
# Asking the thing whether it answers is the only check that means anything.
PORT="$PORT_OVERRIDE"
[ -n "$PORT" ] || PORT="$(sed -n 's/^ *port: *\([0-9]\+\).*/\1/p' "$DEST/config.yaml" | head -1)"
PORT="${PORT:-8000}"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null --max-time 2 "http://127.0.0.1:${PORT}/api/status"; then
    say "Running: http://${IP:-<this-pi>}:${PORT}"
    HEALTHY=1; break
  fi
  sleep 2
done
if [ "${HEALTHY:-0}" != 1 ]; then
  systemctl is-active --quiet "$SERVICE" \
    && warn "unit is up but nothing is answering on port ${PORT}; is it already in use?" \
    || warn "unit is not running"
  die "install finished but the station is not serving: journalctl -u $SERVICE -n 40"
fi

cat <<NOTE

  Three things worth doing, in order of how much they matter:

   1. Settings tab: set your latitude, longitude and altitude. Altitude feeds
      the sea-level pressure reduction on every row, and pressure tendency is
      what drives the precipitation forecast.
   2. Models and Calibration tab: put a thermometer next to the board and enter
      the reading. The Sense HAT sits above a SoC running 20 C hotter than the
      room; one reading fixes the bias on every forecast that follows.
   3. Settings tab: say whether it is indoors, and tell it when you open a
      window or turn the heating on. Those are regime changes and the models
      carry about 55 hours of memory.

  Forecasts need about 10 hours of history before the first training pass.

  No authentication and no TLS: trusted LAN only, do not port-forward it.

NOTE
