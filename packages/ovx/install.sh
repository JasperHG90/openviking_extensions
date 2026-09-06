#!/usr/bin/env bash
# install.sh — install ovx, a profile launcher for the ov CLI.
#
#   curl -fsSL https://raw.githubusercontent.com/JasperHG90/openviking_postgres/main/packages/ovx/install.sh | bash
#
# Options (after the pipe, pass with `bash -s -- ...`):
#   --to <dir>     Install directory (default: $HOME/.local/bin, or $OVX_BIN)
#   --local <path> Install from a local ovx.sh instead of downloading
#   --url <url>    Download from this URL (default: $OVX_SCRIPT_URL or the
#                  main-branch raw URL on GitHub)
#
# ovx needs python3 (3.11+) to read its TOML config and the `ov` CLI to run.

set -euo pipefail

DEFAULT_URL="https://raw.githubusercontent.com/JasperHG90/openviking_postgres/main/packages/ovx/ovx.sh"
INSTALL_DIR="${OVX_BIN:-$HOME/.local/bin}"
SOURCE_URL="${OVX_SCRIPT_URL:-$DEFAULT_URL}"
LOCAL_PATH=""

usage() {
  cat <<EOF
Usage: install.sh [--to <dir>] [--local <path>] [--url <url>]
  --to <dir>     Install directory (default: \$HOME/.local/bin)
  --local <path> Install from a local ovx.sh instead of downloading
  --url <url>    Download URL (default: the main-branch raw URL)
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --to) INSTALL_DIR="$2"; shift 2 ;;
    --local) LOCAL_PATH="$2"; shift 2 ;;
    --url) SOURCE_URL="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ovx install: unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

if [[ -n "$LOCAL_PATH" ]]; then
  if [[ ! -f "$LOCAL_PATH" ]]; then
    echo "ovx install: local file not found: $LOCAL_PATH" >&2; exit 1
  fi
  cp "$LOCAL_PATH" "$tmp/ovx"
else
  command -v curl >/dev/null 2>&1 || { echo "ovx install: needs 'curl' on PATH" >&2; exit 1; }
  echo "ovx install: downloading $SOURCE_URL" >&2
  if ! curl -fsSL "$SOURCE_URL" -o "$tmp/ovx"; then
    echo "ovx install: download failed" >&2; exit 1
  fi
fi

# Sanity: the payload should be a bash script.
if ! head -n1 "$tmp/ovx" | grep -q 'bash'; then
  echo "ovx install: payload does not look like a bash script" >&2; exit 1
fi

mkdir -p "$INSTALL_DIR"
install -m 0755 "$tmp/ovx" "$INSTALL_DIR/ovx"
echo "ovx install: installed to $INSTALL_DIR/ovx" >&2

# Runtime deps: warn, do not fail the install.
if ! command -v python3 >/dev/null 2>&1; then
  echo "ovx install: WARNING: python3 not found — ovx needs python3.11+" >&2
fi
if ! command -v ov >/dev/null 2>&1; then
  echo "ovx install: note: 'ov' CLI not on PATH — install OpenViking to use ovx" >&2
fi

# Tell the user when the install dir is not on PATH.
case ":$PATH:" in
  *":$INSTALL_DIR:"*) ;;
  *) echo "ovx install: '$INSTALL_DIR' is not on your PATH. Add it:" >&2
     echo "    export PATH=\"$INSTALL_DIR:\$PATH\"" >&2 ;;
esac

echo "ovx install: done. Run 'ovx --help' to get started." >&2
