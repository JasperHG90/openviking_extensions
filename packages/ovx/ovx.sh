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
#   -L, --login [P]  Mint a Vault identity token for profile P, store it, exit
#       --logout [P] Remove profile P's stored login, then exit
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
#   ovx -L lab             Log in to 'lab' through Vault, token is stored
#   ovx --logout lab       Forget 'lab's stored login
#   ovx -- -o json status  Pick a profile, forward '-o json status' to ov
#
# Logging in:
#   'ovx -L lab' mints a Vault identity token and stores it under
#   ~/.ovx/tokens/lab.json at mode 600. All it needs is $VAULT_ADDR: ovx talks
#   to Vault's HTTP API itself and does not require the vault CLI. If you
#   already have a session — $VAULT_TOKEN, or ~/.vault-token from a previous
#   'vault login' — it just mints; otherwise it asks for your password.
#
#   $VAULT_TOKEN, $VAULT_NAMESPACE, $VAULT_CACERT and $VAULT_SKIP_VERIFY are
#   read with their usual meanings. A custom $VAULT_TOKEN_HELPER is not
#   supported; set $VAULT_TOKEN instead.
#
#   The profile's 'user' field doubles as the Vault username when ovx has to
#   log in. OpenViking no longer takes identity from 'user' or 'account' — it
#   reads the token's claims — but 'user' still picks the Vault account, and
#   ovx names the one it is using before asking for a password.
#
#   The token is a JWT, which OpenViking accepts in the api_key slot, so a
#   profile that has logged in needs no api_key at all. How long it lasts is
#   the Vault role's business, not ovx's: ovx reads the token's own 'exp'
#   rather than assuming a lifetime, and mints a fresh one as it nears expiry,
#   so changing the role's ttl needs no change here. Run 'ovx <profile> --'
#   after a login and check ~/.ovx/tokens/<profile>.json if you want to know
#   what your role actually grants.
#
#   The role is 'openviking'; override it with $OVX_VAULT_ROLE.
#
#   The stored token outranks the profile's api_key. It expires, which a static
#   key does not — but it is still a credential on disk, unlike a $VAR
#   reference, and there is no server-side revoke for an identity token.
#   'ovx --logout' deletes ovx's copy; a token that leaked before that stays
#   good until it expires. At a week-long ttl, weigh that against the api_key
#   it replaces.
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
# Tokens live beside the config, one file per profile. A profile that has
# logged in needs no api_key at all: the stored token replaces it.
TOKEN_DIR="$OVX_DIR/tokens"

# The Vault role that mints OpenViking identity tokens. The role fixes the
# audience and the ov_account claim the server maps identity from, so it has to
# match what the server was configured against.
VAULT_ROLE="${OVX_VAULT_ROLE:-openviking}"

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
has_tty() {
  { : </dev/tty ; } 2>/dev/null
}

require_tty() {
  if ! has_tty; then
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

# A stored login outranks whatever the profile says. ov has no separate field
# for a bearer token, and it needs none: the server's _extract_token treats an
# api_key with exactly two dots as a JWT and uses it as one. A Vault identity
# token has two dots, so it travels in the api_key slot untouched.
#
# ov also sends anything with two or more dots as an Authorization: Bearer
# header alongside X-API-Key. Both carry the same token and the server reads
# either, so the duplication is harmless and deliberately left alone.
stored_token = os.environ.get("OVX_TOKEN")
if stored_token:
    out["api_key"] = stored_token

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

# Print a profile's `user` field, or nothing when it is unset. Used as the
# default Vault username; unlike `url` it is optional, so an absent value is an
# ordinary state rather than an error.
profile_user() {
  CONFIG_FILE="$CONFIG_FILE" PROFILE_NAME="$1" python3 <<'PY'
import os, re, sys, tomllib

path, name = os.environ["CONFIG_FILE"], os.environ["PROFILE_NAME"]
try:
    with open(path, "rb") as f:
        data = tomllib.load(f)
except (OSError, tomllib.TOMLDecodeError):
    sys.exit(0)

sec = data.get(name)
if not isinstance(sec, dict):
    sys.exit(0)

value = str(sec.get("user", "")).strip()

VAR = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)')
# An unresolved reference yields nothing rather than failing: this only picks a
# default for a prompt, and a login should not be blocked by an unset variable.
if VAR.search(value):
    value = VAR.sub(lambda m: os.environ.get(m.group(1) or m.group(2), ""), value)

sys.stdout.write(value)
PY
}

