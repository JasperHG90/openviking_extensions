"""Logging in to a Vault that enforces a second factor.

Vault answers an MFA-enforced login with HTTP 200 and an ``mfa_requirement``
where the token would be. A client that only looks for a token reads that as
"the password was wrong" — a message pointing at the one layer that was fine.
These pin the fork on the body, where the requirement actually sits, and the
exact shape of the validate call, because every part of that shape fails in a
way that reads like a wrong code.

The fake Vault answers with the shape Vault documents: ``auth`` is a real
object holding an empty ``client_token``, and ``mfa_requirement`` sits inside
it. A first pass at this read the requirement from the top level instead, and a
fake built to match agreed with it — so the whole suite passed against a
response Vault never sends. It is a real HTTP server rather than a patched
``request`` because the payload shape and the absent session token are the
things most likely to be wrong, and only a server that parses the request can
see them.
"""

from __future__ import annotations

import getpass
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest

from ovx import vault

METHOD_ID = "d4e0e6f9-3b2a-4f1c-8e7d-5a9b0c1d2e3f"
REQUEST_ID = "e0e5f3a1-1f2c-4c8f-9a6b-9d2a0c7f5b31"
PASSCODE = "123456"
SESSION = "s.after-the-second-factor"


def _requirement(enforcement: str, *, uses_passcode: bool = True) -> dict[str, Any]:
    """Build an ``mfa_requirement`` the way Vault sends one."""
    method: dict[str, Any] = {
        "type": "totp" if uses_passcode else "duo",
        "id": METHOD_ID,
    }
    # Vault marshals the flag with omitempty, so a false one is simply absent.
    if uses_passcode:
        method["uses_passcode"] = True
    return {
        "mfa_request_id": REQUEST_ID,
        "mfa_constraints": {enforcement: {"any": [method]}},
    }


class _MfaVault(BaseHTTPRequestHandler):
    """A Vault with MFA enforced on the userpass mount.

    Attributes
    ----------
    enforcement : str
        Name of the login enforcement, which is the key ``mfa_constraints``
        uses. Tests rename it to prove nothing depends on the name.
    second_enforcement : str
        A second enforcement to match the same login, which Vault wants
        satisfied as well. Empty for the ordinary single-factor case.
    uses_passcode : bool
        Whether the enrolled method is answered with a code.
    nested : bool
        Where the requirement goes. Vault nests it in ``auth``; the top-level
        variant is accepted too, and one test pins that.
    challenge : bool
        When false, the login answers 200 with no token and no challenge —
        a login that really did fail.
    validate_delay : float
        Seconds to hold the validate request open, as Vault does while it polls
        a push provider.
    seen : list
        Every request as ``(path, X-Vault-Token, body)``.
    """

    enforcement: ClassVar[str] = "operator-totp"
    second_enforcement: ClassVar[str] = ""
    uses_passcode: ClassVar[bool] = True
    nested: ClassVar[bool] = True
    challenge: ClassVar[bool] = True
    validate_delay: ClassVar[float] = 0.0
    seen: ClassVar[list[tuple[str, str | None, dict[str, Any]]]] = []

    def _json(self, status: int, body: dict[str, Any]) -> None:
        """Answer with ``body`` as JSON."""
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _login(self) -> None:
        """Answer a password with a challenge, as an enforced mount does."""
        cls = type(self)
        # Vault sends a populated auth block with an empty client_token, not a
        # null one.
        auth: dict[str, Any] = {"client_token": "", "accessor": "", "num_uses": 0}
        if not cls.challenge:
            self._json(200, {"auth": auth})
            return
        requirement = _requirement(cls.enforcement, uses_passcode=cls.uses_passcode)
        if cls.second_enforcement:
            requirement["mfa_constraints"][cls.second_enforcement] = {
                "any": [{"type": "duo", "id": "a-second-method"}]
            }
        body: dict[str, Any] = {
            "warnings": ["A login request was issued that is subject to MFA validation."]
        }
        if cls.nested:
            auth["mfa_requirement"] = requirement
        else:
            body["mfa_requirement"] = requirement
        self._json(200, {**body, "auth": auth})

    def _validate(self, body: dict[str, Any]) -> None:
        """Issue a token for the right answer, and refuse anything else."""
        cls = type(self)
        # A pushed factor holds the request open while Vault polls the provider.
        time.sleep(cls.validate_delay)
        expected = [PASSCODE] if cls.uses_passcode else []
        payload = body.get("mfa_payload") or {}
        if body.get("mfa_request_id") == REQUEST_ID and payload.get(METHOD_ID) == (
            expected
        ):
            self._json(200, {"auth": {"client_token": SESSION}})
            return
        # What Vault answers a bad code with: permission denied, not a 400.
        self._json(403, {"errors": [f"failed to satisfy enforcement {cls.enforcement}"]})

    def do_POST(self) -> None:
        """Route the two calls a login makes, recording both."""
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen.append((self.path, self.headers.get("X-Vault-Token"), body))

        if self.path.startswith("/v1/auth/userpass/login/"):
            self._login()
        elif self.path == "/v1/sys/mfa/validate":
            self._validate(body)
        else:
            self._json(404, {"errors": ["no handler"]})

    def log_message(self, *args: object) -> None:
        """Stay quiet."""


