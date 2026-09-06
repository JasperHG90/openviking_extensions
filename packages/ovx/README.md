# ovx

Run `ov` against a named profile, without leaving an API key on disk.

## Why this exists

The `ov` CLI reads its client settings from `ovcli.conf`, normally
`~/.openviking/ovcli.conf`:

```json
{
  "url": "https://openviking.example.com",
  "api_key": "bGFi.amFzcGVy.ZmI3MTE0YjY1NWVh…"
}
```

That key sits in plain text for as long as the file exists, which is forever.
It lands in backups, in `grep` output, and in anything that walks your home
directory.

`ovx` keeps it out of there. A profile stores a `$VAR` reference instead of the
secret. At launch `ovx` expands it from your environment, writes the result to a
private temporary file, points `$OPENVIKING_CLI_CONFIG_FILE` at it, runs `ov`,
and deletes the file when `ov` exits. Nothing is written to `~/.openviking`.

## Install

`ovx` needs [`python3`](https://www.python.org/) 3.11+ (to read its TOML config)
and the `ov` CLI.

```bash
curl -fsSL https://raw.githubusercontent.com/JasperHG90/openviking_extensions/main/packages/ovx/install.sh | bash
```

That installs the newest stable [release](https://github.com/JasperHG90/openviking_extensions/releases)
to `~/.local/bin`. Pin a version, or pick another directory:

```bash
curl -fsSL .../install.sh | bash -s -- --version 0.1.0
curl -fsSL .../install.sh | bash -s -- --to /usr/local/bin
```

Prereleases are skipped by a bare install, so a beta is something you ask for
by version and never something the one-liner hands you:

```bash
curl -fsSL .../install.sh | bash -s -- --version 0.2.0
```

The `install.sh` attached to a release is pinned to that release, so taking the
pair from a release page gives you that pair.

To install the unreleased tip of `main`, or work from a clone:

```bash
curl -fsSL .../install.sh | bash -s -- --main
./packages/ovx/install.sh --local packages/ovx/ovx.sh
```

`ovx -V` reports the installed version, and the installer prints it too. Those
last two both report **`dev`**, and that is not a bug: the version is stamped
in at release time and lives in the git tag, so a copy taken from the
repository has no version to claim. Install a release if you want a real one.

> On a stock macOS, `python3` is 3.9 and has no `tomllib`. Install a newer one
> (`brew install python@3.12`, or `uv python install`) and put it ahead on
> `PATH`.

## Use

```bash
ovx                    # pick a profile, create one, or edit
ovx lab                # run bare `ov` with profile `lab`
ovx lab find "query"   # run `ov find "query"` with profile `lab`
ovx lab status
ovx -n                 # create a profile, then run ov
ovx -e lab             # edit `lab`, then run ov
ovx -d lab             # delete `lab`, after confirmation
ovx -l                 # list profiles
ovx -L lab             # log in to `lab` through Studio, store the token
ovx --logout lab       # revoke and forget `lab`'s stored login
ovx -V                 # show the ovx version
ovx -- -o json status  # pick a profile, forward `-o json status` to ov
```

Everything after the profile name goes to `ov` untouched, so `ov`'s own
subcommands and flags need no escaping — `ovx lab -o json status` works as
written. A `--` is only needed when no profile name comes first, as in
`ovx -- -o json status`, where `ovx` would otherwise read `-o` as its own
option. One straight after the profile name is allowed and dropped, so
`ovx lab -- -o json status` does the same thing as without it.

`ovx --help` prints the comment block at the top of `ovx.sh`, so the help and
the file's own header are one text rather than two that can disagree. It then
adds the config file actually in force, which the header cannot know. A test
checks every option the parser accepts appears there.

Each run prints a one-line banner to stderr naming the profile, its URL, and a
masked key, so you can see which instance you are about to hit. It is skipped
when stderr is not a terminal, so it never lands in a pipeline.

## Logging in

`ovx -L lab` runs OpenViking's own OAuth 2.1 flow, so a profile can work with
no API key at all:

```
$ ovx -L lab

ovx: approve this login in OpenViking Studio.

  1. Open  https://openviking.example.com/studio/oauth/verify
  2. Enter  K7MPQ2

Waiting for approval (Ctrl-C to abort)…
ovx: logged in. Token stored for profile 'lab'.
```

Open that URL wherever you are already signed in to Studio, type the code, and
`ovx` stores the resulting token at `~/.ovx/tokens/lab.json`, mode `600`. Later
runs use it and refresh it when it expires, so you log in again only when the
refresh token itself runs out. `ovx --logout lab` revokes it server-side and
deletes the file.

A stored token outranks the profile's own `api_key`, so a profile can carry
both — the key stays as a fallback for when you have not logged in.

### Why it polls instead of listening

Most CLI logins open a browser and catch the redirect on `127.0.0.1`. This one
does not, because OpenViking mints the authorization code inside the status
endpoint, and that read *deletes* the pending row — the code is single-use
across readers, not just across exchanges. Studio's consent page polls that
same endpoint in a loop, so pointing you there would race `ovx` and win.

Studio's *verify* page has no pending id and cannot poll, so sending you there
with a code to type leaves `ovx` the only reader. No listener, no port to pick,
no redirect URI that has to be reachable — and it works when the browser is on
a different machine from the terminal.

The six-character code is only rendered into the authorize page's HTML; the
JSON endpoint withholds it deliberately. `ovx` reads it from that page, which
needs no credential and already shows the code to anyone holding the pending
id. That is a seam an upstream redesign could move.

## Config

Profiles live in `~/.ovx/config.toml`, one table per OpenViking instance. The
keys are the keys of `ovcli.conf`:

```toml
[lab]
url = "https://openviking.example.com"
api_key = "$OV_LAB_API_KEY"    # expanded from env at launch
account = "acme"
user = "jasper"

[local]
url = "http://127.0.0.1:1933"  # no api_key: a local server without auth
```

`url` is required; everything else is optional. Set the environment variable in
your shell startup, ideally from a secret store:

```bash
# ~/.zshrc
export OV_LAB_API_KEY="$(security find-generic-password -s openviking-lab -w)"
```

A value containing `$VAR` or `${VAR}` is expanded at launch. An unset or empty
reference is an error, so a missing secret fails loudly instead of running
against a half-built config.

Nothing stops you typing a literal key instead of a `$VAR`, and `ovx` will
happily use it — but then the secret sits in `~/.ovx/config.toml` permanently,
which is the exact problem this tool exists to avoid. The file is created `0600`
inside a `0700` directory, which is better than `~/.openviking/ovcli.conf`
usually manages, but it is still a key at rest. Use a `$VAR`.

Override the config location with `$OVX_CONFIG_FILE` or `$OVX_DIR`.

### Fields

`ovx` accepts every field `ovcli.conf` accepts and checks each one's type before
writing:

| | |
| --- | --- |
| strings | `url`, `api_key`, `root_api_key`, `account`, `user`, `actor_peer_id`, `agent_id`, `output`, `gateway_token`, `auth_mode`, `ldap_username`, `ldap_password` |
| numbers | `timeout` |
| booleans | `profile`, `echo_command`, `show_progress`, `verbose` |
| tables | `upload`, `extra_headers` (or `extra_header`), `plugin` |

The wizard prompts for `url`, `api_key`, `account`, and `user`. Add the rest by
hand; an edit rewrites only what it prompts for and leaves your other fields,
comments, and blank lines exactly where they were.

A key not on this list is an error. `ov`'s Rust CLI ignores fields it does not
recognize, so without this check a typo like `api_kye` would silently leave you
unauthenticated.

## What this does and does not protect

The temporary file is created with mode `600` inside a mode `700` directory, and
removed when `ov` exits — including when `ov` fails, and when you interrupt it
with Ctrl-C. `ov` runs as a child process rather than through `exec`, precisely
so `ovx` survives to clean up.

`SIGINT`, `SIGTERM` and `SIGHUP` each get their own trap, not just `EXIT`. That
is not belt-and-braces: on bash 3.2, which macOS ships, a Ctrl-C arriving while
`ov` holds the foreground sometimes kills the shell *without* running the exit
trap, leaving the key on disk. It is a few percent of interrupts under load —
rare enough to miss on an idle machine, common enough to matter. Each handler
re-raises its signal after cleaning up, so `ovx` still dies from it and a
calling script can tell an interrupt from a failure. A `SIGKILL` cannot be
trapped and does leave the file behind, as it must.

What that buys you is a key on disk for the seconds a command runs instead of
indefinitely. It is not erasure: `rm` unlinks the file, and on an SSD the blocks
may persist until they are reused. If your threat model includes someone
imaging the disk, this is the wrong tool.

A stored OAuth token is the one thing `ovx` does leave on disk, at
`~/.ovx/tokens/<profile>.json`, mode `600`. That is a real trade and worth
naming: a `$VAR` reference keeps nothing, a token keeps something. What you get
back is that the something expires, can be refreshed without you, and can be
revoked from the server — none of which is true of the static key it replaces.
If you would rather keep nothing at all, do not log in; `$VAR` profiles still
work exactly as before.

`ovx` also does not hide the key from the machine while it runs. It is in the
environment you exported it from, and any process running as you can read it.

`ovx` deliberately does not unset inherited `OPENVIKING_*` variables the way
[`clx`](https://github.com/JasperHG90/clx) unsets `ANTHROPIC_*`. It does not need
to: `ovcli.conf` wins over `OPENVIKING_API_KEY`, `OPENVIKING_URL`,
`OPENVIKING_ACCOUNT`, and `OPENVIKING_USER`, so a stale variable cannot redirect
a command or swap the credential.

One thing `ovx` does not redirect: `ov` reads its display language from
`~/.openviking/ovcli.settings.conf` and refuses to run any command until that
file exists. `$OPENVIKING_CLI_CONFIG_FILE` covers `ovcli.conf` only. Run
`ov language en` once; it holds no secret.

## Development

The script is bash; the tests are Python. `ovx` is not a member of the root uv
workspace — its tests drive a script needing Python 3.11+, and `ov-postgres`
still supports 3.10 — so run them scoped to this directory:

```bash
uv run --directory packages/ovx pytest        # unit tests, offline
uv run --directory packages/ovx pytest -m integration   # needs the ov CLI
```

The unit tests put a shim on `PATH` in place of `ov` that records its argv, the
config path it was handed, and that file's contents and permissions. The
integration test runs the real `ov` against a local HTTP server and asserts the
expanded key arrives in the request header.

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
