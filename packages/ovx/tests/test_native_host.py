"""The Firefox native-messaging host, and registering it.

What matters here is that the host is a *credential boundary*: it hands out a
minted identity token and nothing else, it never falls back to a profile's
static key, and every failure comes back as a message the extension can show
rather than as a dead process.
"""

from __future__ import annotations

import io
import json
import struct
import time
from pathlib import Path

import pytest

from ovx import native_host, native_install
from ovx.errors import OvxError
from ovx.paths import Locations


def framed(payload: dict[str, object]) -> bytes:
    """Encode a message the way Firefox does."""
    body = json.dumps(payload).encode()
    return struct.pack("@I", len(body)) + body


def unframe(raw: bytes) -> dict[str, object]:
    """Decode one message, checking the length prefix agrees with the body."""
    (length,) = struct.unpack("@I", raw[:4])
    body = raw[4:]
    assert len(body) == length, "length prefix disagrees with the payload"
    decoded = json.loads(body)
    assert isinstance(decoded, dict)
    return decoded


def jwt_expiring(seconds_from_now: float) -> str:
    """Return a token whose `exp` claim sits that far ahead."""
    import base64

    def segment(data: dict[str, object]) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    header = segment({"alg": "RS256", "typ": "JWT"})
    body = segment({"exp": int(time.time() + seconds_from_now), "ov_account": "jasper"})
    return f"{header}.{body}.signature"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Locations:
    """An ovx home with one profile, and no ambient Vault to reach."""
    monkeypatch.setenv("OVX_DIR", str(tmp_path / ".ovx"))
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_TOKEN", raising=False)

    locations = Locations.resolve()
    locations.config_file.parent.mkdir(parents=True, exist_ok=True)
    locations.config_file.write_text(
        '[lab]\nurl = "https://ov.example"\naccount = "acme"\n'
        'user = "jasper"\napi_key = "static-key-do-not-leak"\n'
        '\n[other]\nurl = "https://other.example"\n'
    )
    return locations


def store_login(locations: Locations, profile: str, token: str) -> None:
    """Put a stored login in place, the way `ovx --login` would."""
    from ovx import tokens

    locations.token_dir.mkdir(parents=True, exist_ok=True)
    tokens.save(locations.token_dir, profile, token, "openviking")


class TestWireFormat:
    """Reading and writing Firefox's length-prefixed frames."""

    def test_round_trips_a_message(self) -> None:
        stream = io.BytesIO()
        native_host.write_message(stream, {"ok": True, "token": "abc"})
        assert unframe(stream.getvalue()) == {"ok": True, "token": "abc"}

    def test_reads_what_was_framed(self) -> None:
        stream = io.BytesIO(framed({"action": "ping"}))
        assert native_host.read_message(stream) == {"action": "ping"}

    def test_end_of_stream_is_not_an_error(self) -> None:
        assert native_host.read_message(io.BytesIO(b"")) is None
        # A truncated header is the same thing: the browser went away.
        assert native_host.read_message(io.BytesIO(b"\x01\x02")) is None

    def test_refuses_a_length_beyond_the_cap(self) -> None:
        # Refused on the declared length, before anything is allocated, so a
        # stream claiming 4 GB cannot make ovx try to hold it.
        stream = io.BytesIO(struct.pack("@I", native_host.MAX_MESSAGE_BYTES + 1))
        with pytest.raises(OvxError, match="larger than"):
            native_host.read_message(stream)

    def test_refuses_a_body_that_ended_early(self) -> None:
        stream = io.BytesIO(struct.pack("@I", 100) + b"{}")
        with pytest.raises(OvxError, match="ended early"):
            native_host.read_message(stream)

    @pytest.mark.parametrize("body", [b"not json", b'"a string"', b"[1, 2]"])
    def test_refuses_anything_that_is_not_a_json_object(self, body: bytes) -> None:
        stream = io.BytesIO(struct.pack("@I", len(body)) + body)
        with pytest.raises(OvxError):
            native_host.read_message(stream)


