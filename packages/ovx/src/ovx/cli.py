"""The ovx command line.

Argument handling here is unusual and deliberately so: everything after the
profile name belongs to ``ov``, untouched, so ``ov``'s own subcommands and
flags need no escaping. That means option parsing has to stop at the first
positional word rather than run to the end of the line.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field

import typer

from ovx import __version__, tokens, vault
from ovx.config import load_profile, profile_names
from ovx.errors import OvxError, UnusableLogin
from ovx.fs import ensure_private_dir
from ovx.paths import Locations
from ovx.runner import run_ov


@dataclass(frozen=True)
class Invocation:
    """One command line, split into ovx's part and ov's.

    Attributes
    ----------
    ovx_args : list[str]
        Arguments ovx parses itself.
    ov_args : list[str]
        Arguments forwarded to ov verbatim.
    """

    ovx_args: list[str] = field(default_factory=list)
    ov_args: list[str] = field(default_factory=list)


def split_argv(argv: list[str]) -> Invocation:
    """Split a command line at the point ov's arguments begin.

    The rules, which exist so ``ovx lab -o json status`` works as written:

    - Options are ovx's own until the first bare word, which is the profile.
    - Everything after the profile name is ov's, including things that look
      like ovx options — ``ovx lab -e`` passes ``-e`` to ov.
    - A leading ``--`` means "no profile named; the rest is ov's", for
      ``ovx -- -o json status`` where ``-o`` would otherwise be read as ovx's.
    - A ``--`` straight after the profile name is allowed and dropped, so
      ``ovx lab -- -o json status`` does the same thing as without it.

    Parameters
    ----------
    argv :
        Arguments after the program name.

    Returns
    -------
    Invocation
        The two halves.
    """
    for index, argument in enumerate(argv):
        if argument == "--":
            # Everything after is ov's. Nothing before it named a profile,
            # or we would have stopped at that word already.
            return Invocation(ovx_args=argv[:index], ov_args=argv[index + 1 :])
        if not argument.startswith("-"):
            rest = argv[index + 1 :]
            # Drop one "--" immediately after the profile name.
            if rest and rest[0] == "--":
                rest = rest[1:]
            return Invocation(ovx_args=argv[: index + 1], ov_args=rest)
    return Invocation(ovx_args=list(argv), ov_args=[])


# Every option ovx parses itself. ignore_unknown_options is on so that ov's
# flags pass through untouched, which also means click will not complain about
# a typo'd ovx option -- `ovx --lst lab` would silently be read as a profile
# named "--lst". This is what catches it.
KNOWN_OPTIONS = frozenset(
    {
        "-l",
        "--list",
        "-n",
        "--new",
        "-e",
        "--edit",
        "-d",
        "--delete",
        "-L",
        "--login",
        "--logout",
        "-V",
        "--version",
        "-h",
        "--help",
        "--install-completion",
        "--show-completion",
    }
)


def reject_unknown_options(ovx_args: list[str]) -> None:
    """Refuse an option ovx does not know.

    Raises
    ------
    OvxError
        Naming the offending token and the ``--`` escape hatch, which is how
        an operator forwards an ov flag that looks like one of ovx's.
    """
    for argument in ovx_args:
        if not argument.startswith("-") or argument == "-":
            continue
        if argument.split("=", 1)[0] in KNOWN_OPTIONS:
            continue
        raise OvxError(
            f"unknown option: {argument}",
            "Use -- to forward arguments to ov.",
        )


app = typer.Typer(
    add_completion=True,
    no_args_is_help=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)


def _version_callback(value: bool) -> None:
    """Print the version and exit, for ``-V``."""
    if value:
        typer.echo(f"ovx {__version__}")
        raise typer.Exit


def _resolve_profile(locations: Locations, name: str | None, action: str) -> str:
    """Return the profile to act on, refusing a name that does not exist.

    Parameters
    ----------
    locations :
        Where the config and tokens live.
    name :
        The name given on the command line, if any.
    action :
        Verb used when prompting, e.g. "log in to".

    Returns
    -------
    str
        A profile name that exists.
    """
    from ovx.wizard import pick_existing

    known = profile_names(locations.config_file)
    if name is None:
        return pick_existing(known, action)
    if name not in known:
        listed = ", ".join(known) or "(none)"
        raise OvxError(f"profile {name!r} not found", f"known profiles: {listed}")
    return name


def _login(locations: Locations, name: str) -> None:
    """Mint a Vault identity token for ``name`` and store it."""
    if not vault.address():
        raise OvxError(
            "$VAULT_ADDR is not set, so ovx does not know which Vault to use",
            "export VAULT_ADDR=https://vault.example.com",
        )

    session: str | None = None
    if not vault.session_is_live():
        # Only this branch is interactive: Vault needs a password. A caller
        # that already has a session -- CI with $VAULT_TOKEN set, say -- goes
        # straight to minting and needs no terminal at all.
        if not sys.stdin.isatty() and not _has_tty():
            raise OvxError(
                "no Vault session, and no terminal to log in from",
                "Set $VAULT_TOKEN, or log in where you can type.",
            )
        user = _vault_username(locations, name)
        print(f"ovx: no Vault session; logging in as {user!r}.", file=sys.stderr)
        # Mint with the session this login just returned. Falling back to the
        # ambient one would pick up a stale $VAULT_TOKEN and 403 after the
        # password was already typed.
        session = vault.log_in(user)

    chosen = vault.role()
    token = vault.mint(chosen, token=session)
    ensure_private_dir(locations.root)
    ensure_private_dir(locations.token_dir)
    tokens.save(locations.token_dir, name, token, chosen)
    print(f"ovx: logged in. Token stored for profile {name!r}.", file=sys.stderr)


def _vault_username(locations: Locations, name: str) -> str:
    """Return the Vault username for a profile, prompting when unset.

    The profile's ``user`` field doubles as the Vault username. OpenViking no
    longer takes identity from it — that comes from the token's claims — but it
    still picks the Vault account.
    """
    try:
        user = load_profile(locations.config_file, name).user
    except OvxError:
        user = ""
    if user:
        return user
    answer = str(typer.prompt("Vault username", err=True)).strip()
    if not answer:
        raise OvxError("no Vault username given")
    return answer


def _has_tty() -> bool:
    """Whether a controlling terminal is available for a prompt."""
    # Opened the same way getpass does (read *and* write). getpass falls back
    # to reading stdin -- with echo on -- when it cannot open the tty for
    # writing, so a read-only check here could pass and then let a piped
    # password through, which "it cannot be piped in" promises it will not.
    try:
        handle = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        return False
    os.close(handle)
    return True


def _current_token(locations: Locations, name: str) -> tuple[str, bool]:
    """Return a usable token for a profile, minting a replacement if stale.

    Returns
    -------
    tuple[str, bool]
        The token (empty when the profile has never logged in), and whether a
        login existed at all. The second value matters: no login is ordinary
        and falls back to the profile's ``api_key``, while a *broken* login
        raises instead of downgrading.
    """
    stored = tokens.load(locations.token_dir, name)
    if stored is None:
        return "", False
    if not stored.is_stale():
        return stored.token, True

    # Expiring, so mint a replacement. Meant to be invisible while the Vault
    # session lives; it only speaks up when it cannot be done.
    if not vault.address():
        raise UnusableLogin(
            f"the stored login for {name!r} expired and $VAULT_ADDR is not set",
            "Set it to renew, or set the profile's api_key.",
        )
    try:
        vault.check_session()
    except vault.VaultError as error:
        # Say what actually failed. "Your session is gone" sent the operator to
        # `ovx --login`, which fails identically when the real cause was a bad
        # $VAULT_CACERT or a Vault that is simply unreachable.
        raise UnusableLogin(
            f"the stored login for {name!r} expired and cannot be renewed: {error}",
            f"If your Vault session lapsed, run 'ovx --login {name}'.",
        ) from None
    chosen = stored.role or vault.role()
    try:
        token = vault.mint(chosen)
        tokens.save(locations.token_dir, name, token, chosen)
    except OvxError as error:
        # Still a login that exists and cannot be used, so it must exit 2 and
        # never fall back to the profile's api_key.
        raise UnusableLogin(
            f"the stored login for {name!r} expired and could not be renewed: {error}",
            f"Run 'ovx --login {name}' and try again.",
        ) from None
    return token, True


def _epilog() -> str:
    """Return the trailer for --help.

    The header documents the default config path; this reports the one
    actually in force, which is what an operator with $OVX_CONFIG_FILE set
    needs to see.
    """
    return f"Config in use: {Locations.resolve().config_file}"


@app.command(
    epilog=_epilog(),
    context_settings={
        "allow_extra_args": True,
        "ignore_unknown_options": True,
        # Repeated from the app: a command's context_settings replace the
        # app's rather than merging, so omitting this loses `ovx -h`.
        "help_option_names": ["-h", "--help"],
    },
)
def main(
    ctx: typer.Context,
    profile: str | None = typer.Argument(
        None, help="Profile to run against. Omit to pick one."
    ),
    list_profiles: bool = typer.Option(
        False, "-l", "--list", help="List configured profiles and exit."
    ),
    new: bool = typer.Option(
        False, "-n", "--new", help="Create a profile, then run ov with it."
    ),
    edit: bool = typer.Option(False, "-e", "--edit", help="Edit a profile, then run ov."),
    delete: bool = typer.Option(
        False, "-d", "--delete", help="Delete a profile, after confirmation."
    ),
    login: bool = typer.Option(
        False, "-L", "--login", help="Mint a Vault identity token and store it."
    ),
    logout: bool = typer.Option(
        False, "--logout", help="Remove a profile's stored login."
    ),
    version: bool = typer.Option(
        False,
        "-V",
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show the ovx version and exit.",
    ),
) -> None:
    r"""Run ov against a named profile, without leaving an API key on disk.

    \b
    ov reads its settings from ovcli.conf, normally ~/.openviking/ovcli.conf,
    where the API key sits in plain text for as long as the file exists. ovx
    keeps it out of there: a profile holds a $VAR reference, ovx expands it
    from the environment at launch, writes the result to a private temporary
    file, points $OPENVIKING_CLI_CONFIG_FILE at it, runs ov, and deletes the
    file on the way out.

    \b
    Behavior:
      ovx                    Interactive: pick a profile, create one, or edit
      ovx lab                Run bare 'ov' with profile 'lab'
      ovx lab find "query"   Run 'ov find "query"' with profile 'lab'
      ovx -e lab             Edit profile 'lab' (pre-filled), then run ov
      ovx -d lab             Delete profile 'lab', after confirmation
      ovx -L lab             Log in to 'lab' through Vault, token is stored
      ovx --logout lab       Forget 'lab's stored login
      ovx -- -o json status  Pick a profile, forward '-o json status' to ov

    \b
    Everything after the profile name goes to ov untouched, so ov's own
    subcommands and flags need no escaping: 'ovx lab -o json status' works as
    written. A '--' is only needed when no profile name comes first, as in
    'ovx -- -o json status', where ovx would otherwise read '-o' as its own.
    A '--' straight after the profile name is allowed and dropped.

    \b
    Logging in:
      'ovx -L lab' mints a Vault identity token and stores it under
      ~/.ovx/tokens/lab.json at mode 600. All it needs is $VAULT_ADDR: ovx
      talks to Vault's HTTP API itself and does not require the vault CLI. If
      you already have a session -- $VAULT_TOKEN, or ~/.vault-token from a
      previous 'vault login' -- it just mints; otherwise it asks for your
      password.

    \b
      $VAULT_TOKEN, $VAULT_NAMESPACE, $VAULT_CACERT and $VAULT_SKIP_VERIFY are
      read with their usual meanings. A custom $VAULT_TOKEN_HELPER is not
      supported; set $VAULT_TOKEN instead.

    \b
      The profile's 'user' field doubles as the Vault username when ovx has to
      log in. OpenViking no longer takes identity from 'user' or 'account' --
      it reads the token's claims -- but 'user' still picks the Vault account.

    \b
      The role is 'openviking'; override it with $OVX_VAULT_ROLE. How long a
      token lasts is the role's business, not ovx's: ovx reads the token's own
      'exp' and mints a fresh one as it nears expiry.

    \b
      The stored token outranks the profile's api_key. It expires, which a
      static key does not -- but it is still a credential on disk, and there
      is no server-side revoke for an identity token. 'ovx --logout' deletes
      ovx's copy; a token that leaked before that stays good until it expires.

    \b
    Config: ~/.ovx/config.toml  (override with $OVX_CONFIG_FILE or $OVX_DIR)
    Format: TOML, one [profile] table per OpenViking instance. Keys are the
    keys of ovcli.conf:

    \b
      [lab]
      url = "https://openviking.example.com"
      api_key = "$OV_LAB_API_KEY"   # expanded from env at launch
      account = "acme"
      user = "jasper"

    \b
    'url' is required. A value containing $VAR or ${VAR} is expanded from your
    environment at launch; an unresolved reference is an error, so a missing
    secret fails loudly instead of running against a half-built config.
    """
    from ovx import wizard

    locations = Locations.resolve()
    # ov's arguments were split off before click ran, so they arrive on the
    # context rather than in ctx.args -- click never saw them.
    ov_args: list[str] = list(ctx.obj or [])

    if list_profiles:
        wizard.list_profiles(locations.config_file)
        raise typer.Exit

    if login or logout:
        action = "log in to" if login else "log out of"
        name = _resolve_profile(locations, profile, action)
        if login:
            _login(locations, name)
        elif tokens.forget(locations.token_dir, name):
            print(
                f"ovx: logged out of profile {name!r}. The token expires on its own.",
                file=sys.stderr,
            )
        else:
            print(f"ovx: no stored login for profile {name!r}.", file=sys.stderr)
        raise typer.Exit

    if delete:
        name = _resolve_profile(locations, profile, "delete")
        wizard.delete_profile(locations.config_file, name)
        raise typer.Exit

    # Before the wizard, not after: ovx -n otherwise ran every prompt and
    # wrote a profile before discovering ov was missing.
    if shutil.which("ov") is None:
        raise OvxError("required command not found: ov")

    name = wizard.choose_or_create(locations.config_file, profile, edit=edit, new=new)

    token, had_login = _current_token(locations, name)
    loaded = load_profile(locations.config_file, name)
    code = run_ov(loaded, ov_args, token=token, warn_no_credential=not had_login)
    raise typer.Exit(code)


def entrypoint() -> None:
    """Console-script entry point.

    Splits ov's arguments off before click sees them, then turns any expected
    failure into ``ovx: <message>`` rather than a traceback.
    """
    invocation = split_argv(sys.argv[1:])
    try:
        reject_unknown_options(invocation.ovx_args)
        # standalone_mode=False so OvxError reaches the handler below instead
        # of click turning it into a traceback. In that mode click *returns*
        # the exit code rather than raising SystemExit, so it has to be
        # forwarded by hand -- otherwise ov's exit status is silently lost.
        code = app(
            args=invocation.ovx_args, standalone_mode=False, obj=invocation.ov_args
        )
    except OvxError as error:
        print(f"ovx: {error}", file=sys.stderr)
        if error.hint:
            print(f"     {error.hint}", file=sys.stderr)
        raise SystemExit(error.code) from None
    except (typer.Exit, typer.Abort) as error:
        raise SystemExit(getattr(error, "exit_code", 1)) from None
    except Exception as error:
        # standalone_mode=False re-raises usage errors instead of formatting
        # them, so `ovx --list=x` printed a traceback.
        #
        # Matched on the API rather than the class: typer vendors its own copy
        # of click, so the exception raised here is typer._click's UsageError
        # and `except click.UsageError` never fires -- silently, which is how
        # this survived the first attempt at the fix.
        formatter = getattr(error, "format_message", None)
        if formatter is None:
            raise
        # click says "No such option: --lst"; ovx has always named it as an
        # unknown option and pointed at the -- escape hatch.
        message = str(formatter()).replace("No such option:", "unknown option:")
        print(f"ovx: {message}", file=sys.stderr)
        print("     Use -- to forward arguments to ov.", file=sys.stderr)
        raise SystemExit(1) from None

    # Outside the try: raising SystemExit inside it would make Python evaluate
    # the except clauses against it on the way out.
    raise SystemExit(code if isinstance(code, int) else 0)


__all__ = ["app", "entrypoint", "main", "split_argv"]
