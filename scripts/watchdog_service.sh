#!/bin/zsh

set -euo pipefail

ROOT_DIR="/Users/kh/binance"
LABEL="com.binance.bot.watchdog"
SOURCE_PLIST="$ROOT_DIR/deploy/$LABEL.plist"
TARGET_DIR="$HOME/Library/LaunchAgents"
TARGET_PLIST="$TARGET_DIR/$LABEL.plist"
DOMAIN="gui/$(id -u)"

mkdir -p "$TARGET_DIR"
mkdir -p "$ROOT_DIR/logs"

install_service() {
  cp "$SOURCE_PLIST" "$TARGET_PLIST"
  if launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; then
    launchctl enable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
    launchctl kickstart -k "$DOMAIN/$LABEL"
    return
  fi
  launchctl bootstrap "$DOMAIN" "$TARGET_PLIST"
  launchctl enable "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
  launchctl kickstart -k "$DOMAIN/$LABEL"
}

start_service() {
  launchctl kickstart -k "$DOMAIN/$LABEL"
}

stop_service() {
  launchctl bootout "$DOMAIN/$LABEL"
}

status_service() {
  launchctl print "$DOMAIN/$LABEL"
}

uninstall_service() {
  launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true
  rm -f "$TARGET_PLIST"
}

case "${1:-status}" in
  install)
    install_service
    ;;
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    install_service
    ;;
  uninstall)
    uninstall_service
    ;;
  status)
    status_service
    ;;
  *)
    echo "usage: $0 {install|start|stop|restart|status|uninstall}" >&2
    exit 1
    ;;
esac