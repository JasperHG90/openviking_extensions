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

```bash
uv tool install ovx --from "git+https://github.com/JasperHG90/openviking_extensions@ovx-v0.3.0#subdirectory=packages/ovx"
```

Or from a checkout:

```bash
uv tool install ./packages/ovx
```

`ovx` needs Python 3.11+ (for `tomllib`) and the `ov` CLI. `uv` supplies the
interpreter, so a stock macOS with its Python 3.9 is no longer a problem.

`ovx -V` reports the version, which comes from the git tag through hatch-vcs.
A checkout with no tag reports a `0.0.0.dev` version rather than failing: the
version lives in the tag, and an untagged tree has none to claim.

> **Upgrading from the shell script.** `ovx` used to be a single bash file
> installed by `curl | bash` into `~/.local/bin`. Remove that copy —
> `rm ~/.local/bin/ovx` — or whichever comes first on `PATH` wins. Your
> `~/.ovx/config.toml` and stored logins carry over untouched.

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
ovx -L lab             # log in to `lab` through Vault, store the token
ovx --logout lab       # forget `lab`'s stored login
ovx -L --bind lab      # log in and bind, without being asked
ovx -L --no-bind lab   # log in and skip the offer
ovx --bind lab         # write `lab` to ov's own config, for other tools
ovx --unbind           # take that config back off disk
ovx -V                 # show the ovx version
ovx -- -o json status  # pick a profile, forward `-o json status` to ov
```

Everything after the profile name goes to `ov` untouched, so `ov`'s own
subcommands and flags need no escaping — `ovx lab -o json status` works as
written. A `--` is only needed when no profile name comes first, as in
`ovx -- -o json status`, where `ovx` would otherwise read `-o` as its own
option. One straight after the profile name is allowed and dropped, so
`ovx lab -- -o json status` does the same thing as without it.

`ovx --help` carries the whole reference — the behavior examples, the Vault
variables, the config format and the `--` rules — not just a one-line summary.
It ends with the config file actually in force, which the static text cannot
know. Two tests keep it honest: one asserts every option `ovx` accepts appears
there, the other that the reference sections have not been quietly dropped.

Each run prints a one-line banner to stderr naming the profile, its URL, and a
masked key, so you can see which instance you are about to hit. It is skipped
when stderr is not a terminal, so it never lands in a pipeline.

## Binding a profile for other tools

The temp-file mechanism is the point of `ovx`, but it has a cost: the
credential exists only while one command runs, so nothing else can reach it.
A bare `ov`, an editor plugin, or an agent has no way to use a profile.

`ovx --bind` trades that away, deliberately:

```bash
ovx --bind lab     # write the profile to ov's own config
ov status          # now works on its own
ovx --unbind       # take it back off disk
```

It writes to `$OPENVIKING_CLI_CONFIG_FILE`, or `~/.openviking/ovcli.conf` when
that is unset — the same file `ov` reads normally. `$VAR` is expanded, because
a bare `ov` does not know what `$OV_LAB_API_KEY` means, and a stored Vault
login wins over the profile's `api_key` exactly as it does for a normal run.
The file is `600`.

Everything this tool exists to avoid is then true again: the credential sits
on disk indefinitely, lands in backups, and shows up in anything that walks
your home directory. `ovx` says so when you bind, and if it wrote a Vault
token it tells you when that expires — a bound token goes stale and needs
`--bind` again.

A successful `ovx --login` offers to bind, because a fresh token is when it
is most worth doing — and wanting a bare `ov` or an agent to reach the profile
is usually why you logged in. The offer defaults to **no**: binding undoes the
one guarantee `ovx` makes, so a stray Enter must not leave a credential on
disk. Pass `--bind` to say yes without being asked, or `--no-bind` to skip the
question. Both go *before* the profile name, since everything after it belongs
to `ov`. If the profile is already bound, a login re-binds without asking, so
the refreshed token actually reaches the file.

`ovx --unbind` removes it. It will only remove a file `ovx` itself wrote:
`ov`'s config path may well have been set up by hand long before `ovx`
existed, and deleting that on your behalf would be an unpleasant surprise.

## Logging in

`ovx -L lab` mints a [Vault](https://developer.hashicorp.com/vault) identity
token, so a profile can work with no API key at all:

```
$ ovx -L lab
ovx: no Vault session.
Vault username [jasper]: operator
Password for operator:
ovx: logged in. Token stored for profile 'lab'.
ovx: bind this profile to ov's config, so a bare 'ov' and other tools can use it?
     The credential then stays on disk until 'ovx --unbind'.
