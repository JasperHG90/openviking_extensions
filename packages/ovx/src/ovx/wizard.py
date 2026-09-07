"""The interactive parts: listing, picking, creating, editing, deleting.

Every prompt here reads from the terminal rather than stdin, so a profile is
never chosen by whatever happens to be piped in. Editing rewrites only the
fields it prompted for, leaving hand-added fields, comments and blank lines
exactly where they were.
"""

from __future__ import annotations

import getpass
import os
import re
import sys
from pathlib import Path

from ovx.config import (
    FIELD_TYPES,
    WIZARD_FIELDS,
    load_document,
    profile_names,
)
from ovx.errors import OvxError
from ovx.fs import ensure_private_dir, write_private

# Fields whose value is a credential. Their current value is never displayed
# and what you type is never echoed -- an edit otherwise puts the key in
# scrollback, in `script` output, and in any terminal recording.
SECRET_FIELDS = frozenset({"api_key", "root_api_key", "gateway_token", "ldap_password"})


def ask(prompt: str, *, secret: bool = False, default: str = "") -> str:
    """Ask on the controlling terminal, never on stdin.

    typer.prompt reads stdin, so `yes | ovx -d prod` would answer its own
    confirmation and delete the profile unattended. Reading /dev/tty directly
    is what the shell version did and what makes the prompts un-pipeable.
    """
    if secret:
        # No stream argument: getpass opens /dev/tty itself, with echo off.
        # Handing it a text-mode "r+" handle raises "not seekable" on a
        # character device, which crashed every prompt in the wizard.
        #
        # Not stripped. A password is whatever was typed, trailing space and
        # all, and silently trimming one turns a correct password into
        # "invalid username or password" with nothing to see. Only the empty
        # answer means "keep the current value", which is why the emptiness
        # test is on the raw string.
        typed = getpass.getpass(f"{prompt}: ")
        return typed if typed else default

    shown = f" [{default}]" if default else ""
    # Two handles rather than one "r+": the same not-seekable problem.
    with open("/dev/tty", "w") as out:
        out.write(f"{prompt}{shown}: ")
        out.flush()
    with open("/dev/tty") as inp:
        answer = inp.readline()
    if not answer:
        raise OvxError("no answer given (input ended)")
    return answer.strip() or default


def _confirm(question: str) -> bool:
    """Ask a yes/no question on the controlling terminal."""
    return ask(f"{question} [y/N]").lower() in ("y", "yes")


def require_tty() -> None:
    """Refuse cleanly when there is no terminal to prompt on.

    Raises
    ------
    OvxError
        When ``/dev/tty`` cannot be opened.
    """
    try:
        handle = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    except OSError:
        raise OvxError(
            "this action needs an interactive terminal (no controlling tty)"
        ) from None
    os.close(handle)


def list_profiles(config_file: Path) -> None:
    """Print every configured profile with its URL.

    In file order, not sorted, and with the name in its ``[brackets]`` table
    form. This output is what people grep and what they diff against their
    config, so both were load-bearing. "No config" and "no profiles in it" are
    also different situations and say so separately.
    """
    if not config_file.is_file():
        print(f"ovx: no config at {config_file}")
        return
    document = load_document(config_file)
    names = [name for name, value in document.items() if isinstance(value, dict)]
    if not names:
        print(f"ovx: no profiles defined in {config_file}")
        return
    print(f"Profiles in {config_file}:")
    for name in names:
        section = document[name]
        url = section.get("url", "") if isinstance(section, dict) else ""
        print(f"  [{name}]  {url}")


