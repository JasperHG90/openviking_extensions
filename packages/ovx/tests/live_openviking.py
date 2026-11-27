"""A real OpenViking to test against, signed into the way the lab signs in.

The integration suite needs a server that agrees or disagrees with the client
for real. This module starts one: OpenViking in a container, in ``oidc`` auth
mode, trusting a dev-mode Vault whose identity tokens carry ``ov_account`` and
``ov_user`` the way the lab's ``openviking`` role does. The client then holds a
Vault identity token, which is what every caller in the lab sends.

Two containers and a stub, each shaped by watching the server refuse to
start or refuse a request:

- OpenViking will not boot without an embedding provider, so a stub
  OpenAI-compatible endpoint runs in a child process. It answers embeddings
  and chat completions with fixed output; nothing here depends on their
  meaning.
- The token's ``iss`` is the address Vault was told it lives at, which is
  ``127.0.0.1`` from this process. The container cannot fetch anything there,
  so the server is given the issuer as Vault states it and the key set at
  ``host.docker.internal``, which ``--add-host`` makes resolve on Linux too.
- In ``oidc`` mode the root key is refused like any other non-JWT, so there is
  no admin setup to do: the account exists as soon as a token names it.

The image is ``$OV_TEST_IMAGE``, defaulting to the version this package is
checked against. Set ``OV_TEST_KEEP=1`` to leave the containers running.

This file is copied, not shared, between ov-sync, ovx and ov-skills: each is a
standalone uv project and cannot import from the others. It uses ``urllib``
rather than httpx for the same reason: two of the three have no httpx. The
copies are identical except that ovx's adds ``ov_binary``, which copies the
``ov`` CLI out of the image under test; keep the shared part in step.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

DEFAULT_IMAGE = "ghcr.io/volcengine/openviking:v0.5.6"
VAULT_IMAGE = "hashicorp/vault:2.1.0"
VAULT_ROOT_TOKEN = "root"
ACCOUNT = "lab"
USER = "jasper"
PASSWORD = "hunter2"
EMBEDDING_DIMENSION = 1024
BOOT_TIMEOUT_SECONDS = 240.0

# What the stub serves for any ``*.html`` path, so importing a remote page can
# be tested with no internet: the container fetches it through the host
# gateway, at ``LiveOpenViking.page_url``.
PAGE = (
    "<html><head><title>Stub Page</title></head>"
    "<body><h1>Stub Page</h1><p>Served by the integration stub.</p></body></html>"
)


@dataclass(frozen=True)
class LiveOpenViking:
    """How to reach a running OpenViking, and as whom.

    Attributes
    ----------
    url : str
        Base URL, without a trailing slash.
    token : str
        A Vault identity token the server accepts.
    account : str
        The account the token's ``ov_account`` claim names.
    user : str
        The user the token's ``ov_user`` claim names.
    image : str
        The OpenViking image under test.
    container : str
        The OpenViking container's name, for ``docker`` calls against it.
    vault_url : str
        The Vault that minted the token.
    vault_user : str
        A userpass login on that Vault whose entity maps to ``account``/``user``.
    vault_password : str
        That login's password.
    page_url : str
        A web page the server can fetch, for testing a remote import.
    """

    url: str
    token: str
    account: str
    user: str
    image: str
    container: str
    vault_url: str
    vault_user: str
    vault_password: str
    page_url: str


def docker_available() -> bool:
    """Whether a container runtime answers."""
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "info", "--format", "{{.ID}}"],
            check=True,
            capture_output=True,
            timeout=20,
        )
    except (subprocess.SubprocessError, OSError):
        return False
    return True


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _stub_vector(text: str) -> list[float]:
    digest = hashlib.sha256(text.encode()).digest()
    return [digest[i % len(digest)] / 255.0 - 0.5 for i in range(EMBEDDING_DIMENSION)]


class _ModelHandler(BaseHTTPRequestHandler):
    """Embeddings and chat completions, OpenAI-shaped, with fixed answers."""

    def log_message(self, format: str, *args: object) -> None:
        """Stay quiet; pytest captures nothing useful from request logs."""

    def _answer(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """Serve a web page to import, or list one model."""
        if self.path.endswith(".html"):
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._answer({"object": "list", "data": [{"id": "stub", "object": "model"}]})

    def do_POST(self) -> None:
        """Answer an embeddings or a chat completion request."""
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            request = json.loads(raw or b"{}")
        except ValueError:
            request = {}
        if self.path.endswith("/embeddings"):
            inputs = request.get("input", [""])
            texts = inputs if isinstance(inputs, list) else [inputs]
            self._answer(
                {
                    "object": "list",
                    "model": "stub",
                    "data": [
                        {
                            "object": "embedding",
                            "index": i,
                            "embedding": _stub_vector(str(t)),
                        }
                        for i, t in enumerate(texts)
                    ],
                    "usage": {"prompt_tokens": 1, "total_tokens": 1},
                }
            )
            return
        self._answer(
            {
                "id": "stub",
                "object": "chat.completion",
                "model": "stub",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "A stub summary."},
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            }
        )


@contextmanager
def _model_server() -> Iterator[int]:
    """Run the stub in a child process, and yield its port.

    A child rather than a thread: ovx's tests fork a pty, and forking a
    process that has threads running can deadlock the child.
    """
    port = _free_port()
    child = subprocess.Popen([sys.executable, "-I", __file__, str(port)])
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if time.monotonic() > deadline or child.poll() is not None:
                    raise RuntimeError("the stub model server did not start") from None
                time.sleep(0.1)
        yield port
    finally:
        child.terminate()
        child.wait(timeout=10)


def _http(
    method: str,
    url: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("content-type", "application/json")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read().decode() or "{}"
    parsed: dict[str, Any] = json.loads(text)
    return parsed


def _wait_for(url: str, what: str, container: str) -> None:
    deadline = time.monotonic() + BOOT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3):
                return
        except (urllib.error.URLError, OSError):
            time.sleep(1.0)
    logs = subprocess.run(
        ["docker", "logs", "--tail", "40", container], capture_output=True, text=True
    )
    raise RuntimeError(f"{what} did not come up:\n{logs.stdout}{logs.stderr}")


def _docker_run(name: str, args: list[str]) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)
    subprocess.run(
        ["docker", "run", "-d", "--name", name, *args], check=True, capture_output=True
    )


def _configure_vault(vault: str) -> None:
    """Mirror the lab: userpass, an entity carrying ov_* metadata, one role."""
    root = {"x-vault-token": VAULT_ROOT_TOKEN}
    _http("POST", f"{vault}/v1/sys/auth/userpass", {"type": "userpass"}, root)
    policy = 'path "identity/oidc/token/openviking" { capabilities = ["read"] }'
    _http("PUT", f"{vault}/v1/sys/policies/acl/openviking", {"policy": policy}, root)
    _http(
        "POST",
        f"{vault}/v1/auth/userpass/users/{USER}",
        {"password": PASSWORD, "policies": "default,openviking"},
        root,
    )
    mounts = _http("GET", f"{vault}/v1/sys/auth", headers=root)
    accessor = mounts["data"]["userpass/"]["accessor"]
    entity = _http(
        "POST",
        f"{vault}/v1/identity/entity",
        {"name": USER, "metadata": {"ov_account": ACCOUNT, "ov_user": USER}},
        root,
    )
    _http(
        "POST",
        f"{vault}/v1/identity/entity-alias",
        {"name": USER, "canonical_id": entity["data"]["id"], "mount_accessor": accessor},
        root,
    )
    _http(
        "POST",
        f"{vault}/v1/identity/oidc/key/identity_tokens",
        {
            "allowed_client_ids": ["*"],
            "rotation_period": "24h",
            "verification_ttl": "24h",
        },
        root,
    )
    # Unquoted placeholders: Vault emits each substitution as JSON already.
    template = (
        '{"ov_account":{{identity.entity.metadata.ov_account}},'
        '"ov_user":{{identity.entity.metadata.ov_user}}}'
    )
    _http(
        "POST",
        f"{vault}/v1/identity/oidc/role/openviking",
        {
            "key": "identity_tokens",
            # The audience the lab's server checks, set the way the lab sets it.
            "client_id": "openviking",
            "ttl": "1h",
            "template": base64.b64encode(template.encode()).decode(),
        },
        root,
    )


def mint_token(vault: str, user: str = USER, password: str = PASSWORD) -> str:
    """Log in with userpass and mint an identity token for OpenViking."""
    login = _http(
        "POST", f"{vault}/v1/auth/userpass/login/{user}", {"password": password}
    )
    client_token = login["auth"]["client_token"]
    minted = _http(
        "GET",
        f"{vault}/v1/identity/oidc/token/openviking",
        headers={"x-vault-token": client_token},
    )
    token: str = minted["data"]["token"]
    return token


def _openviking_config(vault_port: int, model_port: int) -> dict[str, Any]:
    model_base = f"http://host.docker.internal:{model_port}/v1"
    return {
        "server": {
            "host": "0.0.0.0",
            "port": 1933,
            "auth_mode": "oidc",
            "root_api_key": f"root-{uuid.uuid4().hex}",
            "oidc": {
                "issuer": f"http://127.0.0.1:{vault_port}/v1/identity/oidc",
                "audience": "openviking",
                "jwks_uri": (
                    f"http://host.docker.internal:{vault_port}"
                    "/v1/identity/oidc/.well-known/keys"
                ),
                "identity": {
                    "account_id": {"source": "claim", "claim": "ov_account"},
                    "user_id": {"source": "claim", "claim": "ov_user"},
                },
            },
        },
        "embedding": {
            "dense": {
                "provider": "openai",
                "model": "stub",
                "api_key": "stub",
                "api_base": model_base,
                "dimension": EMBEDDING_DIMENSION,
                "input": "text",
            }
        },
        "vlm": {
            "provider": "openai",
            "model": "stub",
            "api_key": "stub",
            "api_base": model_base,
        },
        # Only so the server may fetch the stub's page through the host
        # gateway: its import guard refuses private addresses otherwise. The
        # lab does not set it.
        "allow_private_networks": True,
    }


@contextmanager
def openviking_stack(prefix: str) -> Iterator[LiveOpenViking]:
    """Start Vault and OpenViking, yield how to reach them, then remove both.

    Parameters
    ----------
    prefix :
        Names the containers, so two packages' suites cannot remove each
        other's.
    """
    image = os.environ.get("OV_TEST_IMAGE", DEFAULT_IMAGE)
    vault_name = f"{prefix}-vault"
    ov_name = f"{prefix}-openviking"
    vault_port = _free_port()
    ov_port = _free_port()
    vault = f"http://127.0.0.1:{vault_port}"
    try:
        with _model_server() as model_port:
            _docker_run(
                vault_name,
                [
                    "-p",
                    f"{vault_port}:8200",
                    "-e",
                    f"VAULT_DEV_ROOT_TOKEN_ID={VAULT_ROOT_TOKEN}",
                    # The issuer is built from this. Dev mode's default is an
                    # address nothing outside the container can use.
                    "-e",
                    f"VAULT_API_ADDR={vault}",
                    VAULT_IMAGE,
                ],
            )
            _wait_for(f"{vault}/v1/sys/health", "Vault", vault_name)
            _configure_vault(vault)

            config = _openviking_config(vault_port, model_port)
            _docker_run(
                ov_name,
                [
                    "--add-host",
                    "host.docker.internal:host-gateway",
                    # Loopback only. Vault cannot be: the container fetches
                    # its keys through the host gateway.
                    "-p",
                    f"127.0.0.1:{ov_port}:1933",
                    "-e",
                    "OPENVIKING_WITH_BOT=0",
                    "-e",
                    f"OPENVIKING_CONF_CONTENT={json.dumps(config)}",
                    image,
                ],
            )
            url = f"http://127.0.0.1:{ov_port}"
            _wait_for(f"{url}/health", f"OpenViking ({image})", ov_name)
            yield LiveOpenViking(
                url=url,
                token=mint_token(vault),
                account=ACCOUNT,
                user=USER,
                image=image,
                container=ov_name,
                vault_url=vault,
                vault_user=USER,
                vault_password=PASSWORD,
                page_url=f"http://host.docker.internal:{model_port}/stub-page.html",
            )
    finally:
        if not os.environ.get("OV_TEST_KEEP"):
            subprocess.run(
                ["docker", "rm", "-f", ov_name, vault_name], capture_output=True
            )


def ov_binary(live: LiveOpenViking, dest_dir: Path) -> Path:
    """Return an ``ov`` binary matching the server's version.

    ``$OV_TEST_OV_BIN`` wins. Otherwise the binary is copied out of the image
    under test, so the CLI and the server always come from one release.
    The wrapper script on the image's PATH is Python; the Rust binary it runs
    sits inside the installed package.
    """
    configured = os.environ.get("OV_TEST_OV_BIN")
    if configured:
        return Path(configured)
    located = subprocess.run(
        [
            "docker",
            "exec",
            live.container,
            "python",
            "-c",
            "import openviking, os; print(os.path.dirname(openviking.__file__))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    binary = dest_dir / "ov"
    subprocess.run(
        [
            "docker",
            "cp",
            f"{live.container}:{located.stdout.strip()}/bin/ov",
            str(binary),
        ],
        check=True,
        capture_output=True,
    )
    return binary


if __name__ == "__main__":
    # 0.0.0.0, not loopback: the container reaches it through the host gateway.
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1])), _ModelHandler).serve_forever()
