#!/bin/sh
set -eu

LOGIN="${1:-${MT5_LOGIN:-}}"
if [ -z "$LOGIN" ]; then
    echo "usage: run-mt5.sh <mt5-login>" >&2
    exit 2
fi
case "$LOGIN" in
    *[!0-9]*)
        echo "run-mt5.sh: login must be numeric" >&2
        exit 2
        ;;
esac

DIR="/opt/guardrails/mt5/$LOGIN"
if [ ! -x "$DIR/terminal64.exe" ]; then
    echo "run-mt5.sh: terminal not found at $DIR/terminal64.exe" >&2
    exit 1
fi

exec /opt/wine-staging/bin/wine "$DIR/terminal64.exe" /portable \
    "/config:Z:\\opt\\guardrails\\mt5\\$LOGIN\\start.ini"