def pick_existing(names: list[str], action: str) -> str:
    """Ask which profile to act on.

    Parameters
    ----------
    names :
        Profiles to choose from.
    action :
        Verb for the prompt, e.g. "delete".

    Returns
    -------
    str
        The chosen name.

    Raises
    ------
    OvxError
        If there is nothing to choose from, or no terminal to ask on.
    """
    if not names:
        raise OvxError(f"no profiles to {action}")
    require_tty()
    print(f"ovx: {action} which profile?", file=sys.stderr)
    for index, name in enumerate(names, start=1):
        print(f"  {index}) [{name}]", file=sys.stderr)
    while True:
        answer = ask(f"{action} which")
        if answer in names:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        print(f"  no profile named {answer!r}", file=sys.stderr)


def choose_or_create(
    config_file: Path, profile: str | None, *, edit: bool, new: bool
) -> str:
    """Resolve which profile to run, creating or editing one when asked.

    Parameters
    ----------
    config_file :
        The ovx config.
    profile :
        Name given on the command line, if any.
    edit :
        Edit the profile before running.
    new :
        Create a profile before running.

    Returns
    -------
    str
        The profile to run against.
    """
    names = profile_names(config_file)

    if new:
        return create_profile(config_file, profile)
    if edit:
        # A named profile must exist. Falling back to the picker turned a typo
        # into a menu, unlike -d/-L/--logout which all refuse.
        if profile is not None and profile not in names:
            listed = ", ".join(names) or "(none)"
            raise OvxError(f"profile {profile!r} not found", f"known profiles: {listed}")
        name = profile or pick_existing(names, "edit")
        edit_profile(config_file, name)
        return name
    if profile is not None:
        if profile not in names:
            listed = ", ".join(names) or "(none)"
            raise OvxError(f"profile {profile!r} not found", f"known profiles: {listed}")
        return profile
    if not names:
        print(
            f"ovx: no profiles found at {config_file} — let's create one.",
            file=sys.stderr,
        )
        return create_profile(config_file, None)
    return choose_profile(config_file)


def choose_profile(config_file: Path) -> str:
    """Show the profile menu and return the chosen name.

    The menu is not just a list: it also offers create, edit and delete, which
    is the only way to reach those without knowing the flags. ``--help`` says
    "Interactive: pick a profile, create one, or edit", so dropping them made
    the help a lie.

    Parameters
    ----------
    config_file :
        The ovx config, re-read after a delete so a removed profile does not
        linger in the menu.

    Returns
    -------
    str
        The profile to run against.
    """
    require_tty()
    while True:
        names = profile_names(config_file)
        if not names:
            return create_profile(config_file, None)

        print("", file=sys.stderr)
        print("Available profiles:", file=sys.stderr)
        for index, name in enumerate(names, start=1):
            print(f"  {index:2d}) {name}", file=sys.stderr)
        create_at = len(names) + 1
        edit_at = create_at + 1
        delete_at = create_at + 2
        print(f"  {create_at:2d}) create a new profile", file=sys.stderr)
        print(f"  {edit_at:2d}) edit an existing profile", file=sys.stderr)
        print(f"  {delete_at:2d}) delete an existing profile", file=sys.stderr)

        answer = ask("Choose")
        if not answer:
            print("  empty input", file=sys.stderr)
            continue

        if answer.isdigit():
            # Leading zeros stripped so "08" reads as 8 rather than octal.
            index = int(answer.lstrip("0") or "0")
            if index == create_at:
                return create_profile(config_file, None)
            if index == edit_at:
                target = pick_existing(names, "edit")
                edit_profile(config_file, target)
                return target
            if index == delete_at:
                delete_profile(config_file, pick_existing(names, "delete"))
                continue
            if 1 <= index <= len(names):
                return names[index - 1]
            print("  out of range", file=sys.stderr)
            continue

        if answer in names:
            return answer
        print(f"  no profile named {answer!r}", file=sys.stderr)


