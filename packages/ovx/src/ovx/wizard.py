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
        return getpass.getpass(f"{prompt}: ").strip() or default

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

    The name is shown in its ``[brackets]`` table form, and the path is
    announced first. Both are load-bearing: this output is what people grep,
    and dropping either silently broke their scripts.
    """
    document = load_document(config_file)
    names = profile_names(config_file)
    if not names:
        print(f"ovx: no profiles found at {config_file}")
        return
    print(f"Profiles in {config_file}:")
    bracketed = {name: f"[{name}]" for name in names}
    width = max(len(shown) for shown in bracketed.values())
    for name in names:
        section = document[name]
        url = section.get("url", "") if isinstance(section, dict) else ""
        print(f"  {bracketed[name]:<{width}}  {url}")


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
    return choose_profile(names)


def choose_profile(names: list[str]) -> str:
    """Show the profile menu and return the chosen name.

    Separate from :func:`pick_existing` because this is the no-arguments
    entry point, where the prompt is a bare "Choose: " rather than a verb.
    """
    require_tty()
    print("ovx: profiles", file=sys.stderr)
    for index, name in enumerate(names, start=1):
        print(f"  {index}) [{name}]", file=sys.stderr)
    while True:
        answer = ask("Choose")
        if answer in names:
            return answer
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
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
        else:
            answers[name] = ask(f"{name} [{current}]", default=current)
    if not answers.get("url"):
        raise OvxError("a profile needs a url")
    return answers


def create_profile(config_file: Path, name: str | None) -> str:
    """Create a profile interactively and append it to the config.

    Returns
    -------
    str
        The new profile's name.
    """
    require_tty()
    existing = profile_names(config_file)
    # A name on the command line is the *default*, not the answer: `ovx -n lab`
    # offers "lab" and still lets you type something else, which is what the
    # shell version did.
    suffix = f" [{name}]" if name else ""
    chosen = ask(f"Profile name{suffix}", default=name or "")
    if not chosen:
        raise OvxError("a profile needs a name")
    if chosen in existing:
        raise OvxError(f"profile {chosen!r} already exists")

    answers = _prompt_fields({})
    ensure_private_dir(config_file.parent)
    _append_profile(config_file, chosen, answers)
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
    "ask",
    "FIELD_TYPES",
    "choose_or_create",
    "choose_profile",
    "create_profile",
    "delete_profile",
    "edit_profile",
    "list_profiles",
    "pick_existing",
    "require_tty",
]
