"""The skills' instructions, carried out against a real OpenViking.

The skills are prose an agent follows, and every instruction in them is a
claim about OpenViking's MCP tools: that a tool of that name takes those
arguments, that ``write`` makes missing parents, that ``glob`` truncates
oldest-first, that a local file goes in through an upload URL. The rest of the
suite checks the prose and the scripts. These check the claims, by making the
calls the skills tell an agent to make, through the same MCP endpoint the
harnesses connect to, as a user holding a Vault identity token.

They start Vault and OpenViking in containers (see ``live_openviking``).
``OV_TEST_IMAGE`` picks the OpenViking image. Marked ``integration`` and
skipped without docker::

    uv run pytest -m integration tests/test_live_openviking.py
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, TypeVar

import pytest
from live_openviking import LiveOpenViking, docker_available, openviking_stack
from test_skills import git_repo, run_script

pytestmark = pytest.mark.integration

T = TypeVar("T")

# Every tool a skill tells the agent to call, and the arguments it names.
# Read off the SKILL.md files: a rename on the server side is a skill that
# silently stops working, because the agent is told to call something absent.
SKILL_TOOLS: dict[str, set[str]] = {
    # /handoff
    "write": {"uri", "content", "mode"},
    # /continue and /learnings
    "read": {"uris"},
    "glob": {"pattern", "uri", "node_limit"},
    # /ingest
    "add_resource": {"path", "to", "description", "watch_interval", "args"},
    "list": {"uri", "recursive"},
    "find": {"query"},
    # /learnings
    "remember": {"messages"},
    "search": {"query"},
    "edit": {"uri", "old_string", "new_string"},
    "forget": {"uri"},
}


@pytest.fixture(scope="session")
def stack() -> Iterator[LiveOpenViking]:
    """Vault and OpenViking, shared by every test in the module."""
    if not docker_available():
        pytest.skip("needs docker to start Vault and OpenViking")
    with openviking_stack("ov-skills-it") as live:
        yield live


class Mcp:
    """A minimal MCP client over streamable HTTP, enough to call tools.

    OpenViking's endpoint is stateless and answers each JSON-RPC request with
    one server-sent event, so a request is one POST and the answer is the
    first ``data:`` line.
    """

    def __init__(self, live: LiveOpenViking) -> None:
        self.live = live
        self._next_id = 0

    def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send one JSON-RPC request and return its ``result``."""
        self._next_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id,
            "method": method,
            "params": params,
        }
        request = urllib.request.Request(
            f"{self.live.url}/mcp",
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "authorization": f"Bearer {self.live.token}",
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            text = response.read().decode()
        payload = next(
            (
                line[len("data: ") :]
                for line in text.splitlines()
                if line.startswith("data: ")
            ),
            text,
        )
        answer: dict[str, Any] = json.loads(payload)
        assert "error" not in answer, answer
        result: dict[str, Any] = answer["result"]
        return result

    def call(self, tool: str, **arguments: object) -> tuple[bool, str]:
        """Call a tool. Return whether it failed, and the text it answered."""
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments})
        text = "\n".join(
            block.get("text", "") for block in result.get("content", []) if block
        )
        return bool(result.get("isError")), text

    def ok(self, tool: str, **arguments: object) -> str:
        """Call a tool that must succeed, and return its text."""
        failed, text = self.call(tool, **arguments)
        assert not failed, f"{tool} failed: {text}"
        return text


@pytest.fixture
def mcp(stack: LiveOpenViking) -> Mcp:
    """Sign an MCP client in as the stack's user."""
    return Mcp(stack)


def unique(prefix: str) -> str:
    """Make a name no other test, or earlier run on a kept server, has used."""
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def eventually(
    read: Callable[[], T], done: Callable[[T], bool], seconds: float = 60.0
) -> T:
    """Poll ``read`` until ``done`` holds for its value, or time runs out."""
    deadline = time.monotonic() + seconds
    value = read()
    while not done(value) and time.monotonic() < deadline:
        time.sleep(1.0)
        value = read()
    return value


def test_every_tool_a_skill_names_takes_the_arguments_it_names(mcp: Mcp) -> None:
    """A tool the skills call, or an argument they pass, must exist."""
    tools = {
        tool["name"]: set(tool["inputSchema"].get("properties", {}))
        for tool in mcp.rpc("tools/list", {})["tools"]
    }

    missing = {
        name: sorted(wanted - tools.get(name, set()))
        for name, wanted in SKILL_TOOLS.items()
        if not wanted <= tools.get(name, set())
    }
    assert missing == {}


def test_a_handoff_written_where_the_script_says_is_found_by_continue(
    mcp: Mcp, tmp_path: Path
) -> None:
    """/handoff writes at the scripted URI; /continue globs the scripted dir.

    The two skills derive the path separately, so this is the round trip that
    proves they agree, against the server that resolves ``~``.
    """
    repo = git_repo(tmp_path, f"git@github.com:acme/{unique('repo')}.git")
    uri = run_script(repo, "new", "vault routing")
    directory = run_script(repo, "dir")
    body = "# Handoff: vault routing\n\n> Routing is wired; the migration is next.\n"

    # No mkdir first: the skill promises missing parents are made.
    mcp.ok("write", uri=uri, content=body, mode="replace")

    found = mcp.ok("glob", pattern="**/*.md", uri=directory, node_limit=500)
    name = uri.rsplit("/", 1)[1]
    assert name in found, found
    listed = re.findall(r"viking://\S+", found)
    assert listed and all(u.startswith("viking://user/") for u in listed), found

    read = mcp.ok("read", uris=listed)
    assert "Routing is wired; the migration is next." in read


