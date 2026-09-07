"""The one exception type ovx raises, and how it maps to an exit code."""

from __future__ import annotations


class OvxError(Exception):
    """Something ovx cannot do, stated in terms the operator can act on.

    The CLI catches this at the top level and prints ``ovx: <message>`` rather
    than a traceback, so every expected failure reads the same way.

    The exit code is part of the interface: scripts distinguish a bad config
    (2) from a missing profile (3) from an unresolved ``$VAR`` (4), and
    collapsing them to 1 silently broke that.

    Parameters
    ----------
    message :
        What went wrong, as a sentence fragment following "ovx: ".
    hint :
        An optional second line suggesting what to do about it.
    code :
        Exit status. Defaults to 1.

    Attributes
    ----------
    hint : str
        The suggestion, empty when there is none.
    """

    #: Exit code for an ordinary failure.
    code = 1

    def __init__(self, message: str, hint: str = "", code: int | None = None) -> None:
        super().__init__(message)
        self.hint = hint
        if code is not None:
            self.code = code


class UnusableLogin(OvxError):
    """A stored login exists but cannot be used.

    Kept distinct from "there is no login", because the two must not be
    handled the same way. Falling back to the profile's ``api_key`` when a
    login is expired or corrupt would quietly downgrade to a weaker,
    longer-lived credential the operator thought they had stopped using, and
    the only visible sign would be a different identity on the server.
    """

    code = 2
