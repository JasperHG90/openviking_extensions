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
  ovx/             Typer CLI, Vault login, Firefox native host (standalone uv project)
  ov-skills/       Agent skills, a bash installer, a Python test suite (standalone uv project)
  ov-dash/         Node service and Svelte client (npm, its own toolchain)
  ov-clip/         Firefox extension (npm, its own toolchain)
```

Two of the four Python packages — `ov-postgres` and `ov-retrieval` — are members of a single [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/): one `uv.lock` and one `.venv` at the root cover both. `ovx` and `ov-skills` stand alone, each with a lock and a `.venv` of its own. Packages in other languages live under `packages/` beside them with their own toolchains.

They stand alone because a workspace resolves one Python floor across every member: `ovx` needs 3.11+ for `tomllib` while `ov-postgres` still supports 3.10. `ov-skills` ships markdown and a bash installer rather than Python, but its tests need 3.11+ too, so it carries a `pyproject.toml` of its own and stays out for the same reason. The root `pyproject.toml` names both in `exclude`, which stops uv adopting them when a command runs inside their directories.

## Development

```bash
uv sync --all-packages             # the two workspace members, with their dev groups
uvx prek@0.2.25 run --all-files    # ruff, mypy, and the Python suites
```

`--all-packages` reaches the workspace and stops there, so it installs `ov-postgres` and `ov-retrieval` only. `ovx` and `ov-skills` build their own environments the first time you `uv run --directory` into them.

`prek` covers the Python packages on a bare checkout, but not the Node ones. Its six Node hooks shell out to each package's own `node_modules`, which nothing at the root installs, so until you have run `npm install` there they fail on a missing `biome` rather than on anything about the code. CI's repo-wide job skips them by name for that reason and runs them in the Node template instead, with the package's pinned toolchain.

To work on one package, run commands scoped to it:

```bash
uv run --directory packages/ov-postgres pytest
uv run --directory packages/ovx pytest          # its own project, its own lock
uv run --directory packages/ov-skills pytest

npm --prefix packages/ov-clip install && npm --prefix packages/ov-clip test
cd packages/ov-dash && npm install && just gates   # ov-dash drives its gates through just
```

## CI

One workflow, [`ci.yaml`](.github/workflows/ci.yaml), covers the repo: repo-wide checks run once, then each package whose files changed is tested by the template that fits it. There are three.

| Template | Packages | What it runs |
| --- | --- | --- |
| [`template-check.yaml`](.github/workflows/template-check.yaml) | `ov-postgres`, `ov-retrieval`, `ovx` | The suite on the floor its `requires-python` names and on the newest interpreter, then builds a wheel and checks it installs and imports on its own. |
| [`template-check-shell.yaml`](.github/workflows/template-check-shell.yaml) | `ov-skills` | The suite on both Ubuntu and macOS, since macOS still ships bash 3.2 and rejects syntax every other bash accepts. |
| [`template-check-node.yaml`](.github/workflows/template-check-node.yaml) | `ov-dash`, `ov-clip` | Type-check, lint and tests against the `node_modules` it installs — the gates the repo-wide job cannot run — then builds whatever the package ships: a container where there is a `Dockerfile`, an extension where there is a `manifest.json`. |

Adding a package means adding one filter block to `ci.yaml` and its name to whichever of the three fallback lists matches its template. It also needs an entry in `release.yaml`'s `package` choice. A Python package needs nothing more there, since that is the branch everything falls into by default. Any other package goes in two more places: the literal naming its template — `'["ov-skills"]'` for a shell package, `'["ov-clip", "ov-dash"]'` for a Node one — and all four `'["ov-skills", "ov-clip", "ov-dash"]'` exclusions, one on the `test` job and three on the publish job's Python steps. Miss the last and the Python template runs against a package that has no wheel. GitHub Actions cannot share a list between workflows or choose a reusable workflow from an expression, so that list is repeated rather than defined once. A Python package also goes in the workspace members in [`pyproject.toml`](pyproject.toml); a standalone one goes in that file's `exclude` list instead.

## Releases

Each package releases on its own, from the manual [`release.yaml`](.github/workflows/release.yaml) workflow (Actions → release). Pick the package and an increment — a plain PATCH/MINOR/MAJOR bump of the package's newest tag — or type an explicit version, and run with `dry_run` first to see the plan. A real run re-tests the package, pushes an annotated `<package>-v<version>` tag, and publishes a GitHub release carrying the built artifacts. Nothing releases on push.

The tag is the only place a version exists. A Python package gets there through hatch-vcs, which reads the tag at build time. A shell package has no wheel and no hatch-vcs, so the release stamps the version in and attaches an installer pinned to the same release — a checkout claims no version, because an untagged working copy has none. `ov-skills` stamps its plugin manifest, which reads `0.0.0` in a checkout, and ships a tarball holding `skills/` and that manifest — the shape `install.sh` unpacks, and a drop-in Claude Code plugin directory for anyone who would rather unpack it by hand. Its installer defaults to the newest release rather than to `main`, and installing the branch tip is opt-in via `--main`.

`ov-clip` follows the same rule: its `manifest.json` reads `0.0.0` in a checkout and the release stamps the tag in, then checks the stamp took — Firefox refuses to install two builds claiming the same version, so a stamp that silently failed would look like "the update did not apply". With `AMO_JWT_ISSUER` and `AMO_JWT_SECRET` set, the release signs through AMO and attaches an installable `.xpi`; without them it attaches an unsigned `.zip` rather than failing, so a fork can still cut a release.

`ov-dash` is a server, so its release carries no file at all. It builds `ghcr.io/jasperhg90/ov-dash` for `linux/amd64` and `linux/arm64` — the lab mixes amd64 boxes with arm boards — and the GitHub release holds only the notes saying how to pull it. Nothing in the running dashboard reports its own version, so the release stamps OCI labels (`title`, `version`, `revision`, `source`) and then reads `version` back off the pushed manifest, checking every architecture carries it and that both really landed. A single-arch push exits 0, and the arm boards would otherwise discover that at `docker run`. Only then does `latest` move, by pointing at the manifest that passed rather than by building again: pushed alongside the version tag, a build that failed verification would already be what every `docker pull` returns.

The first release creates the GHCR package, which inherits this repository's visibility — `ov-dash-v0.1.0` came out publicly pullable with no manual step. A package created by hand with `just push` instead is owner-scoped with no repository linked, and a release cannot write to it until this repo is added under the package's *Manage Actions access*. If a `docker pull` ever comes back `denied`, that link and the package's visibility are the two things to check.

## Shared actions

Two composite actions under [`.github/actions/`](.github/actions/), for the Node packages that keep arriving:

| Action | What it does |
| --- | --- |
| [`setup-node-package`](.github/actions/setup-node-package/) | Install Node and one package's locked dependencies. Used by the CI template and by the release build, so both install a package the same way. |
| [`build-firefox-extension`](.github/actions/build-firefox-extension/) | Stamp the version into `manifest.json`, build, and sign through AMO when credentials exist. Falls back to an unsigned `.zip` and says which it produced. |

They are composite actions rather than reusable workflows because both have to run *inside* another job — a reusable workflow cannot.

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
