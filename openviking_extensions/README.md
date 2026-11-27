# openviking_extensions

Monorepo containing extensions for OpenViking <https://github.com/volcengine/OpenViking>.

## Getting started

Needs [uv](https://docs.astral.sh/uv/) and [just](https://just.systems/).

```
just bootstrap
```

That installs the kmctl CLI (`just kmctl`), installs the APM dependencies in
`apm.yml` and runs `kmctl plugin patch` (`just install`), and creates `docs/`
and `.okf/` when they are missing (`just init`). `just check` confirms it
worked.

Run `just` to see the other recipes. `AGENTS.md` says how this repository
keeps its knowledge.
