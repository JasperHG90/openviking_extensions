# openviking

Packages that extend [OpenViking](https://github.com/volcengine/OpenViking), collected in one repository.

## Packages

| Package | Language | Description |
| --- | --- | --- |
| [`ov-postgres`](packages/ov-postgres/) | Python | PostgreSQL + pgvector backend for OpenViking's vector store |
| [`ovx`](packages/ovx/) | Bash | Run `ov` against a named profile, without leaving an API key on disk |

Each package has its own README with install and usage instructions.

## Repository layout

```
packages/
  ov-postgres/     Python package (uv workspace member)
  ovx/             Bash script with a Python test suite (standalone uv project)
```

Python packages are members of a single [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/): one `uv.lock` and one `.venv` at the root cover all of them. Packages in other languages live under `packages/` beside them with their own toolchains.

`ovx` sits between the two. It ships as a bash script, but its tests are Python, so it carries a `pyproject.toml` of its own. It is excluded from the workspace rather than added to it: a workspace resolves one Python floor across every member, and `ovx`'s tests need 3.11+ while `ov-postgres` still supports 3.10. Standing alone, it keeps its own lock and its own floor.

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

Adding a package means adding one filter block to `ci.yaml` and its name to whichever of the two fallback lists matches its template. It also needs an entry in `release.yaml`'s `package` choice, and — for a shell package — its name in the `'["ovx"]'` literal that `release.yaml` tests against to pick a template. GitHub Actions cannot share a list between workflows or choose a reusable workflow from an expression, so that name is repeated rather than defined once. A Python package also goes in the workspace members in [`pyproject.toml`](pyproject.toml).

## Releases

Each package releases on its own, from the manual [`release.yaml`](.github/workflows/release.yaml) workflow (Actions → release). Pick the package and an increment — a plain PATCH/MINOR/MAJOR bump of the package's newest tag — or type an explicit version, and run with `dry_run` first to see the plan. A real run re-tests the package, pushes an annotated `<package>-v<version>` tag, and publishes a GitHub release carrying the built artifacts. Nothing releases on push.

The tag is the only place a version exists. A Python package gets there through hatch-vcs, which reads the tag at build time. A shell package has no wheel and no hatch-vcs, so the release stamps the version into a copy of the script and attaches that alongside an installer pinned to the same release — a checkout reports `dev`, because an untagged working copy has no version to claim. `ovx`'s installer therefore defaults to the newest release rather than to `main`; installing the branch tip is opt-in via `--main`.

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
