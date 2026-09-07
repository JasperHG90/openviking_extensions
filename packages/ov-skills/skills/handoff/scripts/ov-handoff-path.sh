#!/usr/bin/env bash
# ov-handoff-path.sh — work out where a handoff lives in OpenViking.
#
#   ov-handoff-path.sh dir            viking://~/resources/handoffs/<project>
#   ov-handoff-path.sh new <slug>     …/<project>/<UTC stamp>--<slug>.md
#   ov-handoff-path.sh project        <project>
#
# /handoff and /continue must compute the same path or resume finds nothing, so
# this is a script rather than an instruction: the derivation, the timestamp
# format and the filename shape have no judgment in them and must not drift.
#
# An identical copy ships in both skills; a test asserts they stay byte-equal.

set -euo pipefail

ROOT="viking://~/resources/handoffs"

usage() {
  cat <<'EOF'
Usage: ov-handoff-path.sh <command> [slug]
  dir            Print the handoff directory URI for this project
  new <slug>     Print a URI for a new handoff, stamped with the UTC time
  project        Print just the project path
EOF
}

# Lowercase kebab, with runs of anything else collapsed to a single dash.
slugify() {
  printf '%s' "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -e 's#[^a-z0-9]\{1,\}#-#g' -e 's#^-*##' -e 's#-*$##'
}

# Reject anything that is not a plain host/path with at least two segments.
#
# A remote can be a filesystem path (`/srv/git/repo`) or a relative one
# (`../sibling`), and both would otherwise reach the server as a URI it refuses
# outright -- `..` trips its traversal guard, and a leading slash makes an empty
# first segment. Failing over to the directory name beats failing the command.
is_safe_project() {
  case "$1" in
    */*) ;;                 # must have at least one separator
    *) return 1 ;;
  esac
  case "$1" in
    # A leading slash or dot, an empty segment, or a trailing slash. The
    # leading-dot case also covers a path that starts with `.` or `..`.
    /*|.*|*//*|*/) return 1 ;;
    # A `.` or `..` segment anywhere after the first.
    */../*|*/..|*/./*|*/.) return 1 ;;
  esac
  return 0
}

# The repo's remote as a readable nested path: github.com/acme/api.
#
# Every spelling of one repo folds onto one path, so it does not matter whether
# the user cloned over HTTPS or SSH, with a trailing slash, or through a
# non-default port. Userinfo is dropped, which also keeps a token embedded in a
# remote out of the path.
#
# This is deliberately NOT OpenViking's internal peer id. Peers live under the
# read-only viking://~/peers/ subtree and are keyed by a rule with worktree,
# submodule and length-hashing cases; this directory is ours, so it uses a plain
# readable path and never has to stay in step with that rule.
normalize_remote() {
  local raw="$1" body
  body="$(printf '%s' "$raw" | sed -e 's#^[a-zA-Z][a-zA-Z0-9+.-]*://##')"

  if [[ "$raw" == *://* ]]; then
    # A URL: drop userinfo, then a numeric port. Only here -- in scp form
    # `host:1234/repo` the digits are the start of the path, not a port.
    body="$(
      printf '%s' "$body" \
        | sed -e 's#^[^@/]*@##' \
              -e 's#^\([^/:]\{1,\}\):[0-9]\{1,\}/#\1/#'
    )"
  else
    # scp form, `git@host:owner/repo`: drop userinfo, then the single colon
    # that separates host from path.
    body="$(printf '%s' "$body" | sed -e 's#^[^@/]*@##' -e 's#:#/#')"
  fi

  # Trailing slashes come off before .git and again after, so `api.git/` and
  # `api.git` land on the same path rather than on two directories.
  printf '%s' "$body" \
    | sed -e 's#/\{1,\}$##' -e 's#\.git$##' -e 's#/\{1,\}$##' \
    | tr '[:upper:]' '[:lower:]'
}

project_path() {
  local remote normalized root
  remote="$(git remote get-url origin 2>/dev/null || true)"

  if [[ -n "$remote" ]]; then
    normalized="$(normalize_remote "$remote")"
    if is_safe_project "$normalized"; then
      printf '%s\n' "$normalized"
      return
    fi
  fi

  # No repo, no origin, or a remote that is not a shared identity (a
  # filesystem path, a relative clone). Scope by the repository's own root
  # rather than $PWD: /handoff run from the root and /continue run from a
  # subdirectory have to agree, and $PWD does not.
  root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [[ -n "$root" ]] || root="$PWD"
  # Kept under local/ so it can never collide with a real forge path.
  printf 'local/%s\n' "$(slugify "$(basename "$root")")"
}

case "${1:-}" in
  project)
    project_path
    ;;
  dir)
    printf '%s/%s\n' "$ROOT" "$(project_path)"
    ;;
  new)
    slug="$(slugify "${2:-handoff}")"
    # An empty slug would give a filename ending in "--.md".
    [[ -n "$slug" ]] || slug="handoff"
    # UTC, and leading the filename: /continue sorts handoffs by name because
    # glob output carries no modification time.
    printf '%s/%s/%s--%s.md\n' \
      "$ROOT" "$(project_path)" "$(date -u +%Y-%m-%dT%H%M)" "$slug"
    ;;
  -h|--help)
    usage
    ;;
  *)
    usage >&2
    exit 1
    ;;
esac
