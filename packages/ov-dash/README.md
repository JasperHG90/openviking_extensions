# ov-dash

A dashboard over OpenViking. People sign in with their Vault username and
password; the dashboard keeps the credential that sign-in produces on the
server and calls OpenViking as them. The browser never holds one.

## Why it exists

OpenViking resolves identity from the credential alone. There is no header and
no claim that changes who it thinks you are, so under `auth_mode: "oidc"` the
browser half of the service does not work: Web Studio's credential page has
nothing to offer, and every write surface needs a bearer token that Vault's
provider will only mint through a browser redirect and will not refresh.

This dashboard closes that gap without forking anything. A person proves who
they are to Vault through the dashboard, and the dashboard mints the token
OpenViking accepts on their behalf — the same thing the `ov` CLI does, done
server-side so nobody needs the CLI.

## Two ways to hold a credential

Which one a deployment uses is set by `AUTH_MODE`, and they are genuinely
different — the Account page says which one is in force rather than assuming.

**`vault-userpass` (what `.env.example` ships, and what the lab cluster needs;
the schema's own default is `oidc`, so set this explicitly).** The sign-in
form posts a Vault username and password. The dashboard logs into Vault with
them, mints an OpenViking identity token from `identity/oidc/token/<role>`,
hands the Vault login token straight back with `revoke-self`, and keeps the
minted token in memory. The password is used once and never
stored. **No API key exists anywhere in this mode**, so `KEY_SOURCE` is not
read and nothing needs one.

The session ends at whichever runs out first, the minted token's own `exp` or
`SESSION_TTL_SECONDS`, and nothing renews it — refreshing would need the
password back. The cookie is written with that shorter lifetime, so the cookie
and the token it carries never disagree, and the Account page shows the time.

**`oidc` / `trusted-header` / `dev`.** Identity arrives from a provider, a
proxy, or configuration, and the dashboard resolves a *key* for that person on
every request from `KEY_SOURCE` — Vault KV, a static map, or one environment
variable. This is where the API-key story applies.

## How a Vault sign-in works

1. The browser asks for a page. There is no session, so it lands on Sign in.
2. The form posts to `/auth/vault-login`, guarded like every other
   state-changing route — without that, a page on another origin could have the
   browser accept a `Set-Cookie` for an account the attacker controls.
3. The dashboard logs into Vault's userpass mount with those credentials.
4. It mints an identity token for that person. The role decides the audience,
   the claims and the lifetime, so the dashboard asks for none of them.
5. It revokes the Vault login token. It existed for the mint and nothing later
   uses it; left alone Vault would keep it alive for its whole lease, and the
   sign-out button cannot reach it.
6. The identity is read back out of the minted token — `ov_account` and
   `ov_user` — because that is who OpenViking will answer as, and therefore the
   only trustworthy source for who this session is.
7. Token and identity go into the session store, and the browser is given a
   cookie naming it. Every later request looks the session up and calls
   OpenViking with the token the server is holding.

Signing out deletes the session. The cookie names a session the server holds,
so dropping the entry kills every copy of that cookie at once — not just the
browser that asked. The minted token stays valid until it expires (a signed JWT,
and Vault offers no way to withdraw one early), but nothing can present it as
that person once the session is gone.

### The sessions live in this process

The cookie carries an unguessable id and nothing else — no identity, no
credential. Everything it names is held in memory by the server.

That is what makes sign-out mean something. When the cookie *was* the session,
ending one meant asking a browser to forget it, and any copy taken beforehand —
off a shared machine, out of a synced profile, from a proxy log — kept working
until it expired.

Two consequences, both deliberate:

- **A restart signs everybody out.** The honest cost of holding no database. It
  fails closed, and the price is a sign-in after a deploy.
- **Run one process.** Two replicas each keep their own sessions, so a person
  would be signed out whenever the load balancer sent them to the other one.
  Scaling out needs a shared store, not this.

## How an OIDC sign-in works

1. `/auth/login` builds an authorization URL with PKCE, a state and a nonce, and
   stashes all three in a short-lived signed cookie.
2. The provider authenticates the person and redirects back to
   `/auth/callback` with a code.
3. The dashboard swaps the code for an ID token, verifies its signature against
   the provider's JWKS, and checks the nonce came back unchanged.
4. A claim from that token becomes the OpenViking user (`email-local` by
   default: `jasper@example.com` → `jasper`). That identity goes into a signed
   session cookie — an identity, never a credential.
5. Every later request resolves that user's key from `KEY_SOURCE` and calls
   OpenViking with it.

The session deliberately outlives the ID token here. Vault's provider advertises
no refresh grant, so its token dies in about an hour; the dashboard only needs
it to learn who someone is. Tying the session to it would sign people out
hourly and buy nothing.

## What it shows

| Page | Where the data comes from |
|---|---|
| Home | recursive `fs/ls`, plus memory and session counts |
| Files | one recursive `fs/ls`, hierarchy built client-side |
| Folder / File | `fs/ls`, `fs/stat`, `content/read`, `content/abstract`, `content/overview` |
| Search | `search/find` |
| Memories | files under `<scope>/memories`, grouped by category |
| Sessions | `sessions` |
| Add a file | `resources` import, via a private temp file |

The reading pane also acts on what it is showing: **Describe again** hands the
file back to OpenViking's model, **Delete** removes it, and dragging a row in
the tree onto a folder moves it there (`fs/mv`). The **+** in the Files toolbar
makes a folder to drag things into (`fs/mkdir`). See below for what each of
those does and does not do.

Downloads use `content/download`. A folder has no archive endpoint upstream, so
the server fetches each file and zips them. `/api/image` serves the same bytes
under a real image type so the reading pane can show a picture rather than a
download button.

The type is chosen from the file's *name*, so be clear about what keeps that
safe. Nothing reads the bytes: a file called `evil.png` holding markup is still
served as `image/png`. What stops the browser acting on it is
`x-content-type-options: nosniff`, backed by `default-src 'none'; sandbox`. The
raster-only allowlist is a second line — it keeps `.svg`, which is a document
that can carry script, from being offered a type at all — but the headers are
the control. Removing either would look harmless and would not be.

### A file's description does not come from its own abstract

OpenViking keeps no abstract per file. Ask it for one and it answers with the
*folder's* abstract — byte for byte the same string for every file in the
folder, so the pane used to caption every file with the same words. The
per-file description is written into the folder's overview instead, as a `###`
section named after the file and a line under Quick Navigation, and
`src/server/overview.ts` reads it back out of there. The folder's own abstract
still comes down, labelled as the folder's, for files the overview says nothing
about.

Sometimes it says nothing about a file that OpenViking did describe. A
generated overview is capped at 4000 characters (`semantic.overview_max_chars`,
default in OpenViking's own `openviking_cli/utils/config/parser_config.py`) and
`SemanticProcessor._truncate_generated_text` trims it at a sentence boundary —
no ellipsis, no marker. On a folder of four files that cut lands inside the
Detailed Description and the last three entries simply are not there, while the
front matter still reports every entry as sampled. Quick Navigation sits above
the cut and usually survives it, which is why it is the fallback.

The description is markdown, and the pane renders it as markdown. What
OpenViking writes into an overview section is prose with `**bold**`, bullets
and `[name](viking://…)` links in it; printed as source it read as a wall of
asterisks. Same renderer and same sanitizing as the document below it, so the
links inside a description open in the pane like any other.

### Describe again

A description is written once, by a model, and sometimes it does not arrive —
an image nothing could read, an import that ran while the model was down.
`POST /api/describe` hands the file back: `content/reindex` with
`mode=semantic_and_vectors`, which is the mode that reruns the model. The
SDK's default, `vectors_only`, re-embeds the text already stored, so a file
that never got a description would come back just as empty.

It runs out of band (`wait=false`) because the model takes minutes on a large
document, and a request held open that long times out on the way rather than
finishing. The page follows the job through `GET /api/job`, which reports
`waiting`, `done`, `failed` or `gone`. `gone` is its own answer, not a kind of
`done`: OpenViking drops task records after a while, and a job that failed and
then expired must not read as one that succeeded.

Handing it a file also refreshes the folder above, which is where the per-file
descriptions live (`reindex_executor.py` sets `propagate_to_parent=recursive`,
and the route defaults `recursive` to true).

Two limits worth knowing before you click it. OpenViking lets a `USER` reindex
**only their own namespace** — `content.py:_authorize_reindex_uri` — so on a
file under `OV_SHARED_ROOT` this comes back 403 with OpenViking's own words,
unless the dashboard's key carries an admin role. And a scope root is refused
here for the same reason a delete is: reindexing one reruns the model over
every document under it.

### Dragging a row moves it; there is nothing to reorder

Dropping a row on a folder calls `fs/mv`. What a drag cannot do is set a
position within a folder: OpenViking stores no order of its own and every
listing comes back sorted, so an order dragged into place would not survive the
next read. The destination sent to the server is the *folder*, not the finished
path — the name comes off what is being moved, so the new uri is one the server
derived rather than one it was handed.

The server also refuses a move onto a name the destination already holds, and
that check has to live here: OpenViking's `mv` refuses only when the
destination is a *directory*, and copies straight over an existing file without
a word (`storage/viking_fs/_ops.py`). Dragging `todo.md` into a folder that
already has one would otherwise destroy the second with nothing on screen to
say so.

### Making a folder is the other half of the drag

A drag can only move something into a folder that already exists. OpenViking
has had `fs/mkdir` all along; this dashboard had no way to call it, so getting
somewhere to put things meant uploading a file into a path that did not exist
yet, which is backwards. `POST /api/folder` is the other half.

The name is one path segment, cleaned by `safeSegment` in
`src/shared/names.ts` — the same character rule an uploaded file's name goes
through. It lives in shared because the New folder box previews the cleaned
name as you type: type `Q3 notes`, see `Q3_notes`, get `Q3_notes`.
`folderNameProblem` next to it says why a name will be refused, in the words
the server will use, so the refusal arrives before the round trip.

Three names are refused rather than cleaned. One that survives to nothing —
empty, `.`, `..` — because there is no name left to use; this is the one place
the rule differs from an upload, which has bytes to store either way and so
gets `upload`. One starting with a dot, because every listing here goes out
without `-a` and the folder would be invisible, and because `.abstract.md` is
the file OpenViking writes *inside* a folder — a directory at that name sits
where OpenViking will later want to write. And one over 255 characters, which
no filesystem this lands on will take.

The folder it lands in is the one selected in the tree, or the folder holding
the selected file, and otherwise wherever an upload with no destination would
go. Leave `into` out and the server works that out with the same
`resolveTarget` an upload uses, so there is one answer to "where do my files
live" rather than two. The server stats that parent and refuses a file, exactly
as a move does, because OpenViking will not: `mkdir` makes the parents it needs
and `_ensure_parent_dirs` logs what it could not make at debug level and carries
on, so a folder asked for under `todo.md` comes back as an opaque storage error
or as a directory nested inside a document.

A parent that is not there *yet* is fine, and that is the same `mkdir`
behaviour read the other way. Nothing in this dashboard creates the fixed
`resources` subtree — only OpenViking's own `initialize_user_directories`, from
routes ov-dash never calls — so refusing a missing parent would mean the first
folder in an untouched tree could not be made at all. Only a 404 counts as "not
there": an outage reading the parent still refuses, or a hiccup would have
`mkdir` build a chain under something that is really a file.

The description is optional and worth filling in. OpenViking writes it into the
folder's `.abstract.md` and vectorizes it (`service/fs_service.py`, `mkdir`),
so it is what makes the folder findable; left out, the abstract is the folder's
own name and nothing else.

The server refuses a name the destination already holds. OpenViking's own
`mkdir` runs with `exist_ok=False` and would refuse too, but with a message
about a path on disk — the check here is for the sentence somebody reads.

### Search is the vector face alone

`search/find`, the same search `ov search` runs. The page used to offer grep
and a hybrid of the two, which put a mode decision in front of every query and
charged twenty seconds for choosing wrong — `search/grep` takes about that per
term, against under a second for everything else. The server still exposes all
three modes; nothing in the UI asks for the other two.

An earlier design queried Postgres directly for a keyword half, which would
have put a tenant filter in hand-written SQL — one mistake away from showing one
person another's files. Going through OpenViking removes that risk: it answers
as whoever the key says, so there is no scope filter to get wrong.

## Configure

Copy `.env.example` to `.env`. Everything is checked at boot, and a missing
value names itself.

The three that decide the shape:

- `AUTH_MODE` — `vault-userpass` (the dashboard signs people into Vault and
  mints the token OpenViking accepts — the one that works against a
  Vault-backed OpenViking), `oidc` (the dashboard runs an OIDC login itself,
  for a provider whose tokens OpenViking accepts), `trusted-header` (a proxy
  already did it), or `dev` (one fixed identity).
- `KEY_SOURCE` — `vault` (per-person keys from KV), `static-map` (a JSON object),
  or `env` (one key for everyone; single-user or local only).
- `IDENTITY_FROM` — which claim becomes the OpenViking user. Vault's `sub` is an
  entity id and will not match a username, which is why the default is
  `email-local`.

### The identity is the data path, so it is checked three times

OpenViking derives who you are from the key, and the id also becomes a path
segment — in the Vault path a key is read from, and in the `viking://user/<id>`
prefix every file lives under. A wrong or hostile id is not a bad label, it is
somebody else's files. So a mapped identity must:

1. come from a **verified** address, when mapping from email
   (`OIDC_REQUIRE_EMAIL_VERIFIED`, on by default);
2. be in `OIDC_ALLOWED_EMAIL_DOMAINS`, when that is set — `email-local` throws
   the domain away, so without it the same local part at two domains is one
   OpenViking user sharing one tree;
3. be a **safe single path segment**. This is checked again in the key
   resolver, because that is the sink that matters: `fetch` normalizes `..`
   away before Vault ever sees the path, so an unchecked id reads whatever else
   the dashboard's own credentials can reach.

`OV_ROOT` defaults to `viking://user/{user}`, the canonical per-user root. The
uid-less `viking://user` is rejected by OpenViking outright, and as a dashboard
root it would also make every user's tree a prefix match of every other's.

## Why `/api/*` checks Origin

A cookie and a proxy header are *ambient*: the browser attaches them to
whatever request it is told to make, including one a page the person is merely
visiting started. `POST /api/upload` takes multipart, which is a CORS-simple
request and gets no preflight, so without a check any page could write files
into a signed-in person's OpenViking tree. `oidc` mode happened to survive on
the session cookie's `SameSite=Lax`, but that is cookie policy rather than a
decision, and `trusted-header` and `dev` had no such luck.

So every state-changing `/api/*` call, and `POST /auth/logout` with it, must
carry an `Origin` or `Sec-Fetch-Site` that says it came from this dashboard.
Most `GET`s are left alone: they change nothing, and a web page cannot read the
answer anyway, because no `Access-Control-Allow-Origin` is sent. Two are not,
because "changes nothing" is true about state and false about cost — one search
can be sixteen twenty-second greps, and one folder download half a gigabyte.
`/api/search` and `/api/download` are guarded like writes.

The check is stated positively: a guarded call must *show* it came from here,
by a matching `Origin` or a same-origin `Sec-Fetch-Site`. Refusing only a
*mismatched* `Origin` is not the same thing — it lets a request carrying no
origin header at all straight through, and Hono's `csrf` does not close that,
since it only inspects form-shaped content types. A JSON `DELETE` was once
covered by nothing but the absence of a CORS middleware, which is a property of
what is missing rather than a check.

Every write also checks *what* it is about to touch. A uri outside `OV_ROOT`
and `OV_SHARED_ROOT` is refused, including one that climbs out with `..` —
written plainly, with backslashes, or percent-encoded. It refuses *any* percent
escape, harmless ones included: allowing them means deciding whose decoding
wins, ours or OpenViking's, and it is what closes double encoding without a
decode loop. `requireInScope` in `src/server/app.ts` is the one place that
says so, and `/api/upload`, `/api/file`, `/api/move`, `/api/folder` and
`/api/describe` all go through it.

Somewhere to *put* a file may be a scope root; something to act on may not.
"Delete `viking://user/jasper`", or "rerun the model over all of it", is not a
request a stray click should be able to make, so delete, move and describe all
ask for a uri strictly inside a scope. A new folder's parent is somewhere to
put something, so it is allowed to be a scope root, the same as a move's
destination — nothing in the UI offers one, since the tree's rows start below
the root. Worth knowing if you call it by hand: a folder made directly under
`viking://user/<id>` is one you can drag into but never upload into, because
OpenViking's import validator wants a target under `resources` or `skills`
(`core/uri_validation.py`, `matches_content_kind`).

The *name* under that parent never reaches `requireInScope` as part of a path
at all. `safeSegment` has already reduced it to one segment: separators gone,
and everything outside `[A-Za-z0-9._-]` replaced, so a percent escape comes
through as text rather than as something the next reader might decode. That is
an allowlist rather than a denylist, which is what makes appending the name
*after* the scope check safe. `tests/names.test.ts` pins it directly.

None of this is what stops one person reaching another's tree — the API key is,
since OpenViking answers as whoever it belongs to. What it stops is a typo, or
a request the dashboard's own pages never made, scattering somebody's own
files.

## Run it

```bash
cp .env.example .env      # fill in OV_URL, SESSION_SECRET, VAULT_ADDR
docker compose up --build
```

Locally, without a provider:

```bash
npm install
AUTH_MODE=dev KEY_SOURCE=env OV_API_KEY=ov_yourkey \
  OV_URL=http://localhost:1933 SESSION_SECRET=$(openssl rand -base64 48) \
  SESSION_COOKIE_SECURE=false npm run dev
```

`npm run dev` serves the UI on 5173 and proxies the API to the server on 8080,
so the login redirect and the cookie behave as they will in production.

## Gates

There is a `justfile`. `just` on its own lists everything; the recipes below
are the ones you want most.

```bash
just gates      # what CI runs: tsc, svelte-check, biome, vitest — through prek,
                # so the versions match the ones pinned in .pre-commit-config.yaml
just test       # the fast offline suite alone
just image      # build the container
just push       # publish it, multi-arch, to GHCR
```

Releasing is a workflow, not a recipe: Actions → release, package `ov-dash`. It
re-runs the gates — including the Vault integration suite, against a throwaway
Vault it starts — tags `ov-dash-v<version>`, pushes the image to
`ghcr.io/jasperhg90/ov-dash` for amd64 and arm64, checks both architectures and
their version labels arrived, and only then moves `latest`. `just push` is the
hand path for when you need an image without cutting a release; it tags with the
short commit sha rather than a version.

The first release creates the GHCR package and it inherits the repository's
visibility, so `ghcr.io/jasperhg90/ov-dash` is public and needs no trip to the
package settings.

If the release fails after tagging, the tag stands and the workflow will refuse
that version on a re-run. Finish it by hand with `just push tag=<version>`, or
delete the tag and start again.

Under `just gates` are the same three npm scripts, if you would rather run one:

```bash
npm run check   # tsc, then svelte-check
npm run lint    # biome
npm test        # vitest, fast and offline
```

The unit suite stubs `fetch`, which proves the dashboard sends what we think it
sends and nothing about what Vault does with it. Three of the claims this design
rests on are Vault's behaviour, not ours — that `revoke-self` really kills the
login token, that the minted identity token survives that revocation, and that
the role's template produces the `ov_account`/`ov_user` claims identity is read
from. A stub answers whatever it is told to, so those are checked against a real
Vault instead:

```bash
just test-integration   # starts the Vault if it is not already up
```

or by hand:

```bash
docker run -d --name ovdash-vault -p 18200:8200 \
  -e VAULT_DEV_ROOT_TOKEN_ID=root hashicorp/vault:latest
npm run test:integration
```

It configures the mount, entity, alias and role itself, and is idempotent, so
re-running against the same container is fine. Point it elsewhere with
`VAULT_TEST_ADDR` and `VAULT_TEST_ROOT_TOKEN`. Kept out of `npm test` because
that runs on every commit and has to stay offline.

Biome is scoped to TypeScript; Svelte components are type-checked by
`svelte-check`, since Biome 1.x only half-parses `.svelte` and wants to reformat
every script block.

## What scopes a read

Nothing in this dashboard. `viking://` URIs are checked for their scheme and
passed upstream, so a read is scoped by exactly one thing: the credential
OpenViking answers as. That is the premise the whole design rests on, and it
holds wherever each person has their own credential — `vault-userpass`, or
`KEY_SOURCE=vault`.

It does **not** hold under `KEY_SOURCE=env`, where one key serves every caller.
There, anyone who can sign in can read anyone else's tree by asking for their
URI. That mode is for a single-user instance or local work, and it is not a
way to run this for a team.

Writes are scoped here as well as upstream — `resolveTarget` holds an upload to
the offered scopes, and a memory delete must sit under the memories root — but
that is defence against a mistyped path, not what keeps two people apart.

## Verified against the lab cluster

Run against `https://openviking-api.lab.orangecluster.nl` (OpenViking
v0.4.17.1) on 7 Sep 2026. Working: the file tree (418 entries, ~0.8s), file
read and download, folder download as a zip, memories grouped into OpenViking's
built-in categories, sessions, and all three search modes.

Four things that only a real instance could have told us:

**The cluster runs `auth_mode: "oidc"`, not `api_key`,** and reports
`api_key_manager: "not_configured"`. The dashboard still works, because the
server treats an `X-API-Key` value carrying two dots as a JWT — so what
`KEY_SOURCE` must supply here is a **Vault-minted OIDC identity token**, not an
OpenViking API key. Those tokens carry `ov_account` and `ov_user` claims, which
is where identity comes from; on this cluster they are `lab` and the person's
name, so `OV_ACCOUNT=lab`.

**The SDK's timeout is milliseconds.** The Python SDK's is seconds, and passing
`OV_TIMEOUT_MS / 1000` gave every call a 30ms budget — invisible locally, where
a stubbed server answers instantly, and fatal against a real one. `tests/
timeout.test.ts` now puts a delay in front of the stub so the unit is checked.

**`search/grep` takes about twenty seconds per term.** Everything else answers
in under a second. A 30s budget therefore timed out exact search alone, so the
default is now 60s. That measurement is why the UI now searches by meaning
alone; the timeout stays at 60s because the server still offers grep.

**Multi-scope `find` returns nothing at `limit >= 20`.** Passing both scopes as
an array answers correctly at `limit <= 10` and returns zero above it, while
either scope alone is fine at any limit. So `find` runs once per scope and the
results are merged here — see the comment on `OvClient.find`.

## What is still not verified

The OIDC login flow itself. Every run above used `AUTH_MODE=dev` with a token
lifted from `ovx`, because the dashboard is not yet registered as a client with
Vault's OIDC provider. Discovery, PKCE, the callback and JWKS verification are
covered by unit tests against a stubbed provider, not by a real sign-in.

`KEY_SOURCE=vault` is likewise untested end to end: the Vault token on this
machine was expired, so keys came from `KEY_SOURCE=env`.
