#!/usr/bin/env bash
# ovx — run ov against a profile, without leaving an API key on disk.
#
# ov reads its client settings from ovcli.conf, normally ~/.openviking/ovcli.conf,
# where the API key sits in plain text for as long as the file exists. ovx keeps
# the key out of that file: a profile holds a $VAR reference, ovx expands it from
# the environment at launch, writes the result to a private temporary file, points
# $OPENVIKING_CLI_CONFIG_FILE at it, runs ov, and deletes the file on the way out.
#
# Usage: ovx [PROFILE] [ov args...]
#
# Options:
#   -l, --list       List configured profiles and exit
#   -n, --new        Run the create-profile wizard, then run ov with it
#   -e, --edit [P]   Edit profile P (or pick one) in the wizard, then run ov
#   -d, --delete [P] Delete profile P (or pick one), after confirmation
#   -L, --login [P]  Log in to profile P through Studio, store the token, exit
#       --logout [P] Revoke and remove profile P's stored login, then exit
#   -V, --version    Show the ovx version and exit
#   -h, --help       Show this help
#
# Behavior:
#   ovx                    Interactive: pick a profile, create a new one, or edit
#   ovx lab                Run bare 'ov' with profile 'lab'
#   ovx lab find "query"   Run 'ov find "query"' with profile 'lab'
#   ovx -e lab             Edit profile 'lab' (pre-filled), then run ov
#   ovx -e                 Pick a profile to edit
#   ovx -d lab             Delete profile 'lab', after confirmation
#   ovx -L lab             Log in to 'lab'; approve in Studio, token is stored
#   ovx --logout lab       Revoke and forget 'lab's stored login
#   ovx -- -o json status  Pick a profile, forward '-o json status' to ov
#
# Logging in:
#   'ovx -L lab' runs OpenViking's OAuth flow. It prints a six-character code
#   and a Studio URL; open that URL where you are already signed in, enter the
#   code, and ovx stores the resulting token under ~/.ovx/tokens/lab.json at
#   mode 600. Later runs use it and refresh it automatically, so a profile that
#   has logged in needs no api_key at all.
#
#   The stored token outranks the profile's api_key. It is short-lived,
#   refreshable, and revocable on the server, which a static key is not —
#   but it is still a credential on disk, unlike a $VAR reference.
#
# Everything after the profile name goes to ov untouched, so ov's own
# subcommands and flags need no escaping: 'ovx lab -o json status' works as
# written. A '--' is only needed when no profile name comes first, as in
# 'ovx -- -o json status', where ovx would otherwise read '-o' as its own.
# A '--' straight after the profile name is allowed and dropped, so
# 'ovx lab -- -o json status' passes '-o json status' to ov as well.
#
# Config: ~/.ovx/config.toml  (override with $OVX_CONFIG_FILE or $OVX_DIR)
# Format: TOML, one [profile] table per OpenViking instance. Keys are the keys
# of ovcli.conf:
#
#   [lab]
#   url = "https://openviking.example.com"
#   api_key = "$OV_LAB_API_KEY"   # expanded from env at launch
#   account = "acme"
#   user = "jasper"
#
# 'url' is required. A value containing $VAR or ${VAR} is expanded from your
# environment at launch; an unresolved reference is an error, so a missing
# secret fails loudly instead of running against a half-built config.

set -euo pipefail

# Stamped with the release version when release.yaml packages this script for
# a GitHub release. A checkout or a copy taken straight from main reads "dev",
# which is the honest answer: the version lives in the git tag, and an
# unreleased working copy has no tag to claim.
OVX_VERSION="dev"
OVX_HOMEPAGE="https://github.com/JasperHG90/openviking_extensions/tree/main/packages/ovx"

OVX_DIR="${OVX_DIR:-$HOME/.ovx}"
CONFIG_FILE="${OVX_CONFIG_FILE:-$OVX_DIR/config.toml}"
# OAuth tokens live beside the config, one file per profile. A profile that
# has logged in needs no api_key at all: the stored token replaces it.
TOKEN_DIR="$OVX_DIR/tokens"

# Every field ovcli.conf accepts, with the JSON type ovx writes for it:
# str = string, num = number, bool = true/false, map = table.
#
# The list mirrors OVCLIConfig in openviking_cli/utils/config/ovcli_config.py.
# That reader sets extra="forbid", so a field absent here is one it would
# reject anyway; the Rust CLI is laxer and ignores what it does not know,
# which is worse — a typo'd key silently does nothing. Checking against this
# list turns both cases into one error naming the offending key.
FIELD_SPEC='url:str
api_key:str
root_api_key:str
account:str
user:str
actor_peer_id:str
agent_id:str
timeout:num
profile:bool
output:str
echo_command:bool
show_progress:bool
verbose:bool
upload:map
extra_headers:map
extra_header:map
gateway_token:str
plugin:map
auth_mode:str
ldap_username:str
ldap_password:str'

# The fields the wizard prompts for, in order. A profile may carry any field
# from FIELD_SPEC — the rest are for hand-editing, and an edit leaves them
# untouched rather than dropping what it cannot prompt for.
WIZARD_FIELDS='url api_key account user'

PROFILE=""
NEW=0
EDIT=0
DELETE=0
LIST=0
LOGIN=0
LOGOUT=0
OV_ARGS=()

# Set once a temporary config exists, so the exit trap knows what to remove.
TMPROOT=""

# Print the comment block at the top of this file as the help text: the
# documentation a reader sees on opening the script IS the documentation
# --help prints, so the two cannot drift apart. Everything from line 2 to the
# first blank line, with the leading "# " stripped.
#
# Keeping the text in a comment rather than a heredoc also means no
# backslash-escaping: $VAR and ${VAR} appear in the help exactly as they must
# be typed into a config file.
usage() {
  local body=""
  # $0 is not always this file: `bash < ovx.sh` makes it "bash", and a stripped
  # copy may not be readable. awk is POSIX and effectively always present, but
  # --help runs before need(), so check rather than fail with "awk: not found".
  if [[ -r "$0" ]] && command -v awk >/dev/null 2>&1; then
    body="$(awk 'NR == 1 { next }                       # skip the shebang
                 /^#/    { sub(/^# ?/, ""); print; next }
                 { exit }' "$0")"
  fi
  if [[ -z "$body" ]]; then
    body="ovx — run ov against a profile, without leaving an API key on disk.

Usage: ovx [PROFILE] [ov args...]

The full help is the comment block at the top of ovx.sh, which could not be
read here. See $OVX_HOMEPAGE"
  fi
  # One write, not three: `ovx --help | head` closes the pipe early, and each
  # separate write past that point earns its own "write error: Broken pipe".
  # The header documents the default config path; this reports the one in force.
  printf '%s\n\nConfig in use: %s\n' "$body" "$CONFIG_FILE"
}