Bind [y/N]:
```

**All it needs is `$VAULT_ADDR`.** `ovx` talks to Vault's HTTP API itself, so the
`vault` CLI is not a dependency — installing a second CLI to run this one would
be a poor trade for the JSON parsing it saves. If you already have a session, it
skips straight to minting and asks nothing:

```
$ ovx -L lab
ovx: logged in. Token stored for profile 'lab'.
```

"Already have a session" means `$VAULT_TOKEN` is set, or `~/.vault-token` holds
a live token — the same file the `vault` CLI caches its own session in, so the
two share one login in either direction. `ovx` writes it there after a
password login, mode `600`.

These are read with their usual meanings:

| | |
| --- | --- |
| `VAULT_ADDR` | required — the Vault to call |
| `VAULT_TOKEN` | session token; wins over the cached file, and suppresses writing it |
| `VAULT_NAMESPACE` | sent as `X-Vault-Namespace` |
| `VAULT_CACERT` | CA bundle for a private CA |
| `VAULT_SKIP_VERIFY` | disable TLS verification |

A custom `$VAULT_TOKEN_HELPER` is **not** supported; set `$VAULT_TOKEN` instead.

The password is read straight from `/dev/tty` with echo off and sent in the
request body, so it never reaches `argv`, the environment, your shell history,
or a log. It cannot be piped in, deliberately.

### A second factor

Where the mount enforces MFA, Vault answers the password with a challenge
instead of a token, and `ovx` asks for the code:

```
$ ovx -L lab
ovx: no Vault session.
Vault username [jasper]: operator
Password for operator:
TOTP passcode:
ovx: logged in. Token stored for profile 'lab'.
```

The passcode is read the same way as the password and validated in its own
request, which carries no session token — it is finishing a login, so there is
no session yet to present. What comes back is an ordinary session token, cached
in the same file, so nothing else about logging in changes.

A method that pushes to a device instead of issuing a code — Duo, Okta, PingID —
is not prompted for. `ovx` says to approve it there and waits:

```
ovx: approve the duo request on your device.
```

Where two enforcements match one login, Vault wants both satisfied. `ovx` can
answer one, so it says which two and stops, rather than answering the first and
failing on an enforcement you were never prompted for. The `vault` CLI refuses
that case as well.

The token lands at `~/.ovx/tokens/lab.json`, mode `600`. It is an RS256 JWT
carrying an `ov_account` claim, which is how the server works out who you are.

> The profile's `user` field is *offered* as the Vault username, not used
> outright. OpenViking stopped reading it for identity — that comes from the
> token's claims — so it drifts, and Vault's userpass is case-sensitive. Using
> it silently meant typing a real password at a prompt for an account that did
> not exist, and getting back a bare "invalid username or password". Press
> Enter to accept it, or type the right one.

A stored token outranks the profile's own `api_key`, so a profile can carry
both; the key stays as a fallback for when you have not logged in.

`ovx --logout lab` deletes the local file. There is nothing to revoke — see
below.

### Renewal, and why there is no refresh token

An identity token cannot be refreshed: it is a signed assertion, not a session,
so there is no grant to exchange. `ovx` re-mints instead. Every run checks the
stored token, and within five minutes of expiry asks Vault for a new one before
handing it to `ov`.

How long a token lasts is set on the Vault role, not by `ovx`:

```bash
vault read  identity/oidc/role/openviking          # what it grants today
vault write identity/oidc/role/openviking ttl=168h # change it
```

`ovx` never assumes that number. It reads the `exp` the token itself carries,
so changing the role's `ttl` takes effect on the next mint with no change here.
To see what you were actually granted, decode a stored token's `exp` from
`~/.ovx/tokens/<profile>.json` — it is the real answer, and it is worth
checking, since it is also the whole of your exposure window (see below).

Re-minting needs a live Vault session rather than a stored secret — which is
the point. The renewable thing is your Vault login, which lives in Vault's own
token helper, not a refresh token sitting in `~/.ovx`. With a long `ttl` the
token will usually outlive the Vault session that minted it; that is fine,
since the token stands on its own. You only need Vault again when the token
nears expiry, and if the session has lapsed by then `ovx` says to run `vault
login` rather than failing with a bare 401.

### Why no revoke

`ovx --logout` deletes the local copy and stops there. Nothing tracks an
identity token server-side, so there is no revocation endpoint to call, and
`vault token revoke -self` would destroy your whole Vault session — taking
every other tool on the machine with it.

So the token stays valid until it expires, however long the role grants. If you
need one dead sooner, the only lever is the Vault side: rotate or revoke the
entity's access. Check what your role actually grants before trusting
`--logout` to mean anything.

### How it reaches the server

`ov` has no field for a bearer token and needs none. OpenViking's
`_extract_token` treats an `api_key` containing exactly two dots as a JWT, and
a Vault identity token has exactly two dots — so it travels in the same
`api_key` slot `ovx` already writes, and the transport is unchanged.

`ov` also copies anything with two or more dots into an `Authorization: Bearer`
header. Both carry the same token and the server reads either, so the
duplication is harmless.

### The role

Tokens are minted from `identity/oidc/token/openviking`. Override the role name
with `$OVX_VAULT_ROLE`. The role fixes the audience and the `ov_account` claim,
so it has to match what the server was configured against.

`ovx` does not check the token's `iss`. It must match the server's
configuration exactly, and it is set on the Vault side — validating it here
would only add a second place to get it wrong.

### Lending the token to Firefox

[`ov-clip`](../ov-clip/) saves web pages into OpenViking, and it needs the same
token. It cannot read `~/.ovx/tokens/<profile>.json`: a Firefox extension has
no filesystem access — no read API, and unlike Chrome it cannot be granted host
permissions for `file://`. So `ovx` answers over native messaging instead.