def test_saving_a_handoff_again_replaces_it(mcp: Mcp, tmp_path: Path) -> None:
    """A second save to the same URI is the newer version winning.

    On 0.5.x a save that lands while the previous one is still being indexed
    is refused as busy, so the skill says to wait and try again. This makes
    the second save the way the skill now tells an agent to.
    """
    repo = git_repo(tmp_path, f"git@github.com:acme/{unique('repo')}.git")
    uri = run_script(repo, "new", "same work")

    mcp.ok("write", uri=uri, content="first version", mode="replace")
    failed, said = eventually(
        lambda: mcp.call("write", uri=uri, content="second version", mode="replace"),
        # Only the refusal the skill tells an agent to wait out is waited out.
        lambda answer: not answer[0] or "busy" not in answer[1],
    )
    assert not failed, said

    assert mcp.ok("read", uris=[uri]).strip() == "second version"


def test_glob_truncates_to_the_oldest_handoffs_without_saying_so(
    mcp: Mcp, tmp_path: Path
) -> None:
    """Why /continue always passes node_limit, and checks for a full page."""
    directory = f"viking://~/resources/handoffs/{unique('glob')}"
    for stamp in ("2026-01-01T0000", "2026-02-01T0000", "2026-03-01T0000"):
        mcp.ok("write", uri=f"{directory}/{stamp}--x.md", content=stamp, mode="replace")

    page = mcp.ok("glob", pattern="**/*.md", uri=directory, node_limit=2)

    assert "2026-01-01T0000" in page
    assert "2026-02-01T0000" in page
    assert "2026-03-01T0000" not in page
    assert "truncat" not in page.lower()


def test_a_recursive_list_mixes_directories_and_files(mcp: Mcp) -> None:
    """Why /continue uses glob: list gives it the directories as well."""
    directory = f"viking://~/resources/handoffs/{unique('list')}"
    mcp.ok(
        "write", uri=f"{directory}/sub/2026-01-01T0000--x.md", content="x", mode="replace"
    )

    listed = mcp.ok("list", uri=directory, recursive=True)

    assert "[dir] viking://user/" in listed
    assert "2026-01-01T0000--x.md" in listed


def test_a_local_file_goes_in_through_the_upload_url_add_resource_returns(
    mcp: Mcp, tmp_path: Path
) -> None:
    """/ingest's local-file path: one add_resource, then a multipart POST."""
    document = tmp_path / f"{unique('local')}.md"
    document.write_text("# Local Doc\n\nIngested through the upload URL.\n")

    instruction = mcp.ok("add_resource", path=str(document), description="test")
    match = re.search(
        r"https?://\S+/api/v1/resources/temp_upload\?token=\S+", instruction
    )
    assert match, instruction

    # The command the skill gives, run as written.
    posted = subprocess.run(
        ["curl", "-fsS", "-X", "POST", "-F", f"file=@{document}", match.group(0)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert posted.returncode == 0, posted.stderr
    root = json.loads(posted.stdout)["result"]["root_uri"]

    assert root.startswith("viking://resources/")
    tree = eventually(
        lambda: mcp.call("glob", pattern="**/*.md", uri=root, node_limit=50)[1],
        lambda text: ".md" in text and not text.startswith("No files"),
    )
    assert root in tree, tree


def test_a_remote_page_goes_in_from_one_call(mcp: Mcp, stack: LiveOpenViking) -> None:
    """/ingest's remote path: add_resource with a URL and a target, nothing else."""
    to = f"viking://resources/{unique('page')}"

    said = mcp.ok("add_resource", path=stack.page_url, to=to, description="test")

    assert "upload" not in said.lower(), said
    tree = eventually(
        lambda: mcp.call("list", uri=to, recursive=True)[1],
        lambda text: "[file]" in text,
    )
    assert "[file]" in tree, tree


def test_the_learnings_tools_store_find_correct_and_remove(mcp: Mcp) -> None:
    """Remember takes a messages array; edit and forget work on a stored file."""
    stored = mcp.ok(
        "remember",
        messages=[
            {
                "role": "user",
                "content": "When working in openviking_extensions, install "
                "dependencies with `uv add` so they land in pyproject.toml.",
            }
        ],
    )
    assert "message" in stored.lower(), stored
    mcp.ok("search", query="install dependencies uv add")

    # A memory file is whatever extraction decides to write, so edit and forget
    # are exercised on a file this test owns instead.
    name = f"{unique('learning')}.md"
    uri = f"viking://~/resources/{name}"
    mcp.ok("write", uri=uri, content="prefer uv pip\n", mode="replace", wait=True)
    mcp.ok("edit", uri=uri, old_string="uv pip", new_string="uv add")
    assert mcp.ok("read", uris=[uri]).strip() == "prefer uv add"

    failed, said = eventually(
        lambda: mcp.call("forget", uri=uri),
        lambda answer: not answer[0] or "busy" not in answer[1],
    )
    assert not failed, said
    # Through the listing: a read of a removed file is an error on 0.5.6 and an
    # answer saying so on 0.4.17.1.
    assert name not in mcp.ok("list", uri="viking://~/resources")
