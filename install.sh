#!/usr/bin/env bash
#
# install.sh - set up the Inkbird IBT-4XS -> Telegram bridge.
#
# Copies the project into the target user's home, builds a venv, installs
# dependencies, writes an env file, and generates a systemd unit with the
# CORRECT username and paths (no placeholders to edit by hand).
#
# Run with sudo:   sudo ./install.sh
# Options:
#   --user NAME    install for this user (default: the sudo caller)
#   --dir  PATH    install directory (default: <user home>/ibbq)
#   --no-service   skip creating/enabling the systemd service
#   -h, --help     show this help
#
set -euo pipefail

# ---- pretty output --------------------------------------------------------
info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  ! \033[0m %s\n' "$*"; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# ---- args -----------------------------------------------------------------
TARGET_USER=""
INSTALL_DIR=""
MAKE_SERVICE=1
while [ $# -gt 0 ]; do
  case "$1" in
    --user) TARGET_USER="${2:-}"; shift 2 ;;
    --dir)  INSTALL_DIR="${2:-}"; shift 2 ;;
    --no-service) MAKE_SERVICE=0; shift ;;
    -h|--help) grep '^#' "$0" | sed 's/^#\s\?//'; exit 0 ;;
    *) die "unknown option: $1 (try --help)" ;;
  esac
done

# ---- must be root (for apt + systemd) -------------------------------------
[ "$(id -u)" -eq 0 ] || die "run me with sudo: sudo ./install.sh"

# ---- resolve the target user (NEVER root - systemd needs a real login) ----
if [ -z "$TARGET_USER" ]; then
  TARGET_USER="${SUDO_USER:-}"
fi
[ -n "$TARGET_USER" ] || die "couldn't determine the target user; pass --user NAME"
[ "$TARGET_USER" != "root" ] || die "refusing to install as root; pass --user NAME"
id "$TARGET_USER" >/dev/null 2>&1 || die "user '$TARGET_USER' does not exist"

TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[ -n "$TARGET_HOME" ] || die "couldn't find home directory for '$TARGET_USER'"
[ -n "$INSTALL_DIR" ] || INSTALL_DIR="$TARGET_HOME/ibbq"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
run_user() { sudo -u "$TARGET_USER" "$@"; }

info "Installing for user '$TARGET_USER' into '$INSTALL_DIR'"

# ---- check the source files are next to this script -----------------------
NEED=(ibbq_telegram.py requirements.txt ibbq.env.example README.md)
for f in "${NEED[@]}"; do
  [ -f "$SCRIPT_DIR/$f" ] || die "missing '$f' next to install.sh"
done

# ---- system dependencies --------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
  info "Installing system packages (bluez, python venv, rfkill)"
  apt-get update -qq
  apt-get install -y -qq bluez python3-venv python3-pip rfkill >/dev/null
  ok "system packages ready"
else
  warn "apt-get not found - install bluez + python3-venv yourself if missing"
fi

# ---- copy project files ---------------------------------------------------
info "Copying files"
install -d -o "$TARGET_USER" -g "$TARGET_USER" "$INSTALL_DIR"
for f in "${NEED[@]}"; do
  install -m 0644 -o "$TARGET_USER" -g "$TARGET_USER" "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
done
chmod 0644 "$INSTALL_DIR/ibbq_telegram.py"
ok "files copied"

# ---- python venv + deps ---------------------------------------------------
info "Creating virtualenv and installing Python deps"
if [ ! -x "$INSTALL_DIR/venv/bin/python" ]; then
  run_user python3 -m venv "$INSTALL_DIR/venv"
fi
run_user "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
run_user "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
ok "venv ready at $INSTALL_DIR/venv"

# ---- env file (don't clobber an existing one) -----------------------------
ENV_FILE="$INSTALL_DIR/ibbq.env"
if [ -f "$ENV_FILE" ]; then
  warn "keeping existing $ENV_FILE"