class TestHandling:
    """What the host answers."""

    def test_ping_reports_the_version(self, workspace: Locations) -> None:
        reply = native_host.handle(workspace, {"action": "ping"})
        assert reply["ok"] is True
        assert reply["version"]

    def test_lists_the_profiles(self, workspace: Locations) -> None:
        reply = native_host.handle(workspace, {"action": "profiles"})
        assert reply == {"ok": True, "profiles": ["lab", "other"]}

    def test_hands_over_a_live_token_and_where_to_spend_it(
        self, workspace: Locations
    ) -> None:
        token = jwt_expiring(3600)
        store_login(workspace, "lab", token)

        reply = native_host.handle(workspace, {"action": "token", "profile": "lab"})
        assert reply["ok"] is True
        assert reply["token"] == token
        # The URL travels with the token so the extension has one thing to
        # configure rather than two that can disagree.
        assert reply["url"] == "https://ov.example"

    def test_expands_a_var_in_the_profile_url(
        self, workspace: Locations, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `url = "$OV_URL"` is the whole point of ovx — the value lives in the
        # environment. Handing that string over unexpanded would have the
        # extension fetch from a URL called "$OV_URL".
        workspace.config_file.write_text('[env]\nurl = "$OV_TEST_TARGET"\n')
        store_login(workspace, "env", jwt_expiring(3600))
        monkeypatch.setenv("OV_TEST_TARGET", "https://from-the-env.example")

        reply = native_host.handle(workspace, {"action": "token", "profile": "env"})
        assert reply["url"] == "https://from-the-env.example"

    def test_leaves_the_url_out_when_its_var_is_unset(
        self, workspace: Locations, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # This process is started by Firefox, which does not inherit a shell's
        # exports, so an unresolvable reference is ordinary. The token is still
        # good and the extension has its own address field, so the field is
        # omitted rather than sent wrong or the whole request failed.
        workspace.config_file.write_text('[env]\nurl = "$OV_TEST_TARGET"\n')
        store_login(workspace, "env", jwt_expiring(3600))
        monkeypatch.delenv("OV_TEST_TARGET", raising=False)

        reply = native_host.handle(workspace, {"action": "token", "profile": "env"})
        assert reply["ok"] is True
        assert reply["token"]
        assert "url" not in reply

    def test_sends_no_identity_fields_across(self, workspace: Locations) -> None:
        # OpenViking reads identity from the token's claims, and the extension
        # sends no identity headers, so `account` and `user` would be payload
        # nobody reads crossing a credential boundary.
        store_login(workspace, "lab", jwt_expiring(3600))
        reply = native_host.handle(workspace, {"action": "token", "profile": "lab"})
        assert set(reply) == {"ok", "token", "profile", "url"}

    def test_never_hands_over_the_profile_api_key(self, workspace: Locations) -> None:
        # The whole point of the login is to stop using the static key. Handing
        # it to a browser because the login happened to be missing would be a
        # silent downgrade to a longer-lived credential.
        store_login(workspace, "lab", jwt_expiring(3600))
        reply = native_host.handle(workspace, {"action": "token", "profile": "lab"})
        assert "static-key-do-not-leak" not in json.dumps(reply)
        assert "api_key" not in reply

    def test_refuses_a_profile_that_never_logged_in(self, workspace: Locations) -> None:
        reply = native_host.handle(workspace, {"action": "token", "profile": "other"})
        assert reply["ok"] is False
        assert "no stored login" in str(reply["error"])
        assert "ovx --login other" in str(reply["hint"])

    def test_names_the_profiles_it_does_know(self, workspace: Locations) -> None:
        reply = native_host.handle(workspace, {"action": "token", "profile": "nope"})
        assert reply["ok"] is False
        assert "lab" in str(reply["hint"])

    @pytest.mark.parametrize("request_body", [{}, {"profile": ""}, {"profile": 7}])
    def test_refuses_a_request_naming_no_profile(
        self, workspace: Locations, request_body: dict[str, object]
    ) -> None:
        reply = native_host.handle(workspace, {"action": "token", **request_body})
        assert reply["ok"] is False

    def test_says_so_when_an_expired_login_cannot_be_renewed(
        self, workspace: Locations
    ) -> None:
        # Expired, and no $VAULT_ADDR to renew from. The extension has no
        # terminal, so the reply has to say what to run.
        store_login(workspace, "lab", jwt_expiring(-10))
        reply = native_host.handle(workspace, {"action": "token", "profile": "lab"})
        assert reply["ok"] is False
        assert "VAULT_ADDR" in str(reply["error"])

    def test_an_unknown_action_is_answered_not_ignored(
        self, workspace: Locations
    ) -> None:
        reply = native_host.handle(workspace, {"action": "rm -rf"})
        assert reply["ok"] is False
        assert "unknown action" in str(reply["error"])

    def test_an_unexpected_failure_still_comes_back_as_a_message(
        self, workspace: Locations, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A host that dies is reported to the user as "native application
        # unavailable", which says nothing about what to fix.
        def explode(*args: object, **kwargs: object) -> None:
            raise RuntimeError("something unforeseen")

        monkeypatch.setattr(native_host, "_profiles", explode)
        reply = native_host.handle(workspace, {"action": "profiles"})
        assert reply["ok"] is False
        assert "something unforeseen" in str(reply["error"])


class TestMain:
    """One exchange, end to end, over real streams."""

    def test_answers_one_message_and_stops(
        self, workspace: Locations, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store_login(workspace, "lab", jwt_expiring(3600))

        stdin = io.BytesIO(framed({"action": "token", "profile": "lab"}))
        stdout = io.BytesIO()
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(stdin))
        monkeypatch.setattr("sys.stdout", io.TextIOWrapper(stdout))

        assert native_host.main() == 0
        assert unframe(stdout.getvalue())["ok"] is True

    def test_keeps_stray_output_off_the_protocol_stream(
        self, workspace: Locations, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A print anywhere in the call path would be read as a message length
        # and break the exchange, so the real stdout is taken away first.
        def chatty(*args: object, **kwargs: object) -> dict[str, object]:
            print("this must not reach the browser")
            return {"ok": True, "profiles": []}

        monkeypatch.setattr(native_host, "_profiles", chatty)
        stdin = io.BytesIO(framed({"action": "profiles"}))
        stdout = io.BytesIO()
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(stdin))
        monkeypatch.setattr("sys.stdout", io.TextIOWrapper(stdout))

        assert native_host.main() == 0
        assert unframe(stdout.getvalue()) == {"ok": True, "profiles": []}

    def test_a_body_that_blows_the_parser_still_gets_a_reply(
        self, workspace: Locations, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Deep nesting makes json raise RecursionError, not OvxError, and it
        # happens in read_message — outside `handle`'s catch-all. Escaping
        # would kill the process with nothing on stdout, which Firefox reports
        # as "native application unavailable": exactly the silent death this
        # design exists to avoid. Well under the 1 MB cap, so the size check
        # does not cover it.
        body = (b'{"a":' * 60_000) + b"1" + (b"}" * 60_000)
        raw = struct.pack("@I", len(body)) + body

        stdout = io.BytesIO()
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(raw)))
        monkeypatch.setattr("sys.stdout", io.TextIOWrapper(stdout))

        assert native_host.main() == 1
        reply = unframe(stdout.getvalue())
        assert reply["ok"] is False
        assert str(reply["error"])

    def test_an_empty_stream_exits_quietly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"")))
        monkeypatch.setattr("sys.stdout", io.TextIOWrapper(io.BytesIO()))
        assert native_host.main() == 0


