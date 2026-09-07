#!/usr/bin/env bash
# install.sh — install the ov-skills OpenViking skills.
#
#   curl -fsSL https://raw.githubusercontent.com/JasperHG90/openviking_extensions/main/packages/ov-skills/install.sh | bash
#
# Options (after the pipe, pass with `bash -s -- ...`):
#   --target <h>    Harness to install for: claude, opencode, or hermes.
#                   Repeatable. Default: every harness found on this machine.
#   --to <dir>      Install into this directory instead of a harness default.
#   --category <c>  Hermes groups skills in subdirectories (default: openviking).
#   --version <v>   Install this released version, e.g. 0.1.0
#   --main          Install from the tip of main
#   --local <path>  Install from a local checkout's packages/ov-skills
#   --list          Print what would be installed where, then exit
#
# With no --version, --main or --local, the newest published release is used.
#
# The skills need no runtime of their own. They call OpenViking through
# whatever MCP connection the harness already has.

set -euo pipefail

REPO="JasperHG90/openviking_extensions"
TAG_PREFIX="ov-skills-v"
# A release stamps its own version here, so the installer attached to a release
# installs that release without reaching for the API. Empty in the repository.
PINNED_VERSION=""

# The skills this package owns. The installer replaces exactly these
# directories and never touches anything else in a destination, so a skills
# directory shared with hand-written or third-party skills stays intact.
SKILLS="handoff continue learnings ingest"

# A literal newline, for joining paths that may themselves contain spaces.
NL='
'

TARGETS=""
EXPLICIT_DIR=""
HERMES_CATEGORY="openviking"
SOURCE_URL=""
LOCAL_PATH=""
LIST_ONLY=0

usage() {
  cat <<EOF
Usage: install.sh [--target claude|opencode|hermes] [--to <dir>] [--category <c>]
                  [--version <v>] [--main] [--local <path>] [--list]

  --target <h>    Harness to install for; repeatable. Default: all detected.
  --to <dir>      Install into this directory instead of a harness default.
  --category <c>  Hermes skill category directory (default: openviking).
  --version <v>   Install this released version, e.g. 0.1.0
  --main          Install from the tip of main
  --local <path>  Install from a local checkout's packages/ov-skills
  --list          Print what would be installed where, then exit
EOF
}

die() { echo "ov-skills install: $*" >&2; exit 1; }

need_arg() {
  if [[ "$2" -lt 2 ]]; then
    die "$1 needs a value"
  fi
}

# Where each harness looks for skills.
#
# claude covers two harnesses at once: opencode scans ~/.claude/skills as well
# as its own directory, so a Claude Code install is already visible there. The
# separate opencode target exists for someone who does not use Claude Code and
# would rather not have a ~/.claude at all.
target_dir() {
  case "$1" in
    claude)   printf '%s/.claude/skills' "$HOME" ;;
    opencode) printf '%s/.config/opencode/skills' "$HOME" ;;
    hermes)   printf '%s/.hermes/skills/%s' "$HOME" "$HERMES_CATEGORY" ;;
    *) die "unknown target: $1 (want claude, opencode, or hermes)" ;;
  esac
}

# A harness counts as present when its own config directory exists. Checking
# for the skills directory instead would find nothing on a fresh install, and
# checking PATH would miss opencode, which installs outside it.
detect_targets() {
  local found=""
  [[ -d "$HOME/.claude" ]] && found="$found claude"
  [[ -d "$HOME/.config/opencode" ]] && found="$found opencode"
  [[ -d "$HOME/.hermes" ]] && found="$found hermes"
  printf '%s' "${found# }"
}

asset_url() {
  printf 'https://github.com/%s/releases/download/%s%s/ov-skills.tar.gz' \
    "$REPO" "$TAG_PREFIX" "$1"
}

# Newest published stable ov-skills version, or empty. The repository holds
# several packages, so /releases/latest is no use -- it could name an ovx
# release. Filter the list by tag prefix instead. No jq: this runs on whatever
# machine curls it.
#
# Every match is collected and sorted, rather than taking the first the API
# returns: the list endpoint is ordered by creation date, not by version, and
# this repo's own releases already come back out of order. Taking the first
# would hand out an older release the moment two are created out of sequence.
#
# Prereleases are skipped, so a beta is opt-in through --version and never
# something a bare install hands someone who asked for no version at all.
latest_version() {
  curl -fsSL "https://api.github.com/repos/$REPO/releases?per_page=100" 2>/dev/null \
    | tr ',' '\n' \
    | awk -F'"' -v prefix="$TAG_PREFIX" '
        $2 == "tag_name"   { tag = $4; next }
        $2 == "prerelease" {
          if ($0 !~ /true/ && index(tag, prefix) == 1) {
            print substr(tag, length(prefix) + 1)
          }
        }
      ' \
    | sort -V \
    | tail -n1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target) need_arg "$1" "$#"; TARGETS="$TARGETS $2"; shift 2 ;;
    --to) need_arg "$1" "$#"; EXPLICIT_DIR="$2"; shift 2 ;;
    --category) need_arg "$1" "$#"; HERMES_CATEGORY="$2"; shift 2 ;;
    # Tolerate both "0.1.0" and "v0.1.0"; the tag carries the package name.
    --version) need_arg "$1" "$#"
               SOURCE_URL="$(asset_url "${2#v}")"; LOCAL_PATH=""; shift 2 ;;
    --main) SOURCE_URL="main"; LOCAL_PATH=""; shift ;;
    --local) need_arg "$1" "$#"; LOCAL_PATH="$2"; SOURCE_URL=""; shift 2 ;;
    --list) LIST_ONLY=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ov-skills install: unknown option: $1" >&2; usage >&2; exit 1 ;;
  esac
