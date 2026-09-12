"""Conditions under which reflection refuses to run rather than run wrongly."""

from __future__ import annotations

__all__ = ["ContentUnavailableError", "ObservationUnreadableError", "ReflectionError"]


class ReflectionError(RuntimeError):
    """Base for conditions that stop a sweep."""


class ObservationUnreadableError(ReflectionError):
    """The observation standing at a URI could not be read, and may still exist.

    Distinct from "there is no file yet", which is the ordinary case and comes
    back as ``None``. This is raised when the store refused the read for any
    other reason -- unreachable, timed out, not permitted, unparseable.

    The distinction is the difference between writing a first observation and
    destroying one. A missing file means there is nothing to revise, so writing
    the new claim is right. A failed read means *nothing is known* about what is
    there, and a store that treats the two alike overwrites months of
    accumulated reasoning on a network blip.
    """


class ContentUnavailableError(ReflectionError):
    """The index holds no memory text, so quotes cannot be verified against it.

    OpenViking only stores a row's ``content`` when the backend adapter sets
    ``USE_CONTENT_FIELD``; it is ``False`` by default, and ``ov-postgres``
    derives it from ``store_content``. With it off, ``content`` is dropped at
    write time and the only text on the row is ``abstract`` -- a generated
    summary.

    Falling back to ``abstract`` would be quietly self-defeating. The model
    would be shown the summary, quote the summary, and the quote would verify
    against the summary -- while the ``derived_from`` link written from it
    claims a ``match_text`` that does not appear in the memory file it points
    at. That breaks the one OpenViking contract this design leans on, and does
    so invisibly.

    So reflection stops instead, naming the setting to change.
    """