def _prompt_fields(defaults: dict[str, str]) -> dict[str, str]:
    """Prompt for the wizard's fields, offering ``defaults``."""
    require_tty()
    answers: dict[str, str] = {}
    for name in WIZARD_FIELDS:
        current = defaults.get(name, "")
        if name in SECRET_FIELDS:
            # Never shown, never echoed: the current value IS the secret. The
            # wording says whether one is already set without revealing it.
            prompt = (
                f"{name} (literal or $VAR) [Enter to keep current]"
                if current
                else f"{name} (literal or $VAR, blank for none)"
            )
            answers[name] = ask(prompt, secret=True, default=current)
        elif name == "url":
            # The one required field, so keep asking rather than writing a
            # profile that load_profile would then refuse.
            while True:
                answers[name] = ask(name, default=current)
                if answers[name]:
                    break
                print("  url required", file=sys.stderr)
        else:
            answers[name] = ask(name, default=current)
    return answers


NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# ovx's own default, for a server started with no arguments.
DEFAULT_URL = "https://127.0.0.1:1933"


def _validate_name(name: str, existing: list[str]) -> str | None:
    """Return why ``name`` is unusable, or ``None`` when it is fine.

    The rules are not decoration. A name with a path separator cannot have a
    token file (``tokens.token_path`` refuses it), so ovx would advise
    ``ovx --login <name>`` and then refuse that very command. A purely numeric
    name shadows a menu index, making the profile unselectable by number.
    """
    if not name:
        return "name required"
    if not NAME_PATTERN.match(name):
        return "use only letters, digits, underscore, dash"
    if name.isdigit():
        return "a purely-numeric name clashes with menu indices — add a letter"
    if name in existing:
        return f"'{name}' already exists — choose another"
    return None


def create_profile(config_file: Path, name: str | None) -> str:
    """Create a profile interactively and append it to the config.

    Returns
    -------
    str
        The new profile's name.
    """
    require_tty()
    existing = profile_names(config_file)
    print("", file=sys.stderr)
    print("Create a new profile.", file=sys.stderr)

    while True:
        # A name on the command line is the *default*, not the answer:
        # `ovx -n lab` offers "lab" and still lets you type something else.
        chosen = ask("Profile name", default=name or "")
        complaint = _validate_name(chosen, existing)
        if complaint is None:
            break
        print(f"  {complaint}", file=sys.stderr)

    print(
        "  Tip: give api_key as $VAR to keep the secret out of the file.",
        file=sys.stderr,
    )
    answers = _prompt_fields({"url": DEFAULT_URL})
    ensure_private_dir(config_file.parent)
    _append_profile(config_file, chosen, answers)
    print(f"ovx: wrote profile '{chosen}' to {config_file}", file=sys.stderr)
    return chosen


def edit_profile(config_file: Path, name: str) -> None:
    """Rewrite one profile's wizard fields, leaving everything else alone."""
    document = load_document(config_file)
    section = document.get(name)
    if not isinstance(section, dict):
        raise OvxError(f"profile {name!r} not found")
    defaults = {k: str(section.get(k, "")) for k in WIZARD_FIELDS}
    answers = _prompt_fields(defaults)
    _rewrite_profile(config_file, name, answers)
    print(f"ovx: updated profile '{name}' in {config_file}", file=sys.stderr)


def delete_profile(config_file: Path, name: str) -> None:
    """Remove a profile from the config, after confirmation."""
    require_tty()
    if not _confirm(f"Delete profile '{name}'? This can't be undone."):
        print("ovx: cancelled; nothing was deleted.", file=sys.stderr)
        return
    _rewrite_profile(config_file, name, None)
    print(f"ovx: deleted profile {name!r}.", file=sys.stderr)