class TestInstall:
    """Registering the host with Firefox."""

    def test_writes_the_manifest_where_firefox_looks_on_macos(
        self, tmp_path: Path
    ) -> None:
        written = native_install.install(
            platform="darwin", home=tmp_path, executable=Path("/usr/local/bin/x")
        )
        assert written == (
            tmp_path / "Library/Application Support/Mozilla/NativeMessagingHosts/ovx.json"
        )
        assert json.loads(written.read_text())["path"] == "/usr/local/bin/x"

    def test_writes_the_manifest_where_firefox_looks_on_linux(
        self, tmp_path: Path
    ) -> None:
        written = native_install.install(
            platform="linux", home=tmp_path, executable=Path("/usr/bin/x")
        )
        assert written == tmp_path / ".mozilla/native-messaging-hosts/ovx.json"

    def test_allows_only_the_clipper(self, tmp_path: Path) -> None:
        # This list is the security boundary: an extension not on it cannot
        # start the host, so no other add-on can ask ovx for the token.
        written = native_install.install(
            platform="linux", home=tmp_path, executable=Path("/usr/bin/x")
        )
        body = json.loads(written.read_text())
        assert body["allowed_extensions"] == ["ov-clip@openviking"]
        assert body["type"] == "stdio"
        # Firefox keys on extension ids; `allowed_origins` is Chrome's and
        # would silently allow nothing here.
        assert "allowed_origins" not in body

    def test_refuses_a_platform_it_cannot_place_the_manifest_on(self) -> None:
        with pytest.raises(OvxError, match="no known Firefox"):
            native_install.manifest_dir("win32", Path("/tmp"))

    def test_uninstall_reports_whether_there_was_anything_to_remove(
        self, tmp_path: Path
    ) -> None:
        assert native_install.uninstall(platform="linux", home=tmp_path) is False
        native_install.install(
            platform="linux", home=tmp_path, executable=Path("/usr/bin/x")
        )
        assert native_install.uninstall(platform="linux", home=tmp_path) is True
        assert native_install.uninstall(platform="linux", home=tmp_path) is False

    def test_prefers_the_ovx_being_asked_over_whatever_is_on_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # `uv run ovx --install-firefox-host` from a checkout puts an ephemeral
        # environment first on $PATH. Baking that into the manifest gives one
        # that works until the environment is rebuilt, then points at nothing
        # — and Firefox reports that as "no such native application", which
        # looks identical to never having installed it.
        beside = tmp_path / "venv" / "bin"
        beside.mkdir(parents=True)
        (beside / native_install.HOST_COMMAND).write_text("#!/bin/sh\n")
        elsewhere = tmp_path / "ephemeral"
        elsewhere.mkdir()
        (elsewhere / native_install.HOST_COMMAND).write_text("#!/bin/sh\n")

        monkeypatch.setattr("sys.executable", str(beside / "python"))
        monkeypatch.setenv("PATH", str(elsewhere))
        assert native_install.host_path() == beside / native_install.HOST_COMMAND

    def test_falls_back_to_path_when_it_is_not_beside_the_interpreter(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        elsewhere = tmp_path / "bin"
        elsewhere.mkdir()
        script = elsewhere / native_install.HOST_COMMAND
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)

        monkeypatch.setattr("sys.executable", str(tmp_path / "nowhere" / "python"))
        monkeypatch.setenv("PATH", str(elsewhere))
        assert native_install.host_path() == script.resolve()

    def test_says_so_when_the_host_cannot_be_found_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.executable", str(tmp_path / "nowhere" / "python"))
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        with pytest.raises(OvxError, match="not on \\$PATH"):
            native_install.host_path()

    def test_the_allowed_extension_is_the_id_ov_clip_declares(self) -> None:
        # The most fragile join in the design, and it spans two packages: this
        # literal has to equal `browser_specific_settings.gecko.id` in
        # ov-clip's manifest.json. If they drift, Firefox refuses to start the
        # host and the extension reports "ovx is not installed", which is not
        # the cause. ov-clip's own suite pins the other side to the same
        # string, so either side moving alone fails a test.
        assert native_install.OV_CLIP_ID == "ov-clip@openviking"

    def test_the_host_command_is_the_one_pyproject_ships(self) -> None:
        # A manifest cannot carry arguments, so the host has to be its own
        # console script. If this name and pyproject's disagree, Firefox
        # executes something that does not exist.
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        assert f"{native_install.HOST_COMMAND} = " in pyproject