else
  info "Creating $ENV_FILE"
  cp "$INSTALL_DIR/ibbq.env.example" "$ENV_FILE"
  # point the state file at the install dir
  sed -i "s#^IBBQ_STATE=.*#IBBQ_STATE=$INSTALL_DIR/probe_state.json#" "$ENV_FILE"

  # offer to fill in secrets if we're interactive
  if [ -t 0 ]; then
    read -rp "  Telegram bot token (blank to edit later): " TOKEN || true
    read -rp "  Telegram chat id   (blank to edit later): " CHATID || true
    [ -n "${TOKEN:-}" ]  && sed -i "s#^TELEGRAM_BOT_TOKEN=.*#TELEGRAM_BOT_TOKEN=$TOKEN#" "$ENV_FILE"
    [ -n "${CHATID:-}" ] && sed -i "s#^TELEGRAM_CHAT_ID=.*#TELEGRAM_CHAT_ID=$CHATID#" "$ENV_FILE"
  fi
  chown "$TARGET_USER:$TARGET_USER" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  ok "env file created (chmod 600)"
fi

# ---- bluetooth: unblock, auto-enable on boot, group access ----------------
info "Configuring Bluetooth"
if command -v rfkill >/dev/null 2>&1; then
  rfkill unblock bluetooth || true
fi
BT_CONF=/etc/bluetooth/main.conf
if [ -f "$BT_CONF" ]; then
  if grep -qiE '^\s*#?\s*AutoEnable' "$BT_CONF"; then
    sed -i -E 's/^\s*#?\s*AutoEnable\s*=.*/AutoEnable=true/I' "$BT_CONF"
  elif grep -qi '^\[Policy\]' "$BT_CONF"; then
    sed -i '/^\[Policy\]/a AutoEnable=true' "$BT_CONF"
  else
    printf '\n[Policy]\nAutoEnable=true\n' >> "$BT_CONF"
  fi
fi
# let the service user reach the adapter over D-Bus without setcap
if getent group bluetooth >/dev/null 2>&1; then
  usermod -aG bluetooth "$TARGET_USER" || true
fi
systemctl restart bluetooth || warn "couldn't restart bluetooth service"
if command -v bluetoothctl >/dev/null 2>&1; then
  bluetoothctl power on >/dev/null 2>&1 || true
fi
ok "bluetooth configured (AutoEnable=true, soft-block cleared)"

# ---- systemd unit (generated with real paths - no placeholders) -----------
if [ "$MAKE_SERVICE" -eq 1 ]; then
  info "Writing systemd unit"
  UNIT=/etc/systemd/system/ibbq-telegram.service
  cat > "$UNIT" <<EOF
[Unit]
Description=Inkbird IBT-4XS -> Telegram bridge
After=bluetooth.target network-online.target
Wants=bluetooth.target network-online.target

[Service]
Type=simple
User=$TARGET_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/ibbq_telegram.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable ibbq-telegram >/dev/null 2>&1 || true
  ok "unit installed and enabled at boot"

  # only auto-start if the token looks filled in
  if grep -q '^TELEGRAM_BOT_TOKEN=123456789:' "$ENV_FILE" || \
     ! grep -q '^TELEGRAM_BOT_TOKEN=.\+' "$ENV_FILE"; then
    warn "bot token not set yet - not starting the service"
    NEEDS_TOKEN=1
  else
    systemctl restart ibbq-telegram
    ok "service started"
  fi
fi

# ---- final instructions ---------------------------------------------------
echo
info "Done."
if [ "${NEEDS_TOKEN:-0}" -eq 1 ]; then
  cat <<EOF
Next steps:
  1. Edit your secrets:   nano $ENV_FILE
  2. Start it:            sudo systemctl start ibbq-telegram
  3. Watch the logs:      journalctl -u ibbq-telegram -f
EOF
else
  cat <<EOF
Watch it work:   journalctl -u ibbq-telegram -f
Then power on the thermometer and send /status to your bot.
EOF
fi
echo "Config lives in: $INSTALL_DIR"