need() {
  command -v "$1" >/dev/null 2>&1 \
    || { echo "ovx: required command not found: $1" >&2; exit 1; }
}

trim() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

# Refuse cleanly when there is no controlling terminal. The wizard and the
# profile menus read from /dev/tty; without one, every read fails and the
# script would crash with cryptic unbound-variable errors instead of a
# message. Direct `ovx <profile>` does not call this — it runs headless.
require_tty() {
  if ! { : </dev/tty ; } 2>/dev/null; then
    echo "ovx: this action needs an interactive terminal (no controlling tty)." >&2
    exit 1
  fi
}

ensure_config_dir() {
  mkdir -p "$OVX_DIR"
  chmod 700 "$OVX_DIR" 2>/dev/null || true
}

# Remove the temporary config directory. Idempotent: the exit trap and a
# signal trap can both reach it.
#
# shellcheck disable=SC2317  # reached through the traps below, not by a call
cleanup() {
  if [[ -n "$TMPROOT" ]] && [[ -d "$TMPROOT" ]]; then
    rm -rf "$TMPROOT"
  fi
}

# Clean up on the way out, however we get there.
#
# An EXIT trap alone is not enough. On bash 3.2 — what macOS ships, so what
# most people run this under — a SIGINT arriving while ov holds the foreground
# sometimes kills the shell WITHOUT running the EXIT trap, leaving the
# materialized key on disk. It is a few percent of interrupts under load, not a
# rarity worth ignoring. Naming each signal installs a real handler, so the
# signal is caught rather than fatal and cleanup always runs.
#
# Each handler re-raises the signal after cleaning up, so ovx still dies from
# it and a caller can tell an interrupt from a failure. SIGKILL cannot be
# trapped: that one leaks the file, and nothing can prevent it.
#
# shellcheck disable=SC2317  # reached through the traps below, not by a call
on_signal() {
  cleanup
  trap - "$1"
  kill -"$1" $$
}
trap cleanup EXIT
trap 'on_signal INT' INT
trap 'on_signal TERM' TERM
trap 'on_signal HUP' HUP

# Emit existing profile names, one per line. No output (exit 0) if the file
# is missing or has no profiles; exit 2 on a parse error.
profile_names() {
  CONFIG_FILE="$CONFIG_FILE" python3 <<'PY'
import os, sys
try:
    import tomllib
except ModuleNotFoundError:
    sys.stderr.write("ovx: requires Python 3.11+ (tomllib) to read TOML\n")
    sys.exit(1)
path = os.environ["CONFIG_FILE"]
if not os.path.isfile(path):
    sys.exit(0)
try:
    with open(path, "rb") as f:
        data = tomllib.load(f)
except tomllib.TOMLDecodeError as e:
    sys.stderr.write(f"ovx: failed to parse {path}: {e}\n")
    sys.exit(2)
except OSError as e:
    sys.stderr.write(f"ovx: cannot read {path}: {e}\n")
    sys.exit(1)
for n in data:
    if isinstance(data[n], dict):
        print(n)
PY
}

list_profiles() {
  CONFIG_FILE="$CONFIG_FILE" python3 <<'PY'
import os, sys
try:
    import tomllib
except ModuleNotFoundError:
    sys.stderr.write("ovx: requires Python 3.11+ (tomllib) to read TOML\n")
    sys.exit(1)
path = os.environ["CONFIG_FILE"]
if not os.path.isfile(path):
    print(f"ovx: no config at {path}")
    sys.exit(0)
try:
    with open(path, "rb") as f:
        data = tomllib.load(f)
except tomllib.TOMLDecodeError as e:
    sys.stderr.write(f"ovx: failed to parse {path}: {e}\n")
    sys.exit(2)
except OSError as e:
    sys.stderr.write(f"ovx: cannot read {path}: {e}\n")
    sys.exit(1)
names = [n for n in data if isinstance(data[n], dict)]
if not names:
    print(f"ovx: no profiles defined in {path}")
    sys.exit(0)
print(f"Profiles in {path}:")
for n in names:
    print(f"  [{n}]  {data[n].get('url', '')}")
PY
}

# Validate a profile, expand its $VAR references, and write the resulting
# ovcli.conf JSON to $2 with mode 600. Prints a masked one-line summary to
# stderr when stderr is a terminal.
#
# The values never pass through a shell variable: python writes the file
# itself, so the API key stays out of ovx's own process image and out of any
# command substitution.
materialize() {
  local name="$1" dest="$2"
  CONFIG_FILE="$CONFIG_FILE" PROFILE_NAME="$name" DEST="$dest" \
  FIELD_SPEC="$FIELD_SPEC" python3 <<'PY'
import json, os, re, sys
try:
    import tomllib
except ModuleNotFoundError:
    sys.stderr.write("ovx: requires Python 3.11+ (tomllib) to read TOML\n")
    sys.exit(1)

path = os.environ["CONFIG_FILE"]
name = os.environ["PROFILE_NAME"]
dest = os.environ["DEST"]
spec = dict(
    line.split(":", 1) for line in os.environ["FIELD_SPEC"].splitlines() if line
)

if not os.path.isfile(path):
    sys.stderr.write(f"ovx: config file not found: {path}\n")
    sys.exit(1)
try:
    with open(path, "rb") as f:
        data = tomllib.load(f)
except tomllib.TOMLDecodeError as e:
    sys.stderr.write(f"ovx: failed to parse {path}: {e}\n")
    sys.exit(2)
except OSError as e:
    sys.stderr.write(f"ovx: cannot read {path}: {e}\n")
    sys.exit(1)
if name not in data:
    avail = ", ".join(sorted(k for k in data if isinstance(data[k], dict))) or "(none)"
    sys.stderr.write(f"ovx: profile '{name}' not found. Available: {avail}\n")
    sys.exit(3)
sec = data[name]
if not isinstance(sec, dict):
    sys.stderr.write(f"ovx: profile '{name}' is not a table\n")
    sys.exit(2)

for k in sec:
    if k not in spec:
        sys.stderr.write(f"ovx: profile '{name}' has unknown field '{k}'\n")
        sys.stderr.write(f"      allowed: {', '.join(sorted(spec))}\n")
        sys.exit(2)

if not str(sec.get("url", "")).strip():
    sys.stderr.write(f"ovx: profile '{name}' is missing required field 'url'\n")
    sys.exit(2)

# A TOML value has to match the JSON type ovcli.conf expects for that key,
# or ov rejects the file after ovx has already reported success.
CHECK = {
    "str": (str, "a string"),
    "num": ((int, float), "a number"),
    "bool": (bool, "true or false"),
    "map": (dict, "a table"),
}
for k, v in sec.items():
    want, describe = CHECK[spec[k]]
    # bool is a subclass of int, so a bare `true` would satisfy a num check.
    if spec[k] == "num" and isinstance(v, bool):
        ok = False
    else:
        ok = isinstance(v, want)
    if not ok:
        sys.stderr.write(f"ovx: profile '{name}' field '{k}' must be {describe}\n")
        sys.exit(2)

VAR = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)')