@pytest.fixture
def mfa_vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point ovx at a fresh MFA-enforcing Vault, with its own token file."""
    _MfaVault.enforcement = "operator-totp"
    _MfaVault.second_enforcement = ""
    _MfaVault.uses_passcode = True
    _MfaVault.nested = True
    _MfaVault.challenge = True
    _MfaVault.validate_delay = 0.0
    _MfaVault.seen = []
    server = HTTPServer(("127.0.0.1", 0), _MfaVault)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("VAULT_ADDR", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("VAULT_TOKEN_FILE", str(tmp_path / "vault-token"))
    monkeypatch.delenv("VAULT_TOKEN", raising=False)
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def no_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything asks the terminal for a passcode."""

    def refuse(*args: object, **kwargs: object) -> str:
        raise AssertionError("prompted for a passcode with no code to type")

    monkeypatch.setattr(getpass, "getpass", refuse)


def _validates() -> list[tuple[str, str | None, dict[str, Any]]]:
    """Return the validate calls the fake Vault received."""
    return [call for call in _MfaVault.seen if call[0] == "/v1/sys/mfa/validate"]


def test_a_challenge_finishes_the_login(mfa_vault: None, tmp_path: Path) -> None:
    """A 200 with no token must be answered, not reported as a bad password."""
    session = vault.log_in("jasper", password="hunter2", passcode=PASSCODE)

    assert session == SESSION
    # An ordinary login token, so it is cached like one and mints like one.
    assert (tmp_path / "vault-token").read_text() == SESSION


def test_the_requirement_is_read_from_inside_auth(mfa_vault: None) -> None:
    """Vault nests it in ``auth``, beside an empty ``client_token``.

    Reading it from the top level instead left the fix inert: the challenge was
    never found, and the login still failed claiming no token came back.
    """
    assert _MfaVault.nested, "the fake must send the shape Vault sends"

    assert vault.log_in("jasper", password="hunter2", passcode=PASSCODE) == SESSION


def test_a_top_level_requirement_is_answered_too(mfa_vault: None) -> None:
    """Looking in one place is how this bug started; the second check is free."""
    _MfaVault.nested = False

    assert vault.log_in("jasper", password="hunter2", passcode=PASSCODE) == SESSION


def test_the_passcode_is_a_list_keyed_by_the_method_id(mfa_vault: None) -> None:
    """A bare string is refused in a way that reads like a wrong code."""
    vault.log_in("jasper", password="hunter2", passcode=PASSCODE)

    _, _, body = _validates()[0]
    assert body == {
        "mfa_request_id": REQUEST_ID,
        "mfa_payload": {METHOD_ID: [PASSCODE]},
    }