def _quote(value: str) -> str:
    """Return ``value`` as a TOML basic string.

    Newlines and tabs are escaped, not passed through: a profile name typed
    with an embedded newline otherwise wrote ``["a`` and left a config that
    tomllib could not read.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _write_config(config_file: Path, text: str) -> None:
    """Write the config at mode 0600, replacing atomically.

    The file may hold a literal api_key -- ovx discourages that but does not
    forbid it -- so it must never exist world-readable, not even briefly.
    ``Path.write_text`` creates at the umask default and a later chmod leaves
    exactly that window.
    """
    write_private(config_file, text)


def _header_name(line: str) -> str | None:
    """Return the table name a line declares, or ``None`` if it declares none.

    A trailing comment is stripped, so ``[lab] # the lab`` is still ``lab``;
    the shell version did this and the port refusing it made ``ovx -e`` fail
    *after* prompting for everything.
    """
    stripped = line.strip()
    if not stripped.startswith("["):
        return None
    closing = stripped.find("]")
    if closing == -1:
        return None
    return stripped[1:closing].strip().strip('"').strip("'")


def _append_profile(config_file: Path, name: str, fields: dict[str, str]) -> None:
    """Append a ``[name]`` table to the config file."""
    lines = [f"[{_quote(name)}]"]
    lines += [f"{k} = {_quote(v)}" for k, v in fields.items() if v]
    body = "\n".join(lines) + "\n"
    existing = config_file.read_text() if config_file.is_file() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    separator = "" if not existing or existing.endswith("\n\n") else "\n"
    _write_config(config_file, existing + separator + body)


def _rewrite_profile(config_file: Path, name: str, fields: dict[str, str] | None) -> None:
    """Replace or remove one table, preserving the rest of the file.

    A hand-edited config carries comments, ordering and fields the wizard does
    not prompt for. Reserializing the parsed document would discard all of it,
    so the file is edited as text.

    Lines are split without their endings and rejoined with a trailing
    newline, which normalizes a file that did not end in one -- keeping the
    endings produced ``...[lab]url = "..."`` on such a file, leaving a config
    tomllib could no longer parse.

    Parameters
    ----------
    config_file :
        The config to rewrite.
    name :
        Table to replace.
    fields :
        New wizard fields, or ``None`` to delete the table.
    """
    text = config_file.read_text() if config_file.is_file() else ""
    lines = text.splitlines()

    start = None
    end = len(lines)
    for index, line in enumerate(lines):
        header = _header_name(line)
        if header is None:
            continue
        if header == name and start is None:
            start = index
        elif start is not None:
            end = index
            break
    if start is None:
        # tomllib reports ["prod".eu] as a profile named "prod", but there is
        # no plain [prod] header to rewrite. Say so rather than "not found",
        # which would read as a typo.
        raise OvxError(
            f"profile {name!r} is not a plain [{name}] section in {config_file}",
            "ovx only rewrites plain tables; edit this one by hand.",
        )

    replacement: list[str] = []
    if fields is not None:
        replacement = [lines[start]]
        remaining = dict(fields)
        # Rewrite each field where it already sits, so a comment above a key
        # stays above that key rather than sliding to the end of the table.
        for line in lines[start + 1 : end]:
            key = _assigned_key(line)
            if key in remaining:
                value = remaining.pop(key)
                if value:
                    replacement.append(f"{key} = {_quote(value)}")
                continue
            replacement.append(line)
        # Anything that was not already present goes just after the header.
        insert = [f"{k} = {_quote(v)}" for k, v in remaining.items() if v]
        replacement[1:1] = insert
    else:
        # Deleting also drops the blank line that separated this table from
        # the next, so repeated edits do not accumulate empty lines.
        while end < len(lines) and not lines[end].strip():
            end += 1

    kept = lines[:start] + replacement + lines[end:]
    body = "\n".join(kept)
    _write_config(config_file, body + "\n" if body else "")


def _assigned_key(line: str) -> str | None:
    """Return the key a line assigns, or ``None`` when it assigns none."""
    match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
    return match.group(1) if match else None


__all__ = [
    "FIELD_TYPES",
    "ask",
    "choose_or_create",
    "choose_profile",
    "create_profile",
    "delete_profile",
    "edit_profile",
    "list_profiles",
    "pick_existing",
    "require_tty",
]