def expand(value, key):
    """Expand $VAR / ${VAR} in every string, walking into nested tables."""
    if isinstance(value, str):
        def repl(m):
            var = m.group(1) or m.group(2)
            if not os.environ.get(var):
                sys.stderr.write(
                    f"ovx: profile '{name}' field '{key}' references "
                    f"${var} which is not set\n"
                )
                sys.exit(4)
            return os.environ[var]
        return VAR.sub(repl, value)
    if isinstance(value, dict):
        return {k: expand(v, f"{key}.{k}") for k, v in value.items()}
    return value

out = {k: expand(v, k) for k, v in sec.items()}

# A stored OAuth token outranks whatever the profile says. ov has no separate
# field for one: ApiKeyAuthPlugin dispatches on the ovat_ prefix, so the token
# travels in api_key and the server resolves it as the person who approved it.
oauth_token = os.environ.get("OVX_OAUTH_TOKEN")
if oauth_token:
    out["api_key"] = oauth_token

# O_EXCL so a pre-existing path is never followed or overwritten, and 0600
# from the start rather than a chmod after the secret has already landed.
fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(out, f, indent=2)
    f.write("\n")

# The banner confirms which instance the command is about to hit, which is
# the whole point of naming a profile. Only on a terminal: in a pipeline it
# would corrupt whatever is reading ov's stderr.
if sys.stderr.isatty():
    key = out.get("api_key") or ""
    shown = f"{key[:4]}…" if len(key) > 8 else ("<set>" if key else "<unset>")
    sys.stderr.write(
        f"ovx: profile={name}  url={out.get('url', '<unset>')}  api_key={shown}\n"
    )
PY
}

# Append a [profile] section to the config file. Only the wizard fields are
# written; anything else is added by hand.
write_profile() {
  local name="$1" url="$2" api_key="$3" account="$4" user="$5" rc=0
  ensure_config_dir
  CONFIG_FILE="$CONFIG_FILE" PROFILE_NAME="$name" WIZARD_FIELDS="$WIZARD_FIELDS" \
  V_url="$url" V_api_key="$api_key" V_account="$account" V_user="$user" \
  python3 <<'PY' || rc=$?
import os, sys
path = os.environ["CONFIG_FILE"]
name = os.environ["PROFILE_NAME"]
def esc(s):
    return (s.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\n", "\\n")
             .replace("\r", "\\r")
             .replace("\t", "\\t"))
lines = [f'["{esc(name)}"]']
for k in os.environ["WIZARD_FIELDS"].split():
    v = os.environ[f"V_{k}"]
    if v != "":
        lines.append(f'{k} = "{esc(v)}"')
content = "\n".join(lines) + "\n"
try:
    need_sep = os.path.isfile(path) and os.path.getsize(path) > 0
    # 0600 from creation. A plain open() would make the file 0644 under the
    # usual umask and leave it that way until the chmod below, which lands
    # only after a literal api_key has already been written into it.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as f:
        if need_sep:
            f.write("\n")
        f.write(content)
except OSError as e:
    sys.stderr.write(f"ovx: cannot write {path}: {e}\n")
    sys.exit(1)
PY
  if (( rc != 0 )); then
    return "$rc"
  fi
  chmod 600 "$CONFIG_FILE" 2>/dev/null || true
}

