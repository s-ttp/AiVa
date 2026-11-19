#!/bin/sh
# Writes /config.js (served from /var/run/aiva) from AIVA_GATEWAY_URL and
# AIVA_STORY_MODE at container start. Run by the nginx image's
# /docker-entrypoint.sh before nginx starts.
set -eu

target=/var/run/aiva/config.js
url="${AIVA_GATEWAY_URL:-}"

case "$url" in
  "") echo "$0: AIVA_GATEWAY_URL is not set; the UI will show the gateway as not configured." ;;
  http://* | https://*) ;;
  *) echo "$0: AIVA_GATEWAY_URL must start with http:// or https://" >&2; exit 1 ;;
esac

# Strict allowlist so the value can be embedded in JavaScript without escaping.
case "$url" in
  *[!A-Za-z0-9._~:/%-]*)
    echo "$0: AIVA_GATEWAY_URL may only contain A-Z a-z 0-9 . _ ~ : / % -" >&2
    exit 1
    ;;
esac

# Story mode, the Control Center's story-led visitor screens: off unless the Space sets it to true.
story="${AIVA_STORY_MODE:-false}"
case "$story" in
  true | false) ;;
  *) echo "$0: AIVA_STORY_MODE must be true or false" >&2; exit 1 ;;
esac

printf 'window.__AIVA_CONFIG__ = Object.freeze({ gatewayUrl: "%s", storyMode: %s });\n' "$url" "$story" > "$target"
echo "$0: wrote $target"