done

# --to names one directory, so it cannot also fan out across harnesses.
if [[ -n "$EXPLICIT_DIR" ]] && [[ -n "$TARGETS" ]]; then
  die "--to and --target are mutually exclusive"
fi

if [[ -z "$EXPLICIT_DIR" ]] && [[ -z "$TARGETS" ]]; then
  TARGETS="$(detect_targets)"
  if [[ -z "$TARGETS" ]]; then
    echo "ov-skills install: found no harness on this machine." >&2
    echo "             Looked for ~/.claude, ~/.config/opencode, ~/.hermes." >&2
    echo "             Pass --target <claude|opencode|hermes>, or --to <dir>." >&2
    exit 1
  fi
  echo "ov-skills install: detected$(printf ' %s' "$TARGETS")" >&2
fi

# Resolve destinations before fetching anything, so a bad --target fails before
# it costs a download.
# Newline-separated, not space-separated: $HOME may contain a space, and these
# are paths.
DESTS=""
if [[ -n "$EXPLICIT_DIR" ]]; then
  DESTS="$EXPLICIT_DIR"
else
  # Word splitting is the point here; harness names never contain spaces.
  # shellcheck disable=SC2086
  for t in $TARGETS; do
    d="$(target_dir "$t")"
    if [[ -z "$DESTS" ]]; then DESTS="$d"; else DESTS="$DESTS${NL}$d"; fi
  done
fi

if [[ "$LIST_ONLY" -eq 1 ]]; then
  echo "ov-skills install: would install$(printf ' %s' "$SKILLS") into:" >&2
  printf '%s\n' "$DESTS" | while IFS= read -r d; do echo "  $d" >&2; done
  exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# Assemble the payload at $tmp/skills, whatever the source.
if [[ -n "$LOCAL_PATH" ]]; then
  src="$LOCAL_PATH"
  # Accept either the package directory or its skills/ directory.
  [[ -d "$src/skills" ]] && src="$src/skills"
  [[ -d "$src" ]] || die "local path not found: $LOCAL_PATH"
  cp -R "$src" "$tmp/skills"
else
  command -v curl >/dev/null 2>&1 || die "needs 'curl' on PATH"
  command -v tar >/dev/null 2>&1 || die "needs 'tar' on PATH"

  if [[ "$SOURCE_URL" = "main" ]]; then
    echo "ov-skills install: downloading the tip of main" >&2
    # The repo tarball, of which only the skills subdirectory is kept. GitHub
    # wraps it in a branch-named top directory: stripping three components
    # turns <repo>-main/packages/ov-skills/skills/... into skills/...
    #
    # GNU tar needs --wildcards to treat the pattern as a glob; bsdtar, which
    # macOS ships, does that by default and rejects the flag. So it is added
    # only where it is required.
    tar_opts=(-xz -C "$tmp" --strip-components=3)
    if tar --version 2>/dev/null | grep -qi 'gnu tar'; then
      tar_opts+=(--wildcards)
    fi
    curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/heads/main" \
      | tar "${tar_opts[@]}" '*/packages/ov-skills/skills' \
      || die "could not fetch the skills from main"
  else
    if [[ -z "$SOURCE_URL" ]]; then
      version="$PINNED_VERSION"
      if [[ -z "$version" ]]; then
        echo "ov-skills install: looking up the newest release" >&2
        version="$(latest_version || true)"
      fi
      if [[ -z "$version" ]]; then
        echo "ov-skills install: found no published $TAG_PREFIX* release." >&2
        echo "             Pass --version <v>, or --main for the unreleased tip." >&2
        exit 1
      fi
      SOURCE_URL="$(asset_url "$version")"
    fi
    echo "ov-skills install: downloading $SOURCE_URL" >&2
    curl -fsSL "$SOURCE_URL" | tar -xz -C "$tmp" \
      || die "download failed"
  fi
fi

[[ -d "$tmp/skills" ]] || die "payload has no skills/ directory"

# Sanity: every skill this installer claims to ship must actually be present,
# so a truncated or wrong-shaped payload fails here rather than half-installing.
# shellcheck disable=SC2086
for s in $SKILLS; do
  [[ -f "$tmp/skills/$s/SKILL.md" ]] || die "payload is missing skills/$s/SKILL.md"
done

# /handoff and /continue compute their storage path with a shipped script
# rather than from prose, so a payload that lost it leaves both skills unable
# to agree on where handoffs live. Check for it, and restore the mode bit: a
# tarball unpacked under a restrictive umask can arrive without it.
for s in handoff continue; do
  helper="$tmp/skills/$s/scripts/ov-handoff-path.sh"
  [[ -f "$helper" ]] || die "payload is missing skills/$s/scripts/ov-handoff-path.sh"
  chmod +x "$helper"
done

printf '%s\n' "$DESTS" | while IFS= read -r dest; do
  [[ -n "$dest" ]] || continue
  mkdir -p "$dest"
  for s in $SKILLS; do
    # Replace rather than merge: a stale file left from an older version would
    # otherwise linger inside the skill directory.
    rm -rf "${dest:?}/$s"
    cp -R "$tmp/skills/$s" "$dest/$s"
  done
  echo "ov-skills install: installed$(printf ' %s' "$SKILLS") to $dest" >&2
done

echo "ov-skills install: done. Restart your agent, then try /handoff." >&2