```bash
ovx --install-firefox-host    # register the host with Firefox
ovx --uninstall-firefox-host  # take it away again
```

The first writes a small manifest —
`~/Library/Application Support/Mozilla/NativeMessagingHosts/ovx.json` on macOS,
`~/.mozilla/native-messaging-hosts/ovx.json` on Linux — pointing at the
`ovx-firefox-host` console script. Restart Firefox for it to be noticed.
Windows keeps these in the registry rather than on disk, so it is not supported
and says so rather than pretending.

`allowed_extensions` in that manifest names `ov-clip@openviking` and nothing
else, which is the boundary: no other add-on on the machine can start the host,
so none can ask for your token.

The host answers three things — `ping`, `profiles`, and `token` — and hands
back a minted identity token with the profile's `url`. It never hands back a
profile's `api_key`, even when there is no login to serve: falling back to the
static key would be exactly the silent downgrade to a longer-lived credential
that `ovx` exists to stop. A profile with no login gets an error saying to run
`ovx --login` instead.

Renewal is shared with the CLI rather than reimplemented, so the browser and
`ov` cannot drift into treating an expiring token differently.

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

A stored identity token is the one thing `ovx` does leave on disk, at
`~/.ovx/tokens/<profile>.json`, mode `600`. That is a real trade and worth
naming plainly: a `$VAR` reference keeps nothing, a token keeps something.

What you get back is that the something expires on its own, which a static key
never does. What you do *not* get back is revocation — there is none for an
identity token. A leaked token is usable by whoever holds it until it expires,
and deleting your copy with `--logout` does nothing about that.

So the role's `ttl` is the whole of your exposure window — read it off a stored
token's `exp` rather than assuming, because it is easy to set generously and
there is no revoke to fall back on. Weigh it against the `api_key` it removes:
the key is worse, since it never expires at all, but at a long `ttl` the gap is
narrow. If you would rather keep nothing at all, do not log
in; `$VAR` profiles still work exactly as before.

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

```bash
uv run --directory packages/ovx pytest        # its own project, its own lock
uv run --directory packages/ovx ovx --help    # run it in place
```

The suite drives the installed console entrypoint as a subprocess, so it tests
what an operator actually runs rather than the functions underneath. A shim on
`PATH` stands in for `ov` and records the config it was handed; a fake Vault
HTTP server stands in for Vault. Interactive paths are driven through a pty.

`tests/test_units.py` covers what the subprocess tests reach only through their
happy paths — file modes, temp-directory cleanup, `$VAR` expansion, argv
splitting and the exit-code contract. Those were added after a review mutated
the source and found 24 of 29 changes left the suite green.

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
