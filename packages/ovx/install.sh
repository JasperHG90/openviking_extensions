#!/usr/bin/env bash
# install.sh — install ovx, a profile launcher for the ov CLI.
#
#   curl -fsSL https://raw.githubusercontent.com/JasperHG90/openviking_extensions/main/packages/ovx/install.sh | bash
#
# Options (after the pipe, pass with `bash -s -- ...`):
#   --to <dir>      Install directory (default: $HOME/.local/bin, or $OVX_BIN)
#   --version <v>   Install this released version, e.g. 0.1.0
#   --main          Install the tip of main; it reports its version as "dev"
#   --local <path>  Install from a local ovx.sh instead of downloading
#   --url <url>     Download from this URL
#
# With none of those, the newest published ovx release is installed. Only a
# release carries a real version: the script in the repository says "dev",
# because the version is stamped in at release time and lives in the git tag.
#
# --version, --main, --url and --local all choose the payload; the last one
# given wins.
#
# ovx needs python3 (3.11+) to read its TOML config and the `ov` CLI to run.

set -euo pipefail

REPO="JasperHG90/openviking_extensions"
TAG_PREFIX="ovx-v"
MAIN_URL="https://raw.githubusercontent.com/$REPO/main/packages/ovx/ovx.sh"
# A release stamps its own version here, so the installer attached to a release
# installs that release without reaching for the API. Empty in the repository.
PINNED_VERSION=""
INSTALL_DIR="${OVX_BIN:-$HOME/.local/bin}"
SOURCE_URL="${OVX_SCRIPT_URL:-}"
LOCAL_PATH=""

usage() {
  cat <<EOF
Usage: install.sh [--to <dir>] [--version <v>] [--main] [--local <path>] [--url <url>]
  --to <dir>      Install directory (default: \$HOME/.local/bin)
  --version <v>   Install this released version, e.g. 0.1.0
  --main          Install the tip of main; it reports its version as "dev"
  --local <path>  Install from a local ovx.sh instead of downloading
  --url <url>     Download URL

With none of these, the newest published release is installed.
EOF
}

asset_url() {
  printf 'https://github.com/%s/releases/download/%s%s/ovx.sh' "$REPO" "$TAG_PREFIX" "$1"
}

# Newest published stable ovx version, or empty. The repository holds several
# packages, so /releases/latest is no use here -- it could name an
# ov-postgres release. Filter the list by tag prefix instead; the API returns
# it newest first. No jq: this runs on whatever machine curls it.
#
# Prereleases are skipped. The list endpoint returns them alongside stable
# releases, so marking a release "pre-release" on GitHub does nothing here on
# its own -- the flag has to be read. A beta is opt-in through --version, not
# something a bare install hands someone who asked for no version at all.
#
# awk rather than grep, because the decision needs two fields from the same
# release: `tag_name` and `prerelease`. Splitting on commas puts each field on
# its own line, and `tag_name` always precedes `prerelease` within a release
# object, so one pass can hold the tag until the flag arrives.
latest_version() {
  curl -fsSL "https://api.github.com/repos/$REPO/releases?per_page=100" 2>/dev/null \
    | tr ',' '\n' \
    | awk -F'"' -v prefix="$TAG_PREFIX" '
        $2 == "tag_name"   { tag = $4; next }
        $2 == "prerelease" {
          if ($0 !~ /true/ && index(tag, prefix) == 1) {
            print substr(tag, length(prefix) + 1)
            exit
          }
        }
      '
}

# Every option here takes a value. Without this check a trailing `--version`
# would expand to nothing, `shift 2` would fail, and set -e would exit 1 with
# no message at all on bash 3.2.
need_arg() {
  if [[ "$2" -lt 2 ]]; then
    echo "ovx install: $1 needs a value" >&2
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --to) need_arg "$1" "$#"; INSTALL_DIR="$2"; shift 2 ;;
    # Tolerate both "0.1.0" and "v0.1.0"; the tag carries the package name.
    --version) need_arg "$1" "$#"
               SOURCE_URL="$(asset_url "${2#v}")"; LOCAL_PATH=""; shift 2 ;;
    --main) SOURCE_URL="$MAIN_URL"; LOCAL_PATH=""; shift ;;
    --local) need_arg "$1" "$#"; LOCAL_PATH="$2"; shift 2 ;;
    --url) need_arg "$1" "$#"; SOURCE_URL="$2"; LOCAL_PATH=""; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ovx install: unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

# Nothing chose a payload, so install the newest release. Installing main by
# default would hand out a script reporting "dev", which is what a version
# flag exists to avoid.
if [[ -z "$SOURCE_URL" ]] && [[ -z "$LOCAL_PATH" ]]; then
  command -v curl >/dev/null 2>&1 || { echo "ovx install: needs 'curl' on PATH" >&2; exit 1; }
  version="$PINNED_VERSION"
  if [[ -z "$version" ]]; then
    echo "ovx install: looking up the newest release" >&2
    version="$(latest_version || true)"
  fi
  if [[ -z "$version" ]]; then
    echo "ovx install: found no published $TAG_PREFIX* release." >&2
    echo "             Pass --version <v>, or --main for the unreleased tip." >&2
    exit 1
  fi
  SOURCE_URL="$(asset_url "$version")"
fi

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
# Report what actually landed, so a --version install that fetched something
# other than what was asked for is visible now rather than days later. Ask the
# installed script rather than assuming: this is the only check that the
# payload is the version requested.
installed="$("$INSTALL_DIR/ovx" -V 2>/dev/null || true)"
if [[ -z "$installed" ]]; then
  echo "ovx install: WARNING: the installed script did not report a version" >&2
  installed="ovx (version unknown)"
fi
echo "ovx install: installed $installed to $INSTALL_DIR/ovx" >&2

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
