# openviking

Packages that extend [OpenViking](https://github.com/volcengine/OpenViking), collected in one repository.

## Packages

| Package | Language | Description |
| --- | --- | --- |
| [`ov-postgres`](packages/ov-postgres/) | Python | PostgreSQL + pgvector backend for OpenViking's vector store |
| [`ov-retrieval`](packages/ov-retrieval/) | Python | Adds a keyword leg and a diversity pass to OpenViking's retrieval |
| [`ovx`](packages/ovx/) | Python | Run `ov` against a named profile, without leaving an API key on disk |
| [`ov-skills`](packages/ov-skills/) | Markdown + Bash | `/handoff`, `/continue`, `/learnings`, `/ingest` for Claude Code, opencode, and Hermes |
| [`ov-dash`](packages/ov-dash/) | TypeScript | A dashboard over OpenViking that signs people in through Vault and keeps their credential server-side |
| [`ov-clip`](packages/ov-clip/) | TypeScript | Firefox extension that saves the page you are reading into OpenViking, using `ovx`'s Vault login |

Each package has its own README with install and usage instructions.

## Repository layout

```
packages/
  ov-postgres/     Python package (uv workspace member)
  ov-retrieval/    Python package (uv workspace member)
  ovx/             Python package with a Typer CLI (standalone uv project)
  ov-skills/       Agent skills with a Python test suite (standalone uv project)
  ov-dash/         Node service and Svelte client (npm, its own toolchain)
  ov-clip/         Firefox extension (npm, its own toolchain)
```

Python packages are members of a single [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/): one `uv.lock` and one `.venv` at the root cover all of them. Packages in other languages live under `packages/` beside them with their own toolchains.

`ovx` is a Python package too, but is excluded from the workspace rather than added to it: a workspace resolves one Python floor across every member, and `ovx` needs 3.11+ for `tomllib` while `ov-postgres` still supports 3.10. Standing alone it keeps its own lock and its own floor.

`ov-skills` ships markdown and a bash installer rather than Python, but is tested in Python, so it carries a `pyproject.toml` of its own for the same reason.

## Development

```bash
uv sync --all-packages        # install every workspace package with its dev group
uvx prek run --all-files      # lint, type-check, and unit-test everything
```

To work on one package, run commands scoped to it:

```bash
uv run --directory packages/ov-postgres pytest
uv run --directory packages/ovx pytest        # its own project, its own lock
```

## CI

One workflow, [`ci.yaml`](.github/workflows/ci.yaml), covers the repo: repo-wide checks run once, then each package whose files changed is tested by the template that fits it. [`template-check.yaml`](.github/workflows/template-check.yaml) tests a Python package across its supported interpreters and builds and imports its wheel; [`template-check-shell.yaml`](.github/workflows/template-check-shell.yaml) runs a shell package's suite on both Ubuntu and macOS, since macOS still ships bash 3.2 and rejects syntax every other bash accepts.

Adding a package means adding one filter block to `ci.yaml` and its name to whichever of the three fallback lists matches its template. It also needs an entry in `release.yaml`'s `package` choice. A Python package needs nothing more there, since that is the branch everything falls into by default. Any other package goes in two more places: the literal naming its template — `'["ov-skills"]'` for a shell package, `'["ov-clip", "ov-dash"]'` for a Node one — and all four `'["ov-skills", "ov-clip", "ov-dash"]'` exclusions, one on the `test` job and three on the publish job's Python steps. Miss the last and the Python template runs against a package that has no wheel. GitHub Actions cannot share a list between workflows or choose a reusable workflow from an expression, so that list is repeated rather than defined once. A Python package also goes in the workspace members in [`pyproject.toml`](pyproject.toml); a standalone one goes in that file's `exclude` list instead.

## Releases

Each package releases on its own, from the manual [`release.yaml`](.github/workflows/release.yaml) workflow (Actions → release). Pick the package and an increment — a plain PATCH/MINOR/MAJOR bump of the package's newest tag — or type an explicit version, and run with `dry_run` first to see the plan. A real run re-tests the package, pushes an annotated `<package>-v<version>` tag, and publishes a GitHub release carrying the built artifacts. Nothing releases on push.

The tag is the only place a version exists. A Python package gets there through hatch-vcs, which reads the tag at build time. A shell package has no wheel and no hatch-vcs, so the release stamps the version in and attaches an installer pinned to the same release — a checkout claims no version, because an untagged working copy has none. `ov-skills` stamps its plugin manifest, which reads `0.0.0` in a checkout, and ships a tarball of `skills/`; its installer defaults to the newest release rather than to `main`, and installing the branch tip is opt-in via `--main`.

`ov-clip` follows the same rule: its `manifest.json` reads `0.0.0` in a checkout and the release stamps the tag in, then checks the stamp took — Firefox refuses to install two builds claiming the same version, so a stamp that silently failed would look like "the update did not apply". With `AMO_JWT_ISSUER` and `AMO_JWT_SECRET` set, the release signs through AMO and attaches an installable `.xpi`; without them it attaches an unsigned `.zip` rather than failing, so a fork can still cut a release.

`ov-dash` is a server, so its release carries no file at all. It builds `ghcr.io/jasperhg90/ov-dash` for `linux/amd64` and `linux/arm64` — the lab mixes amd64 boxes with arm boards — and the GitHub release holds only the notes saying how to pull it. Nothing in the running dashboard reports its own version, so the release stamps OCI labels (`title`, `version`, `revision`, `source`) and then reads `version` back off the pushed manifest, checking every architecture carries it and that both really landed. A single-arch push exits 0, and the arm boards would otherwise discover that at `docker run`. Only then does `latest` move, by pointing at the manifest that passed rather than by building again: pushed alongside the version tag, a build that failed verification would already be what every `docker pull` returns.

The image lives in a GHCR package that starts out **private**, and a new one is created by the first release that pushes it. Until someone opens Packages → `ov-dash` → *Package settings* and changes visibility to public, the `docker pull` line in the release notes fails with `denied` for everyone. If the package was instead created by hand with `just push`, it is owner-scoped with no repository linked, and the release cannot write to it until this repo is added under the package's *Manage Actions access*.

## Shared actions

Two composite actions under [`.github/actions/`](.github/actions/), for the Node packages that keep arriving:

| Action | What it does |
| --- | --- |
| [`setup-node-package`](.github/actions/setup-node-package/) | Install Node and one package's locked dependencies. Used by the CI template and by the release build, so both install a package the same way. |
| [`build-firefox-extension`](.github/actions/build-firefox-extension/) | Stamp the version into `manifest.json`, build, and sign through AMO when credentials exist. Falls back to an unsigned `.zip` and says which it produced. |

They are composite actions rather than reusable workflows because both have to run *inside* another job — a reusable workflow cannot.

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