has_profile() {
  local n
  [[ ${#PROFILE_NAMES[@]} -eq 0 ]] && return 1
  for n in "${PROFILE_NAMES[@]}"; do
    [[ "$n" == "$1" ]] && return 0
  done
  return 1
}

# Prompt for the wizard fields, using $1..$4 as defaults (raw, may be empty).
# Sets globals URL API_KEY ACCOUNT USER. A non-empty api_key default is kept
# on Enter and is NEVER echoed (the literal stays hidden).
prompt_fields() {
  local d_url="${1:-}" d_api_key="${2:-}" d_account="${3:-}" d_user="${4:-}"
  # Init the globals we set via `read`. A failed read (no tty, EOF) would
  # otherwise leave them unset and trip `set -u` on the lines below.
  URL="" API_KEY="" ACCOUNT="" USER_FIELD=""
  while :; do
    read -r -p "url [${d_url}]: " URL </dev/tty
    URL="$(trim "${URL:-$d_url}")"
    [[ -n "$URL" ]] && break
    echo "  url required" >&2
  done
  if [[ -n "$d_api_key" ]]; then
    # -s: do not echo the typed key. The kept default is never shown either.
    # echo advances the line since -s also swallows the Enter newline. It must
    # go to stderr: create_profile echoes the profile NAME on stdout for capture
    # by $(...), so a stray stdout newline here would corrupt the captured name.
    read -rs -p "api_key (literal or \$VAR) [Enter to keep current]: " API_KEY </dev/tty
    echo >&2
    API_KEY="$(trim "$API_KEY")"
    [[ -z "$API_KEY" ]] && API_KEY="$d_api_key"
  else
    read -rs -p "api_key (literal or \$VAR, blank for none): " API_KEY </dev/tty
    echo >&2
    API_KEY="$(trim "$API_KEY")"
  fi
  read -r -p "account [${d_account}]: " ACCOUNT </dev/tty
  ACCOUNT="$(trim "${ACCOUNT:-$d_account}")"
  read -r -p "user [${d_user}]: " USER_FIELD </dev/tty
  USER_FIELD="$(trim "${USER_FIELD:-$d_user}")"
}

create_profile() {
  local default_name="${1:-}" name
  echo "" >&2
  echo "Create a new profile." >&2
  while :; do
    read -r -p "Profile name${default_name:+ [$default_name]}: " name </dev/tty
    name="$(trim "${name:-$default_name}")"
    if [[ -z "$name" ]]; then
      echo "  name required" >&2; continue
    fi
    if [[ ! "$name" =~ ^[A-Za-z0-9_-]+$ ]]; then
      echo "  use only letters, digits, underscore, dash" >&2; continue
    fi
    if [[ "$name" =~ ^[0-9]+$ ]]; then
      echo "  a purely-numeric name clashes with menu indices — add a letter" >&2; continue
    fi
    if has_profile "$name"; then
      echo "  '$name' already exists — choose another" >&2; continue
    fi
    break
  done
  echo "  Tip: give api_key as \$VAR to keep the secret out of the file." >&2
  prompt_fields "https://127.0.0.1:1933" "" "" ""
  # Report success only if the write succeeded. This function runs inside
  # $(...), where errexit is off, so a failed write would otherwise be
  # announced as a success and the caller would run ov against nothing.
  write_profile "$name" "$URL" "$API_KEY" "$ACCOUNT" "$USER_FIELD" || return $?
  echo "ovx: wrote profile '$name' to $CONFIG_FILE" >&2
  echo "$name"
}

# Emit the raw (unexpanded) wizard-field values for a profile, NUL-delimited
# in WIZARD_FIELDS order. Used to pre-fill an edit. NUL-delimited (not
# newline) so a value containing a newline cannot desync the reader in
# edit_profile; the reader uses `read -rd ''`.
read_profile_raw() {
  local name="$1"
  CONFIG_FILE="$CONFIG_FILE" PROFILE_NAME="$name" WIZARD_FIELDS="$WIZARD_FIELDS" \
  python3 <<'PY'
import os, sys
try:
    import tomllib
except ModuleNotFoundError:
    sys.stderr.write("ovx: requires Python 3.11+ (tomllib) to read TOML\n")
    sys.exit(1)
path = os.environ["CONFIG_FILE"]
name = os.environ["PROFILE_NAME"]
if not os.path.isfile(path):
    sys.stderr.write(f"ovx: config file not found: {path}\n")
    sys.exit(1)
try:
    with open(path, "rb") as f:
        data = tomllib.load(f)
except tomllib.TOMLDecodeError as e:
    sys.stderr.write(f"ovx: failed to parse {path}: {e}\n")
    sys.exit(2)
except OSError as e:
    sys.stderr.write(f"ovx: cannot read {path}: {e}\n")
    sys.exit(1)
if name not in data or not isinstance(data[name], dict):
    sys.stderr.write(f"ovx: profile '{name}' not found\n")
    sys.exit(3)
sec = data[name]
for k in os.environ["WIZARD_FIELDS"].split():
    sys.stdout.write(str(sec.get(k, "")) + "\0")
PY
}

# Surgically update one [profile] section: rewrite the wizard fields in place
# and leave every other line in the file (other sections, comments inside or
# outside this section, blank lines, fields the wizard does not prompt for)
# alone.
rewrite_profile() {
  local name="$1" url="$2" api_key="$3" account="$4" user="$5" rc=0
  CONFIG_FILE="$CONFIG_FILE" PROFILE_NAME="$name" WIZARD_FIELDS="$WIZARD_FIELDS" \
  V_url="$url" V_api_key="$api_key" V_account="$account" V_user="$user" \
  python3 <<'PY' || rc=$?
import os, sys, re
path = os.environ["CONFIG_FILE"]
name = os.environ["PROFILE_NAME"]
def esc(s):
    # Escape for a TOML basic string. Backslash and quote first, then control
    # chars so a value containing a newline round-trips as "\n", not a raw
    # newline (which is illegal inside a basic string).
    return (s.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\n", "\\n")
             .replace("\r", "\\r")
             .replace("\t", "\\t"))
# Ordered so keys typed during this edit land in a stable position if appended.
ORDER = [(k, os.environ[f"V_{k}"]) for k in os.environ["WIZARD_FIELDS"].split()]
known = dict(ORDER)
def header_name(line):
    s = line.strip()
    if "#" in s:
        s = s[:s.index("#")].strip()  # tolerate a trailing comment on the header
    if s.startswith("[") and s.endswith("]") and len(s) > 2:
        key = s[1:-1].strip()
        if len(key) >= 2 and key[0] == key[-1] and key[0] in ('"', "'"):
            key = key[1:-1]
        return key
    return None
def assign_key(line):
    m = re.match(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
    return m.group(1) if m else None
try:
    with open(path, "r") as f:
        lines = f.read().splitlines()
except OSError as e:
    sys.stderr.write(f"ovx: cannot read {path}: {e}\n")
    sys.exit(1)
start = None
for i, ln in enumerate(lines):
    if header_name(ln) == name:
        start = i
        break
if start is None:
    # tomllib listed this name, so something in the file declares it in a form
    # this line scanner cannot rewrite -- a dotted header like [prod.eu], or a
    # quoted name holding a '#'. Refuse rather than write the wrong section.
    sys.stderr.write(
        f"ovx: profile '{name}' is not a plain [{name}] section in {path};\n"
        f"      edit it by hand\n"
    )
    sys.exit(3)
end = len(lines)
for i in range(start + 1, len(lines)):
    if header_name(lines[i]) is not None:
        end = i
        break
# Rebuild the section: keep the header, then walk the existing body. Wizard
# keys are rewritten in place (dropped if the new value is empty); comments,
# blanks, and hand-added fields are preserved verbatim. Wizard keys not
# already present are appended at the end.
new_body = [f'["{esc(name)}"]']
written = set()
for ln in lines[start + 1:end]:
    k = assign_key(ln)
    if k in known:
        v = known[k]
        if v != "":
            new_body.append(f'{k} = "{esc(v)}"')
        written.add(k)
    else:
        new_body.append(ln)
for k, v in ORDER:
    if k not in written and v != "":
        new_body.append(f'{k} = "{esc(v)}"')
result = lines[:start] + new_body + lines[end:]
try:
    with open(path, "w") as f:
        f.write("\n".join(result) + "\n")
except OSError as e:
    sys.stderr.write(f"ovx: cannot write {path}: {e}\n")
    sys.exit(1)
PY
  if (( rc != 0 )); then
    return "$rc"
  fi
  chmod 600 "$CONFIG_FILE" 2>/dev/null || true
}

edit_profile() {
  local name="$1" d_url d_api_key d_account d_user
  if [[ ${#PROFILE_NAMES[@]} -eq 0 ]] || ! has_profile "$name"; then
    echo "ovx: profile '$name' not found." >&2
    list_profiles >&2
    return 1
  fi
  # Read the NUL-delimited fields straight from the function. Not via $():
  # command substitution truncates at the first NUL. read -rd '' reads up to
  # each NUL, so a value containing a newline is preserved exactly.
  { IFS= read -rd '' d_url;     IFS= read -rd '' d_api_key;
    IFS= read -rd '' d_account; IFS= read -rd '' d_user; \
  } < <(read_profile_raw "$name")
  echo "" >&2
  echo "Edit profile '$name' (press Enter to keep current)." >&2
  echo "  Tip: give api_key as \$VAR to keep the secret out of the file." >&2
  prompt_fields "$d_url" "$d_api_key" "$d_account" "$d_user"
  rewrite_profile "$name" "$URL" "$API_KEY" "$ACCOUNT" "$USER_FIELD" || return $?
  echo "ovx: updated profile '$name' in $CONFIG_FILE" >&2
}

# Confirm, then remove a [profile] section (its header, every line in its
# body, and one blank separator line before it, if present) from the config
# file. Refuses on anything but an explicit y/yes so a stray Enter never
# deletes a profile.
delete_profile() {
  local name="$1" confirm rc=0
  read -r -p "Delete profile '$name'? This can't be undone. [y/N]: " confirm </dev/tty
  confirm="$(trim "$confirm")"
  if [[ ! "$confirm" =~ ^[Yy]([Ee][Ss])?$ ]]; then
    echo "ovx: cancelled — '$name' was not deleted." >&2
    return 1
  fi
  CONFIG_FILE="$CONFIG_FILE" PROFILE_NAME="$name" python3 <<'PY' || rc=$?
import os, sys
path = os.environ["CONFIG_FILE"]
name = os.environ["PROFILE_NAME"]
def header_name(line):
    s = line.strip()
    if "#" in s:
        s = s[:s.index("#")].strip()  # tolerate a trailing comment on the header
    if s.startswith("[") and s.endswith("]") and len(s) > 2:
        key = s[1:-1].strip()
        if len(key) >= 2 and key[0] == key[-1] and key[0] in ('"', "'"):
            key = key[1:-1]
        return key
    return None
try:
    with open(path, "r") as f:
        lines = f.read().splitlines()
except OSError as e:
    sys.stderr.write(f"ovx: cannot read {path}: {e}\n")
    sys.exit(1)
start = None
for i, ln in enumerate(lines):
    if header_name(ln) == name:
        start = i
        break
if start is None:
    # See the matching note in rewrite_profile: tomllib named this profile but
    # no plain [name] header declares it, so deleting by line range would cut
    # the wrong section.
    sys.stderr.write(
        f"ovx: profile '{name}' is not a plain [{name}] section in {path};\n"
        f"      delete it by hand\n"
    )
    sys.exit(3)
end = len(lines)
for i in range(start + 1, len(lines)):
    if header_name(lines[i]) is not None:
        end = i
        break
del_start = start
if del_start > 0 and lines[del_start - 1].strip() == "":
    del_start -= 1
result = lines[:del_start] + lines[end:]
text = ("\n".join(result) + "\n") if result else ""
try:
    with open(path, "w") as f:
        f.write(text)
except OSError as e:
    sys.stderr.write(f"ovx: cannot write {path}: {e}\n")
    sys.exit(1)
PY
  if (( rc != 0 )); then
    return "$rc"
  fi
  chmod 600 "$CONFIG_FILE" 2>/dev/null || true
  echo "ovx: deleted profile '$name' from $CONFIG_FILE" >&2
}

# List existing profiles and read a choice; echo the chosen name. No create
# option — used for picking a target to edit or delete. $1 is the verb shown
# in the prompt and the empty-list message (defaults to "edit").
pick_existing() {
  local verb="${1:-edit}" sel i n cap
  if [[ ${#PROFILE_NAMES[@]} -eq 0 ]]; then
    echo "ovx: no profiles to $verb — use 'ovx -n' to create one." >&2
    return 1
  fi
  cap="$(tr '[:lower:]' '[:upper:]' <<<"${verb:0:1}")${verb:1}"
  while :; do
    echo "" >&2
    echo "Profiles:" >&2
    i=1
    for n in "${PROFILE_NAMES[@]}"; do
      printf '  %2d) %s\n' "$i" "$n" >&2
      i=$((i+1))
    done
    read -r -p "$cap which: " sel </dev/tty
    sel="$(trim "$sel")"
    if [[ -z "$sel" ]]; then echo "  empty input" >&2; continue; fi
    if [[ "$sel" =~ ^[0-9]+$ ]]; then
      sel="${sel#"${sel%%[!0]*}"}"; sel="${sel:-0}"
      if (( sel >= 1 && sel <= ${#PROFILE_NAMES[@]} )); then
        echo "${PROFILE_NAMES[$((sel-1))]}"
        return
      fi
      echo "  out of range" >&2; continue
    fi
    if has_profile "$sel"; then echo "$sel"; return; fi
    echo "  no profile named '$sel'" >&2
  done
}

choose_profile() {
  local sel i n new_idx edit_idx delete_idx target names line
  if [[ ${#PROFILE_NAMES[@]} -eq 0 ]]; then
    create_profile
    return
  fi
  while :; do
    echo "" >&2
    echo "Available profiles:" >&2
    i=1
    for n in "${PROFILE_NAMES[@]}"; do
      printf '  %2d) %s\n' "$i" "$n" >&2
      i=$((i+1))
    done
    new_idx="$i"           # create
    edit_idx=$((i+1))      # edit
    delete_idx=$((i+2))    # delete
    printf '  %2d) create a new profile\n' "$new_idx" >&2
    printf '  %2d) edit an existing profile\n' "$edit_idx" >&2
    printf '  %2d) delete an existing profile\n' "$delete_idx" >&2
    read -r -p "Choose: " sel </dev/tty
    sel="$(trim "$sel")"
    if [[ -z "$sel" ]]; then echo "  empty input" >&2; continue; fi
    if [[ "$sel" =~ ^[0-9]+$ ]]; then
      # Strip leading zeros so (( )) reads decimal, not octal (avoids
      # "value too great for base" noise on input like 08/09).
      sel="${sel#"${sel%%[!0]*}"}"
      sel="${sel:-0}"
      if (( sel == new_idx )); then
        create_profile
        return
      elif (( sel == edit_idx )); then
        target="$(pick_existing edit)" || continue
        edit_profile "$target" || continue
        echo "$target"; return
      elif (( sel == delete_idx )); then
        target="$(pick_existing delete)" || continue
        delete_profile "$target" || continue
        # Refresh PROFILE_NAMES: the deleted name must not linger in the
        # menu or in has_profile lookups for the rest of this loop.
        names="$(profile_names)" || continue
        PROFILE_NAMES=()
        if [[ -n "$names" ]]; then
          while IFS= read -r line; do
            [[ -n "$line" ]] && PROFILE_NAMES+=("$line")
          done <<<"$names"
        fi
        if [[ ${#PROFILE_NAMES[@]} -eq 0 ]]; then
          create_profile
          return
        fi
        continue
      elif (( sel >= 1 && sel < new_idx )); then
        echo "${PROFILE_NAMES[$((sel-1))]}"
        return
      else
        echo "  out of range" >&2; continue
      fi
    fi
    if has_profile "$sel"; then
      echo "$sel"; return
    fi
    echo "  no profile named '$sel'" >&2
  done
}

# Write the profile to a private temporary ovcli.conf, run ov against it, and
# return ov's exit code. The config is removed by the exit trap, not here:
# a signal must not leave it behind either.
#
# ov runs as a child rather than via exec so this script survives to clean up.
# Errexit does not apply inside a function called in a `||` context, so every
# step that can fail is checked by hand.
# Print a profile's `url`, with $VAR references expanded. Login needs the base
# URL before any config is materialized, and reading just this one field keeps
# a missing api_key from failing a login whose whole point is not needing one.
profile_url() {
  CONFIG_FILE="$CONFIG_FILE" PROFILE_NAME="$1" python3 <<'PY'
import os, re, sys, tomllib

path, name = os.environ["CONFIG_FILE"], os.environ["PROFILE_NAME"]
try:
    with open(path, "rb") as f:
        data = tomllib.load(f)
except (OSError, tomllib.TOMLDecodeError) as e:
    sys.stderr.write(f"ovx: cannot read {path}: {e}\n")
    sys.exit(2)

sec = data.get(name)
if not isinstance(sec, dict):
    sys.stderr.write(f"ovx: profile '{name}' not found\n")
    sys.exit(3)

url = str(sec.get("url", "")).strip()
if not url:
    sys.stderr.write(f"ovx: profile '{name}' is missing required field 'url'\n")
    sys.exit(2)

VAR = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)')

def repl(m):
    var = m.group(1) or m.group(2)
    if not os.environ.get(var):
        sys.stderr.write(f"ovx: profile '{name}' url references ${var} which is not set\n")
        sys.exit(4)
    return os.environ[var]

sys.stdout.write(VAR.sub(repl, url))
PY
}

# Path of the token file for a profile. Kept in one place so login, logout and
# materialize cannot disagree about where a token lives.
token_file() {
  printf '%s/%s.json' "$TOKEN_DIR" "$1"
}

# Run the OAuth 2.1 authorization-code flow and store the resulting tokens.
#
# The poll shape, and why: OpenViking mints the authorization code inside
# GET /oauth/authorize/page/status, which deletes the pending row on the first
# read that finds it verified. That makes the code single-use across readers,
# so whoever polls first consumes it. Studio's consent page polls in a loop, so
# sending the operator there would race us and win. Studio's *verify* page has
# no pending id and cannot poll, so directing the operator to type the
# 6-character code there leaves ovx the only reader and no listener is needed.
#
# The display code is only rendered into the authorize page's HTML — the JSON
# endpoint withholds it on purpose — so ovx reads it from that page. The page
# needs no credential and already shows the code to anyone holding the pending
# id, so reading it here grants ovx nothing it did not already have.
#
# urllib rather than httpx: this script runs under whatever python3 the
# operator has, with no virtualenv and nothing installed, so the standard
# library is the only thing it can rely on.
oauth_login() {
  local name="$1" base
  base="$(profile_url "$name")" || return $?
  ensure_config_dir
  mkdir -p "$TOKEN_DIR"
  chmod 700 "$TOKEN_DIR" 2>/dev/null || true

  OVX_BASE_URL="$base" OVX_TOKEN_FILE="$(token_file "$name")" \
  OVX_PROFILE="$name" OVX_VERSION="$OVX_VERSION" python3 <<'PY'
import base64, hashlib, json, os, re, secrets, sys, time
import urllib.error, urllib.parse, urllib.request

BASE = os.environ["OVX_BASE_URL"].rstrip("/")
DEST = os.environ["OVX_TOKEN_FILE"]
PROFILE = os.environ["OVX_PROFILE"]
UA = "ovx/" + os.environ.get("OVX_VERSION", "dev")

# A redirect_uri is required by the protocol and validated by exact match
# against what we register, but nothing ever dials it: the authorization code
# comes back to us in the status response, not over a loopback socket.
REDIRECT_URI = "http://127.0.0.1:1/ovx-callback"
POLL_SECONDS = 2
POLL_TIMEOUT = 600
DISPLAY_CODE = re.compile(r'id="displayCode"[^>]*>\s*([A-Z2-9]{6})\s*<')


def die(message):
    sys.stderr.write(f"ovx: {message}\n")
    sys.exit(1)


class Redirected(Exception):
    """Carries the Location of a redirect we want to read, not follow."""

    def __init__(self, location):
        self.location = location


class CatchRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Redirected(newurl)


NO_REDIRECT = urllib.request.build_opener(CatchRedirect)


def request(url, *, data=None, form=None, opener=None, want_json=True):
    """One HTTP call. Returns parsed JSON, or the raw body when want_json is off."""
    body, headers = None, {"User-Agent": UA, "Accept": "application/json"}
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers)
    # An opener exposes .open(); the module-level shortcut is .urlopen().
    open_it = opener.open if opener is not None else urllib.request.urlopen
    try:
        with open_it(req, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()[:300]
        raise SystemExit(f"ovx: {url} returned HTTP {e.code}: {detail or e.reason}")
    except OSError as e:
        raise SystemExit(f"ovx: cannot reach {url}: {e}")
    if not want_json:
        return raw.decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError:
        raise SystemExit(f"ovx: {url} returned a non-JSON body")


# 1. Discover the endpoints rather than hard-coding them, so a server that
#    moves them stays reachable.
meta = request(f"{BASE}/.well-known/oauth-authorization-server")
for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
    if not meta.get(key):
        die(f"server metadata is missing {key}; is OAuth enabled on {BASE}?")

# 2. Register. Every OpenViking client is public + PKCE, so there is no secret
#    to hold and a fresh registration per login costs nothing.
client = request(
    meta["registration_endpoint"],
    data={
        "client_name": f"ovx ({PROFILE})",
        "redirect_uris": [REDIRECT_URI],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "scope": "mcp",
    },
)
client_id = client.get("client_id") or die("registration returned no client_id")

# 3. PKCE. S256 only — the server is OAuth 2.1, which forbids `plain`.
verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
challenge = (
    base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
    .rstrip(b"=")
    .decode()
)
state = secrets.token_urlsafe(16)

# 4. Authorize. The response is a redirect to the authorize page, and the
#    pending id we need is in its query string.
authorize_url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(
    {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "mcp",
    }
)
try:
    request(authorize_url, opener=NO_REDIRECT, want_json=False)
except Redirected as r:
    location = r.location
else:
    die("the authorize endpoint did not redirect; cannot find the pending id")

pending = urllib.parse.parse_qs(urllib.parse.urlparse(location).query).get("pending", [""])[0]
if not pending:
    die(f"no pending id in the authorize redirect: {location}")

# 5. Read the display code off the server-rendered page.
page = request(f"{BASE}/oauth/authorize/page?pending={urllib.parse.quote(pending)}",
               want_json=False)
found = DISPLAY_CODE.search(page)
if not found:
    die("could not read the verification code from the authorize page")

sys.stderr.write(
    f"\novx: approve this login in OpenViking Studio.\n\n"
    f"  1. Open  {BASE}/studio/oauth/verify\n"
    f"  2. Enter  {found.group(1)}\n\n"
    f"Waiting for approval (Ctrl-C to abort)…\n"
)

# 6. Poll. We are the only reader, so the code is ours when it is minted.
deadline = time.monotonic() + POLL_TIMEOUT
status_url = f"{BASE}/oauth/authorize/page/status?pending={urllib.parse.quote(pending)}"
code = None
while time.monotonic() < deadline:
    try:
        status = request(status_url)
    except SystemExit as e:
        if "HTTP 410" in str(e):
            die("this login expired or was denied. Run ovx --login again.")
        raise
    if status.get("status") == "approved" and status.get("redirect_url"):
        query = urllib.parse.parse_qs(
            urllib.parse.urlparse(status["redirect_url"]).query
        )
        if query.get("state", [""])[0] != state:
            die("state mismatch on the authorization response; aborting")
        code = query.get("code", [""])[0]
        break
    time.sleep(POLL_SECONDS)

if not code:
    die("timed out waiting for approval")

# 7. Exchange the code for tokens.
token = request(
    meta["token_endpoint"],
    form={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    },
)
access = token.get("access_token") or die("token response carried no access_token")

record = {
    "version": 1,
    "profile": PROFILE,
    "base_url": BASE,
    "client_id": client_id,
    "access_token": access,
    "refresh_token": token.get("refresh_token"),
    # Absolute, computed at receipt: a clock read decides freshness rather
    # than a countdown nobody is running.
    "expires_at": int(time.time()) + int(token.get("expires_in") or 0),
    "token_endpoint": meta["token_endpoint"],
    "revocation_endpoint": meta.get("revocation_endpoint"),
}

# 0600 from creation, not a chmod afterwards, so the token is never briefly
# world-readable. Replacing an existing login is normal, so O_EXCL on a temp
# name plus a rename rather than O_EXCL on the destination.
tmp = DEST + ".new"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
try:
    with os.fdopen(fd, "w") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    os.replace(tmp, DEST)
except BaseException:
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise

sys.stderr.write(f"ovx: logged in. Token stored for profile '{PROFILE}'.\n")
PY
}

# Best-effort revoke, then delete the token file.
oauth_logout() {
  local name="$1" file
  file="$(token_file "$name")"
  if [[ ! -f "$file" ]]; then
    echo "ovx: no stored login for profile '$name'." >&2
    return 0
  fi
  OVX_TOKEN_FILE="$file" OVX_VERSION="$OVX_VERSION" python3 <<'PY'
import json, os, sys, urllib.error, urllib.parse, urllib.request

path = os.environ["OVX_TOKEN_FILE"]
with open(path) as f:
    record = json.load(f)

# Revocation is courtesy: the file is going either way, and a server that is
# down must not leave a token undeletable on disk.
endpoint = record.get("revocation_endpoint")
if endpoint and record.get("refresh_token"):
    body = urllib.parse.urlencode(
        {
            "token": record["refresh_token"],
            "token_type_hint": "refresh_token",
            "client_id": record.get("client_id", ""),
        }
    ).encode()
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "ovx/" + os.environ.get("OVX_VERSION", "dev"),
        },
    )
    try:
        urllib.request.urlopen(request, timeout=15).close()
    except (urllib.error.HTTPError, OSError) as e:
        sys.stderr.write(f"ovx: could not revoke the token ({e}); removing it locally.\n")

os.unlink(path)
sys.stderr.write(f"ovx: logged out of profile '{record.get('profile', '?')}'.\n")
PY
}

# Print a usable access token for a profile, refreshing it first when it has
# expired. Silent and exit 1 when there is no stored login, so callers can
# treat "not logged in" as an ordinary state rather than an error.
token_current() {
  local name="$1" file
  file="$(token_file "$name")"
  [[ -f "$file" ]] || return 1
  OVX_TOKEN_FILE="$file" OVX_VERSION="$OVX_VERSION" python3 <<'PY'
import json, os, sys, time, urllib.error, urllib.parse, urllib.request

# A token inside this window is treated as already gone. Refreshing costs one
# request; handing ov a token that dies mid-command costs a confusing 401.
SKEW = 60

path = os.environ["OVX_TOKEN_FILE"]
try:
    with open(path) as f:
        record = json.load(f)
except (OSError, ValueError) as e:
    sys.stderr.write(f"ovx: unreadable token file {path}: {e}\n")
    sys.exit(1)

expires_at = record.get("expires_at") or 0
if expires_at and time.time() + SKEW >= expires_at:
    refresh = record.get("refresh_token")
    endpoint = record.get("token_endpoint")
    if not refresh or not endpoint:
        sys.stderr.write("ovx: stored login expired and cannot be refreshed. Run ovx --login.\n")
        sys.exit(1)
    body = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": record.get("client_id", ""),
        }
    ).encode()
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "ovx/" + os.environ.get("OVX_VERSION", "dev"),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            fresh = json.loads(response.read())
    except (urllib.error.HTTPError, OSError, ValueError) as e:
        sys.stderr.write(f"ovx: could not refresh the stored login ({e}). Run ovx --login.\n")
        sys.exit(1)
    record["access_token"] = fresh.get("access_token") or record["access_token"]
    # A refresh may rotate the refresh token; keeping the old one would break
    # the next refresh.
    record["refresh_token"] = fresh.get("refresh_token") or record["refresh_token"]
    record["expires_at"] = int(time.time()) + int(fresh.get("expires_in") or 0)
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)

sys.stdout.write(record["access_token"])
PY
}

run_ov() {
  local name="$1" conf rc
  TMPROOT="$(mktemp -d "${TMPDIR:-/tmp}/ovx.XXXXXXXX")" || {
    echo "ovx: could not create a temporary directory" >&2
    return 1
  }
  chmod 700 "$TMPROOT" 2>/dev/null || true
  # Named ovcli.conf, not a random name: ov derives sibling paths such as
  # ovcli.conf.<name> from it.
  conf="$TMPROOT/ovcli.conf"
  # A stored login, refreshed if stale. Absent one, token_current fails and the
  # profile's own api_key stands — logging in is optional, not a precondition.
  local token
  if token="$(token_current "$name")"; then
    export OVX_OAUTH_TOKEN="$token"
  fi
  materialize "$name" "$conf" || return $?
  export OPENVIKING_CLI_CONFIG_FILE="$conf"
  rc=0
  if [[ ${#OV_ARGS[@]} -gt 0 ]]; then
    ov "${OV_ARGS[@]}" || rc=$?
  else
    ov || rc=$?
  fi
  return "$rc"
}

# --- arg parsing ---------------------------------------------------------

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)    usage; exit 0 ;;
    -V|--version) echo "ovx $OVX_VERSION"; exit 0 ;;
    -l|--list)    LIST=1; shift ;;
    -n|--new)     NEW=1; shift ;;
    -e|--edit)    EDIT=1; shift ;;
    -d|--delete)  DELETE=1; shift ;;
    -L|--login)   LOGIN=1; shift ;;
    --logout)     LOGOUT=1; shift ;;
    --)           shift; OV_ARGS+=("$@"); break ;;
    -*)           echo "ovx: unknown option: $1 (use -- to forward args to ov)" >&2; exit 1 ;;
    *)            PROFILE="$1"; shift
                   # Forward remaining args to ov, stripping a single leading
                   # "--" separator so `ovx lab -- -o json` works.
                   [[ "${1:-}" == "--" ]] && shift
                   OV_ARGS+=("$@"); break ;;
  esac
done

need python3

if [[ "$LIST" -eq 1 ]]; then
  list_profiles
  exit 0
fi

# ov is needed to run a command, and checking here — before profile
# resolution — means a missing ov fails before the wizard writes a profile to
# disk. Deleting a profile only edits ovx's own config, so it does not need ov
# and should not be blocked by its absence.
if [[ "$DELETE" -ne 1 && "$LOGIN" -ne 1 && "$LOGOUT" -ne 1 ]]; then
  need ov
fi

# --- resolve profile -----------------------------------------------------

# The interactive branches (edit, new, delete, or no profile given) read from
# /dev/tty; refuse cleanly if there is no controlling terminal. A direct
# `ovx <profile>` skips this and runs headless.
if (( EDIT || NEW || DELETE )) || [[ -z "$PROFILE" ]]; then
  require_tty
fi

names="$(profile_names)" || exit $?
PROFILE_NAMES=()
if [[ -n "$names" ]]; then
  while IFS= read -r line; do
    [[ -n "$line" ]] && PROFILE_NAMES+=("$line")
  done <<<"$names"
fi

if [[ "$LOGIN" -eq 1 || "$LOGOUT" -eq 1 ]]; then
  action=login
  [[ "$LOGOUT" -eq 1 ]] && action=logout
  if [[ -z "$PROFILE" ]]; then
    PROFILE="$(pick_existing "$action")" || exit $?
  elif ! has_profile "$PROFILE"; then
    echo "ovx: profile '$PROFILE' not found." >&2
    list_profiles >&2
    exit 1
  fi
  if [[ "$LOGOUT" -eq 1 ]]; then
    oauth_logout "$PROFILE" || exit $?
  else
    oauth_login "$PROFILE" || exit $?
  fi
  exit 0
fi

if [[ "$DELETE" -eq 1 ]]; then
  if [[ -z "$PROFILE" ]]; then
    PROFILE="$(pick_existing delete)" || exit $?
  elif ! has_profile "$PROFILE"; then
    echo "ovx: profile '$PROFILE' not found." >&2
    list_profiles >&2
    exit 1
  fi
  delete_profile "$PROFILE" || exit $?
  exit 0
elif [[ "$EDIT" -eq 1 ]]; then
  if [[ -z "$PROFILE" ]]; then
    PROFILE="$(pick_existing edit)" || exit $?
  fi
  edit_profile "$PROFILE" || exit $?
elif [[ "$NEW" -eq 1 ]]; then
  PROFILE="$(create_profile "${PROFILE:-}")"
elif [[ -n "$PROFILE" ]]; then
  if ! has_profile "$PROFILE"; then
    echo "ovx: profile '$PROFILE' not found." >&2
    list_profiles >&2
    exit 1
  fi
elif [[ ${#PROFILE_NAMES[@]} -eq 0 ]]; then
  echo "ovx: no profiles found at $CONFIG_FILE — let's create one." >&2
  PROFILE="$(create_profile)"
else
  PROFILE="$(choose_profile)"
fi

# --- run -----------------------------------------------------------------

RC=0
run_ov "$PROFILE" || RC=$?
exit "$RC"