def test_the_validate_call_sends_no_session_token(
    mfa_vault: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It completes a login; a stale cached token has no business on it."""
    monkeypatch.setenv("VAULT_TOKEN", "s.dead")

    assert vault.log_in("jasper", password="hunter2", passcode=PASSCODE) == SESSION
    assert [token for _, token, _ in _MfaVault.seen] == [None, None]


def test_the_enforcement_name_is_not_hardcoded(mfa_vault: None) -> None:
    """``mfa_constraints`` is keyed by the enforcement, which the operator names.

    Keying on ``operator-totp`` would break login the day it is renamed or a
    second enforcement is added — and it would break it by blaming the
    password.
    """
    _MfaVault.enforcement = "renamed-after-the-fact"

    assert vault.log_in("jasper", password="hunter2", passcode=PASSCODE) == SESSION


def test_a_pushed_factor_is_not_a_passcode_prompt(
    mfa_vault: None, no_prompt: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """Duo, Okta and PingID push to a device; there is no code to type.

    Vault's field takes an empty slice for those, so prompting would ask for
    something that does not exist and then refuse the empty answer — blaming
    the operator for a method ovx read wrong.
    """
    _MfaVault.uses_passcode = False

    assert vault.log_in("jasper", password="hunter2") == SESSION

    _, _, body = _validates()[0]
    assert body["mfa_payload"] == {METHOD_ID: []}
    # Otherwise ovx sits silent while Vault waits on a phone nobody knows to
    # pick up.
    assert "approve the duo request" in capsys.readouterr().err


def test_a_pushed_factor_waits_longer_than_a_normal_call(
    mfa_vault: None, no_prompt: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Vault polls the provider for minutes; 30 seconds would cut a login short.

    Losing the race is worse than waiting: Vault mints the token when the
    operator taps, and ovx would already have stopped listening and reported a
    failed login.
    """
    _MfaVault.uses_passcode = False
    _MfaVault.validate_delay = 0.4
    monkeypatch.setattr(vault, "PUSH_TIMEOUT", 0.1)

    with pytest.raises(vault.VaultError, match="second factor"):
        vault.log_in("jasper", password="hunter2")


def test_a_typed_passcode_does_not_wait_on_the_push_window(
    mfa_vault: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The long window belongs to a pushed factor alone, not to every call."""
    _MfaVault.validate_delay = 0.4
    monkeypatch.setattr(vault, "PUSH_TIMEOUT", 0.1)

    assert vault.log_in("jasper", password="hunter2", passcode=PASSCODE) == SESSION


def test_only_the_pushed_call_gets_the_long_window(
    mfa_vault: None, no_prompt: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The password POST keeps the ordinary timeout, whatever the factor is.

    Nothing else pins this: only the validate call is ever slow in these tests,
    so putting ``PUSH_TIMEOUT`` on the login call as well passed the whole
    suite — and left a hung Vault holding a password for five minutes.
    """
    asked: list[tuple[str, float]] = []
    real = vault.request

    # Spelled out rather than **kwargs: a new argument on request then raises
    # TypeError here and fails this test, where a **kwargs spy would swallow it
    # and record nothing. mypy cannot see the drift -- monkeypatch.setattr takes
    # the replacement as object, so the two signatures are never compared.
    def spy(
        path: str,
        *,
        payload: dict[str, object] | None = None,
        token: str | None = None,
        timeout: float = vault.DEFAULT_TIMEOUT,
    ) -> dict[str, object]:
        asked.append((path, timeout))
        return real(path, payload=payload, token=token, timeout=timeout)

    monkeypatch.setattr(vault, "request", spy)
    _MfaVault.uses_passcode = False

    assert vault.log_in("jasper", password="hunter2") == SESSION

    assert asked == [
        ("auth/userpass/login/jasper", vault.DEFAULT_TIMEOUT),
        ("sys/mfa/validate", vault.PUSH_TIMEOUT),
    ]
    # The pair above is only worth anything while the two differ.
    assert vault.PUSH_TIMEOUT > vault.DEFAULT_TIMEOUT


def test_two_enforcements_are_refused_not_half_answered(
    mfa_vault: None, no_prompt: None
) -> None:
    """Vault wants every matched enforcement satisfied, and ovx answers one.

    Answering the first anyway fails with "failed to satisfy enforcement
    <other>" — naming a factor the operator was never prompted for. Vault's own
    CLI refuses this case too.
    """
    _MfaVault.second_enforcement = "also-duo"

    with pytest.raises(vault.VaultError) as raised:
        vault.log_in("jasper", password="hunter2")

    message = str(raised.value)
    assert "also-duo" in message and "operator-totp" in message
    # Naming the limit is half of it; the hint has to leave the operator
    # somewhere to go, including one that needs no Vault admin rights.
    assert "sys/mfa/validate" in raised.value.hint
    assert _validates() == []


def test_a_wrong_passcode_blames_the_second_factor(mfa_vault: None) -> None:
    """The message has to name the layer that refused, and carry Vault's own."""
    with pytest.raises(vault.VaultError) as raised:
        vault.log_in("jasper", password="hunter2", passcode="000000")

    message = str(raised.value)
    assert "second factor" in message
    assert "failed to satisfy enforcement" in message
    assert "password" not in message


def test_an_empty_passcode_never_reaches_vault(mfa_vault: None) -> None:
    """Sending nothing would come back as "wrong code", which it is not."""
    with pytest.raises(vault.VaultError, match="no passcode given"):
        vault.log_in("jasper", password="hunter2", passcode="   ")

    assert _validates() == []


def test_no_token_and_no_challenge_is_still_a_failed_login(
    mfa_vault: None, no_prompt: None
) -> None:
    """The MFA fork must not swallow the case it was carved out of."""
    _MfaVault.challenge = False

    with pytest.raises(vault.VaultError, match="returned no token"):
        vault.log_in("jasper", password="hunter2")

    assert _validates() == []


@pytest.mark.parametrize(
    "requirement",
    [
        pytest.param({}, id="empty"),
        pytest.param({"mfa_constraints": {"x": {"any": [{"id": "m"}]}}}, id="no-id"),
        pytest.param({"mfa_request_id": REQUEST_ID}, id="no-constraints"),
        pytest.param(
            {"mfa_request_id": REQUEST_ID, "mfa_constraints": {"x": {"any": []}}},
            id="no-methods",
        ),
        pytest.param(
            {
                "mfa_request_id": REQUEST_ID,
                "mfa_constraints": {"x": {"any": [{"type": "totp"}]}},
            },
            id="method-without-an-id",
        ),
        pytest.param(
            {"mfa_request_id": REQUEST_ID, "mfa_constraints": {"x": {"any": "totp"}}},
            id="methods-not-a-list",
        ),
        pytest.param(
            {"mfa_request_id": REQUEST_ID, "mfa_constraints": {"x": None}},
            id="constraint-not-an-object",
        ),
    ],
)
def test_an_unusable_requirement_is_no_challenge(requirement: dict[str, Any]) -> None:
    """Half a challenge cannot be answered, so it stays a failed login.

    Prompting for a passcode ovx has nowhere to send would be worse than the
    defect: the operator would type a real code and still be told the login
    failed.
    """
    body: dict[str, object] = {
        "auth": {"client_token": "", "mfa_requirement": requirement}
    }

    assert vault._challenge(body) is None


def test_the_first_method_of_a_constraint_wins() -> None:
    """Any one method satisfies a constraint, and the first is the one taken.

    Which one it is decides whether the operator types a code or taps a phone,
    so it cannot be left to dict order by accident. Vault's own CLI takes the
    first as well.
    """
    found = vault._challenge(
        {
            "auth": {
                "client_token": "",
                "mfa_requirement": {
                    "mfa_request_id": REQUEST_ID,
                    "mfa_constraints": {
                        "both-enrolled": {
                            "any": [
                                {"type": "totp", "id": METHOD_ID, "uses_passcode": True},
                                {"type": "duo", "id": "the-second-one"},
                            ]
                        }
                    },
                },
            }
        }
    )

    assert found is not None
    assert (found.method_id, found.method_type) == (METHOD_ID, "totp")


def test_a_method_with_no_type_is_not_called_a_totp() -> None:
    """The type names what to approve, so ovx must not invent one Vault withheld.

    It reaches the operator as "approve the <type> request on your device", and
    a guessed "totp" would send them looking for a code that is not coming.
    """
    found = vault._challenge(
        {
            "auth": {
                "client_token": "",
                "mfa_requirement": {
                    "mfa_request_id": REQUEST_ID,
                    "mfa_constraints": {"x": {"any": [{"id": METHOD_ID}]}},
                },
            }
        }
    )

    assert found is not None
    assert found.method_type == "unnamed"


def test_the_first_answerable_method_wins() -> None:
    """A constraint carrying no method must not hide the one that does."""
    found = vault._challenge(
        {
            "auth": {
                "client_token": "",
                "mfa_requirement": {
                    "mfa_request_id": REQUEST_ID,
                    "mfa_constraints": {
                        "empty-one": {"any": []},
                        "the-real-one": {
                            "any": [
                                {
                                    "type": "totp",
                                    "id": METHOD_ID,
                                    "uses_passcode": True,
                                }
                            ]
                        },
                    },
                },
            }
        }
    )

    assert found == vault._Challenge(
        request_id=REQUEST_ID,
        method_id=METHOD_ID,
        method_type="totp",
        uses_passcode=True,
    )