# Path of the token file for a profile. Kept in one place so login, logout and
# materialize cannot disagree about where a token lives.
#
# The name is checked, not just interpolated: a profile is a TOML key, so it can
# be anything the operator types, and the README invites hand-editing the file.
# Without this, `ovx --logout '../../.ssh/id_ed25519'` would rm -f exactly that.
token_file() {
  case "$1" in
    ""|*/*|*\\*|.|..)
      echo "ovx: refusing profile name '$1': it must not contain a path." >&2
      return 1 ;;
  esac
  printf '%s/%s.json' "$TOKEN_DIR" "$1"
}

# Everything below talks to Vault over its HTTP API, using the python3 this
# script already requires. The `vault` CLI is deliberately NOT a dependency:
# ovx needs three endpoints, and asking an operator to install a second CLI to
# run this one is a poor trade for the JSON parsing it would save.
#
# What the CLI does own is the token helper -- the file it caches your session
# token in. That is `~/.vault-token` unless $VAULT_TOKEN is set, and it is a
# plain file, so ovx reads and writes it directly. A custom
# $VAULT_TOKEN_HELPER is not supported; set $VAULT_TOKEN instead.

# One Python entry point for every Vault call, dispatching on $1.
#
# A single heredoc rather than three sharing a preamble string: the request
# plumbing is written once, and every block in this script stays a plain
# quoted heredoc instead of a `python3 -c "$var"'...'` splice, which is easy to
# get subtly wrong and hard for shellcheck to read.
#
#   vault_call lookup          exit 0 if the cached session is live
#   vault_call login <user>    prompt for a password, cache the session token
#   vault_call mint <role>     print an identity token for the role
#
# Reads VAULT_ADDR, VAULT_TOKEN, VAULT_NAMESPACE, VAULT_CACERT and
# VAULT_SKIP_VERIFY with their usual meanings, so a lab behind a private CA or
# inside a namespace works without ovx inventing its own names for any of them.
vault_call() {
  OVX_VAULT_OP="$1" OVX_VAULT_ARG="${2:-}" python3 <<'PY'
import getpass
import json
import os
import ssl
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

ADDR = os.environ.get("VAULT_ADDR", "").rstrip("/")
NAMESPACE = os.environ.get("VAULT_NAMESPACE", "").strip()
TOKEN_FILE = os.path.expanduser(
    os.environ.get("VAULT_TOKEN_FILE") or "~/.vault-token"
)


def ssl_context() -> ssl.SSLContext:
    """Return the TLS context for talking to Vault, honoring the usual vars."""
    if os.environ.get("VAULT_SKIP_VERIFY", "").lower() in ("1", "true", "yes"):
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    return ssl.create_default_context(cafile=os.environ.get("VAULT_CACERT") or None)


def session_token() -> str:
    """Return the cached Vault session token, or an empty string."""
    token = os.environ.get("VAULT_TOKEN", "").strip()
    if token:
        return token
    try:
        with open(TOKEN_FILE) as handle:
            return handle.read().strip()
    except OSError:
        return ""


def vault(path, payload=None, token=None, timeout=30):
    """Call Vault and return the decoded body.

    Parameters
    ----------
    path :
        API path below /v1, e.g. "auth/token/lookup-self".
    payload :
        Body to POST as JSON; a GET is sent when omitted.
    token :
        Session token for X-Vault-Token; the cached one when omitted.
    timeout :
        Seconds to wait.

    Returns
    -------
    dict
        The parsed response body.

    Raises
    ------
    RuntimeError
        On a transport failure or a non-2xx status, carrying Vault's message.
    """
    headers = {"Accept": "application/json"}
    auth = token if token is not None else session_token()
    if auth:
        headers["X-Vault-Token"] = auth
    if NAMESPACE:
        headers["X-Vault-Namespace"] = NAMESPACE
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        ADDR + "/v1/" + path, data=data, headers=headers
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=ssl_context()
        ) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        message = ""
        try:
            message = json.loads(error.read() or b"{}").get("errors", [""])[0]
        except Exception:
            pass
        detail = ": " + message if message else ""
        raise RuntimeError("HTTP " + str(error.code) + detail) from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise RuntimeError(str(error)) from None


def fail(message: str) -> None:
    """Report a problem on stderr and exit 1."""
    sys.stderr.write("ovx: " + message + "\n")
    sys.exit(1)


def write_private(path: str, content: str) -> None:
    """Write ``content`` to ``path`` at mode 0600, replacing atomically."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ovx-tmp.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def do_lookup() -> None:
    """Exit 0 when the cached session token is live, 1 otherwise."""
    if not session_token():
        sys.exit(1)
    try:
        vault("auth/token/lookup-self")
    except RuntimeError:
        sys.exit(1)


def do_login(user: str) -> None:
    """Exchange a password for a session token and cache it."""
    # getpass reads from /dev/tty with echo off and prompts on stderr, so the
    # password never appears on screen, in argv, or on stdout. It cannot be
    # piped in either, which is deliberate: a piped password lands in shell
    # history or a CI log.
    try:
        password = getpass.getpass("Password for " + user + ": ", stream=sys.stderr)
    except (OSError, EOFError):
        fail("could not read a password from the terminal.")
    if not password:
        fail("no password given.")

    try:
        body = vault(
            "auth/userpass/login/" + urllib.parse.quote(user, safe=""),
            payload={"password": password},
            token="",
        )
    except RuntimeError as error:
        fail("Vault login failed (" + str(error) + ").")

    token = (body.get("auth") or {}).get("client_token")
    if not token:
        fail("Vault accepted the login but returned no token.")

    # Cache it where the vault CLI keeps its own, so the two share one session
    # in either direction. Skipped when $VAULT_TOKEN is set: that is the
    # caller's session to manage, not a file for ovx to overwrite.
    if not os.environ.get("VAULT_TOKEN", "").strip():
        try:
            write_private(TOKEN_FILE, token)
        except OSError as error:
            fail("logged in but could not cache the token: " + str(error))


def do_mint(role: str) -> None:
    """Print an OpenViking identity token minted from ``role``.

    The issuer is deliberately not checked. It has to match the server's
    configuration byte for byte and is set on the Vault side, so validating it
    here would only add a second place to get it wrong.
    """
    try:
        body = vault("identity/oidc/token/" + urllib.parse.quote(role, safe=""))
    except RuntimeError as error:
        fail("could not mint from role '" + role + "' (" + str(error) + ").")

    token = (body.get("data") or {}).get("token")
    if not token:
        fail("Vault returned no token for role '" + role + "'.")
    sys.stdout.write(token)


OP = os.environ["OVX_VAULT_OP"]
ARG = os.environ["OVX_VAULT_ARG"]

if not ADDR:
    fail("$VAULT_ADDR is not set.")

if OP == "lookup":
    do_lookup()
elif OP == "login":
    do_login(ARG)
elif OP == "mint":
    do_mint(ARG)
else:
    fail("unknown Vault operation '" + OP + "'.")
PY
}

# Refuse early, and name what is missing. VAULT_ADDR is the only hard
# requirement: there is no binary to look for, since ovx calls Vault itself.
vault_preflight() {
  if [[ -z "${VAULT_ADDR:-}" ]]; then
    echo "ovx: \$VAULT_ADDR is not set, so ovx does not know which Vault to use." >&2
    echo "     export VAULT_ADDR=https://vault.example.com" >&2
    return 1
  fi
  return 0
}

# Whether a usable Vault session exists. Minting needs one; this is how ovx
# decides whether to ask for a password.
vault_session_ok() {
  [[ -n "${VAULT_ADDR:-}" ]] || return 1
  vault_call lookup >/dev/null 2>&1
}

# Mint an OpenViking identity token: RS256, carrying the ov_account claim the
# server maps the caller from. Its lifetime comes from the role, so ovx never
# states one -- it reads the exp the token actually carries.
vault_mint() {
  vault_call mint "$VAULT_ROLE"
}

# Mint a token for a profile and store it, logging in to Vault first when there
# is no session.
vault_login() {
  local name="$1" user token
  vault_preflight || return 1

  if ! vault_session_ok; then
    # Only this branch is interactive. A caller that already has a session --
    # CI with $VAULT_TOKEN set, say -- goes straight to minting and needs no
    # terminal, so the tty check belongs here rather than around the command.
    if ! has_tty; then
      echo "ovx: no Vault session, and no terminal to log in from." >&2
      echo "     Set \$VAULT_TOKEN, or log in where you can type." >&2
      return 1
    fi
    user="$(profile_user "$name")"
    if [[ -z "$user" ]]; then
      printf 'Vault username: ' >&2
      IFS= read -r user </dev/tty || true
    fi
    if [[ -z "$user" ]]; then
      echo "ovx: no Vault username given." >&2
      return 1
    fi
    echo "ovx: no Vault session; logging in as '$user'." >&2
    vault_call login "$user" || return 1
  fi

  token="$(vault_mint)" || return 1
  if [[ -z "$token" ]]; then
    echo "ovx: Vault returned an empty token for role '$VAULT_ROLE'." >&2
    return 1
  fi

  ensure_config_dir
  mkdir -p "$TOKEN_DIR"
  chmod 700 "$TOKEN_DIR" 2>/dev/null || true
  store_token "$name" "$token" || return 1
  echo "ovx: logged in. Token stored for profile '$name'." >&2
}

# Write a minted token to the profile's token file, recording when it expires.
#
# The expiry is read out of the JWT rather than assumed from the role's TTL:
# the TTL is server-side configuration that can change without ovx knowing,
# while exp is what the server will actually enforce.
store_token() {
  local dest
  dest="$(token_file "$1")" || return 1
  OVX_TOKEN_FILE="$dest" OVX_PROFILE="$1" \
  OVX_RAW_TOKEN="$2" OVX_VAULT_ROLE_NAME="$VAULT_ROLE" python3 <<'PY'
import base64, json, os, sys, tempfile

token = os.environ["OVX_RAW_TOKEN"].strip()
path = os.environ["OVX_TOKEN_FILE"]


def claims(jwt):
    """Return the JWT's payload, or an empty dict when it cannot be read."""
    parts = jwt.split(".")
    if len(parts) != 3:
        return {}
    payload = parts[1]
    # base64url without padding is what JWTs use; restore it before decoding.
    payload += "=" * (-len(payload) % 4)
    try:
        body = json.loads(base64.urlsafe_b64decode(payload))
    except (ValueError, TypeError):
        return {}
    return body if isinstance(body, dict) else {}


expires_at = claims(token).get("exp")
if not isinstance(expires_at, int) or isinstance(expires_at, bool):
    # Without an expiry ovx cannot re-mint ahead of time, so the token would be
    # used until the server rejected it. Fail here instead: an identity token
    # that carries no exp is not the credential this was built for.
    sys.stderr.write("ovx: the minted token carries no usable 'exp' claim.\n")
    sys.exit(1)

record = {
    "profile": os.environ["OVX_PROFILE"],
    "kind": "vault-identity",
    "role": os.environ["OVX_VAULT_ROLE_NAME"],
    "token": token,
    "expires_at": expires_at,
}

# 0600 from creation, not a chmod afterwards, so the token is never briefly
# world-readable. mkstemp rather than a fixed "<path>.new": two ovx runs
# re-minting at once would otherwise share that name, and one could replace the
# real file with the other's half-written copy.
try:
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".token.")
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(record, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
except OSError as error:
    sys.stderr.write(f"ovx: could not write {path}: {error}\n")
    sys.exit(1)
PY
}

# Forget a stored login.
#
# There is no server-side revoke for a Vault identity token: it is a signed
# assertion, not a session, and nothing tracks it. Revoking the Vault session
# would be a much bigger hammer -- it takes every other tool on the machine
# with it -- so it is deliberately not done.
#
# So this deletes ovx's copy and nothing more. A token that leaked before the
# logout stays valid until it expires, which at the current week-long role ttl
# is a long time. Rotating the Vault entity is the only way to cut it short.
vault_logout() {
  local name="$1" file
  file="$(token_file "$name")" || return 1
  if [[ ! -f "$file" ]]; then
    echo "ovx: no stored login for profile '$name'." >&2
    return 0
  fi
  rm -f "$file"
  echo "ovx: logged out of profile '$name'. The token expires on its own." >&2
}

# Print a usable token for a profile, minting a fresh one first when the stored
# one is close to expiring.
#
# Three outcomes, and the difference matters:
#   0  the token is on stdout
#   1  there is no stored login at all -- an ordinary state, and the caller
#      falls back to the profile's api_key
#   2  there IS a login and it cannot be used
#
# 2 is not 1. Falling back to the api_key when a login is expired or corrupt
# would quietly downgrade to a weaker, longer-lived credential the operator
# thought they had stopped using, and the only sign would be a different
# identity on the server. Say so instead.
#
# Identity tokens are not refreshable -- there is no grant to exchange. They
# are re-minted, which needs a live Vault session rather than a stored secret.
# That is the trade for keeping nothing long-lived on disk: the Vault session
# is the renewable thing.
token_current() {
  local name="$1" file stored token
  file="$(token_file "$name")" || return 2
  [[ -f "$file" ]] || return 1

  # Prints the token when it is still good, or the word "stale" when it needs
  # re-minting, so the expiry decision lives in one place.
  stored="$(OVX_TOKEN_FILE="$file" python3 <<'PY'
import json, os, sys, time

# A token inside this window is treated as already gone. Minting costs one
# Vault call; handing ov a token that dies mid-command costs a confusing 401.
SKEW = 300

path = os.environ["OVX_TOKEN_FILE"]


def unusable(message):
    """Report a stored login that exists but cannot be used, and exit 2."""
    sys.stderr.write(f"ovx: {message}\n")
    sys.stderr.write(f"     Run 'ovx --login' to replace it, or delete {path}.\n")
    sys.exit(2)


try:
    with open(path) as handle:
        record = json.load(handle)
except OSError as error:
    unusable(f"cannot read the token file {path}: {error}")
except ValueError as error:
    unusable(f"the token file {path} is not valid JSON: {error}")

# A JSON document is not necessarily an object, and a hand-edited or
# half-written file can be any shape at all. Check rather than assume, so a
# malformed record produces a message instead of a traceback.
if not isinstance(record, dict):
    unusable(f"the token file {path} does not hold an object")

token = record.get("token")
if token is None and record.get("access_token"):
    # An OAuth-era record, from before ovx moved to Vault identity tokens. Its
    # ovat_ token cannot be renewed and the server no longer mints them.
    unusable("this stored login predates Vault identity tokens")
if not isinstance(token, str) or not token:
    unusable(f"the token file {path} carries no usable token")

expires_at = record.get("expires_at")
if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
    unusable(f"the token file {path} carries no usable expiry")

if time.time() + SKEW >= expires_at:
    sys.stdout.write("stale")
else:
    sys.stdout.write(token)
PY
)" || return $?

  if [[ "$stored" != "stale" ]]; then
    printf '%s' "$stored"
    return 0
  fi

  # Expiring, so mint a replacement. This is meant to be invisible while the
  # Vault session lives; it only speaks up when it cannot be done.
  if [[ -z "${VAULT_ADDR:-}" ]]; then
    echo "ovx: the stored login for '$name' expired and \$VAULT_ADDR is not set." >&2
    echo "     Set it to renew, or set the profile's api_key." >&2
    return 2
  fi
  if ! vault_session_ok; then
    echo "ovx: the stored login for '$name' expired and your Vault session is gone." >&2
    echo "     Run 'ovx --login $name' and try again." >&2
    return 2
  fi
  token="$(vault_mint)" || return 2
  if [[ -z "$token" ]]; then
    echo "ovx: Vault returned an empty token while renewing '$name'." >&2
    return 2
  fi
  store_token "$name" "$token" || return 2
  printf '%s' "$token"
}

# Write the profile to a private temporary ovcli.conf, run ov against it, and
# return ov's exit code. The config is removed by the exit trap, not here:
# a signal must not leave it behind either.
#
# ov runs as a child rather than via exec so this script survives to clean up.
# Errexit does not apply inside a function called in a `||` context, so every
# step that can fail is checked by hand.
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
  # A stored login, re-minted if it is close to expiring. Absent one,
  # token_current fails and the profile's own api_key stands — logging in is
  # optional, not a precondition.
  local token trc=0
  token="$(token_current "$name")" || trc=$?
  if [[ "$trc" -ne 0 && "$trc" -ne 1 ]]; then
    # A login exists but is unusable. token_current has already said why.
    return "$trc"
  fi

  # The token is passed to materialize alone, not exported. An exported one
  # would be inherited by ov and readable from its environment by anything
  # running as this user; only the temp ovcli.conf needs to carry it.
  OVX_TOKEN="$token" materialize "$name" "$conf" || return $?

  # No login and no key is not an error -- the server may not want one -- but
  # under an oidc server it is the state right after --logout, and ov's own
  # 401 says nothing about how to fix it. Point at the fix and carry on.
  if [[ "$trc" -eq 1 ]] && ! grep -q '"api_key"' "$conf"; then
    echo "ovx: no stored login for '$name', and the profile has no api_key." >&2
    echo "     Run 'ovx --login $name' if this server wants one." >&2
  fi

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
    vault_logout "$PROFILE" || exit $?
  else
    vault_login "$PROFILE" || exit $?
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
