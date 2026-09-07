# ov-clip

A Firefox extension that saves the page you are reading into OpenViking.

It is a port of [memex's firefox-extension](https://github.com/JasperHG90/memex/tree/main/packages/firefox-extension), with the same page-reading engine and a different back end: it writes into OpenViking, and it borrows the sign-in [`ovx`](../ovx/) already has.

## Why it reads the page instead of sending a link

A server given a URL fetches it as a stranger and gets what a stranger gets: a consent wall, a login form, a shell waiting for JavaScript. The extension reads the page already rendered in your tab, so what lands in OpenViking is what you were reading. Articles go through [Readability](https://github.com/mozilla/readability) and [Turndown](https://github.com/mixmark-io/turndown) to markdown; PDFs are uploaded whole so OpenViking parses the real file.

## How it signs in

It doesn't. `ovx` does, and this borrows the result.

`ovx --login <profile>` logs into Vault and mints an identity token — the same credential `ov` uses, which OpenViking resolves an identity from. The extension asks `ovx` for that token over [native messaging](https://developer.mozilla.org/docs/Mozilla/Add-ons/WebExtensions/Native_messaging) each time it saves, then talks to OpenViking directly.

What that gives you:

- **No credential in the browser.** Nothing is stored, encrypted or otherwise, because nothing needs to be. Close Firefox and there is nothing to lose.
- **No password here.** Signing in happens in a terminal. The extension never sees a Vault password and never talks to Vault at all.
- **Renewal for free.** `ovx` mints a fresh token off your live Vault session as the old one nears expiry, exactly as it does for `ov`. The extension gets whatever is current.
- **One place to configure.** The profile's `url` travels back with the token, so the OpenViking address is not typed twice into two things that can disagree.

The reason it works this way rather than reading `~/.ovx/tokens/<profile>.json` directly: a Firefox extension has no filesystem access. There is no read API, and unlike Chrome it cannot be granted host permissions for `file://`. Native messaging is the documented way across.

`ovx --install-firefox-host` writes a manifest naming `ov-clip@openviking` and nothing else, so no other add-on on your machine can start the host and ask for your token.

## Install

### From a release

1. Download the `.xpi` from [Releases](../../releases).
2. Open `about:addons`, and drag the file onto the page — or gear icon → **Install Add-on From File…**

If the release only carries an unsigned `.zip`, release Firefox will refuse it. Use Developer Edition or Nightly with `xpinstall.signatures.required = false` in `about:config`, or load it temporarily as below.

### From a checkout

```bash
cd packages/ov-clip
npm install
npm run build
```

Then `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on…** → pick `packages/ov-clip/manifest.json`. A temporary add-on is gone when Firefox restarts, which is fine for development.

**Where the button goes.** Firefox has not pinned extension buttons to the toolbar since version 109 — new ones land in the Extensions panel, behind the puzzle-piece icon. Click that, find **Save to OpenViking**, then the gear beside it → **Pin to Toolbar**. Clicking it from inside the panel works too, and counts as the user action that grants `activeTab`.

### Then set it up

```bash
ovx -n                      # make a profile, if you have none
ovx --login <profile>       # sign in through Vault
ovx --install-firefox-host  # let the extension ask ovx for tokens
```

Restart Firefox so it reads the new host manifest, then open the extension's settings and pick the profile. The page says whether `ovx` is answering.

`ovx --uninstall-firefox-host` revokes it again: the extension can then ask for nothing, whatever else it does.

## Where things land

The popup offers two scopes:

| Scope | Default URI | Who can read it |
| --- | --- | --- |
| My files | `viking://~/resources` | you only — user scopes are isolated |
| Shared with the account | `viking://resources` | everyone in the account |

`viking://~` is OpenViking's home alias, resolving to whoever the token says you are — that is what lets the extension work without knowing your user id, since the id comes from the token's claims rather than from anything the browser can see. Both scopes are editable in settings, and blanking one hides it.

The `/resources` on the end is not decoration. The home root also holds `memories/`, `sessions/`, `peers/` and `privacy/`, and importing to it is refused outright with *"to must target resource content"*. All of this is measured against a real OpenViking by the integration suite, not assumed.

Plus a folder below either, `clips` by default. Change it in settings and every
clip follows; change it in the popup and only that save does. Blank it to save
straight into the scope. So a page saved with everything left alone lands at
`viking://~/resources/clips/…`.

What gets written:

- **An article** — one file in a directory of its own, `<folder>/<slug>/`. Its pictures are linked at their original addresses rather than stored: an image imported alongside would be a separate resource, not a sibling file, so a relative link would resolve to nothing.
- **A PDF** — the file as it arrived, also in a directory of its own. If you typed a note or a tag, a small markdown companion goes in `<slug>-notes/` *beside* it, not inside.

**Why every clip gets its own directory.** OpenViking treats a destination as
*the* resource. Import a second document to a destination that already holds
one and it does not merge and does not rename — the first is replaced and gone,
its abstract and overview with it. Two clips sharing a folder would mean each
new one silently destroying the last. The integration suite proves both halves:
that sharing destroys, and that the layout the planner chooses does not.

Two clips of the same title do share a directory, deliberately: re-clipping a
page updates it rather than piling up copies.

OpenViking decides the final name: it wraps the document in a directory and renames the file from the markdown's own `title`, so `the-vault-runbook.md` titled "The Vault Runbook" lands at `…/the-vault-runbook/The_Vault_Runbook.md`. The popup reports where the server says it went rather than composing a path.

Every markdown file opens with frontmatter: title, canonical `source_url` with tracking parameters stripped, hostname, author, publication date, your tags, and when you clipped it.

The PDF is fetched with the site's cookies, so one behind a login downloads as the real file rather than as the login page. What comes back is checked for the PDF header rather than trusted on its `Content-Type` — a site that wants you signed in answers with its login page, HTTP 200, and whatever header it likes, including none. If it is not a PDF the save stops and says so.

The popup shows the destination it resolved to before you save, because a folder name is cleaned on its way into a URI: typing — or picking from the suggestions — `My Clips` saves into `My-Clips`.

## Scripts

| Command | What it does |
| --- | --- |
| `npm run build` | Compile TypeScript and copy the page assets |
| `npm run check` | Type-check |
| `npm run lint` | biome, then build and `web-ext lint` |
| `npm test` | Unit tests (vitest) — fast, offline |
| `npm run test:integration` | Against a real OpenViking (starts a container) |
| `npm run dev` | Build and open Firefox with the extension loaded |
| `npm run package` | Build a distributable `.zip` in `dist/` |

`npm run lint` reports two `UNSAFE_VAR_ASSIGNMENT` warnings against `popup/popup.js`. Both are inside the bundled Readability library, which sets `innerHTML` on documents it parses itself. There are no errors.

## Releasing

Actions → **release**, pick `ov-clip`. The version lives in the git tag: `manifest.json` carries `0.0.0` in a checkout, and the release stamps the tag's version in and checks it took.

With `AMO_JWT_ISSUER` and `AMO_JWT_SECRET` set as repository secrets, the release signs the build through AMO on the `unlisted` channel and attaches a `.xpi` that installs on release Firefox. Without them it attaches an unsigned `.zip` instead — a fork can still cut a release, it is just harder to install. Credentials come from the [AMO Developer Hub](https://addons.mozilla.org/developers/addon/api/key/).

A CI run that touches this package also uploads an unsigned build as a workflow artifact, which is the only practical way to try an extension change before it is released.

## What is tested, and what is not

The suite covers the libraries: naming, URL canonicalization, frontmatter, save planning, the OpenViking client, the ovx bridge, settings and scopes, and the ported image and metadata extractors. One test reads both sides of each page's wiring — every id the script asks for against every id the HTML defines — because `el()` throws at module load and a renamed id would otherwise show up as a blank popup and nothing else.

### Against a real OpenViking

`npm run test:integration` runs the client against an actual server. It starts
`ghcr.io/volcengine/openviking` in a container, creates an account and a user
key, and saves real files — or points at one you are already running when
`$OV_TEST_URL` and `$OV_TEST_KEY` are set. With neither a container runtime nor
those variables it skips, so `npm test` stays runnable anywhere.

Two things the container needs, both learned by watching it refuse to boot and
both encoded in the harness: `dev` auth mode will not bind anything but
localhost, so it runs on `api_key`; and it will not start without an embedding
provider, so a stub OpenAI-compatible endpoint runs in the test process purely
to satisfy the config.

This suite exists because five things were wrong that the stubbed unit tests
could not see, since a stub only proves the client sends what its author
believed it should:

- `viking://~` is refused as a destination — it is a home root, not a document
  folder. The default is `viking://~/resources`.
- An import answers with `root_uri`, and the client was composing
  `<to>/<filename>` instead — a URI to nothing.
- `fs/ls` entries carry no `name` field, so the folder suggestions were always
  empty.
- Failures carry their message at `error.message`, one level into the envelope;
  reading the top level turned "to must target resource content" into
  "OpenViking answered 400".
- Images cannot be stored beside an article, which removed a feature.

Not covered: the popup and options behavior, and a real native-messaging round
trip. Those need a real extension host — `browser.runtime.sendNativeMessage`,
`browser.scripting`, a live tab — which vitest and jsdom cannot stand in for.
The host's own half *is* tested, in `ovx`'s suite: the wire format, the
replies, and that it never hands over a profile's static `api_key`.

Two things are worth doing in a real Firefox before trusting this end to end, in order:

1. `ovx --install-firefox-host`, restart, open the settings, and confirm it says ovx is answering. That exercises the native-messaging round trip, which the integration suite does not cover.
2. **Save one real page.** The integration suite proves the OpenViking half against a real server, but it uses an API key; a Vault identity token is accepted by the same code path and has not been exercised end to end here.

### Why `activeTab` is in the manifest next to `<all_urls>`

It looks redundant and is not. An MV3 host permission is *optional*: Firefox grants `<all_urls>` at install only from version 127, and even then the person can revoke it afterwards. `activeTab` is granted outright when the toolbar button is clicked, which is the only way this popup opens — so it, not `<all_urls>`, is what keeps `tab.url`, `tab.title` and script injection working on the page being clipped for someone who has revoked the rest.

`<all_urls>` is still needed: the background page fetches an article's images from wherever they live, and the popup reaches whatever OpenViking the profile names. Neither has an `activeTab` equivalent.

That is also why `strict_min_version` is **127.0**, not the 115 that everything else here would support. On 115–126 a host permission was never granted at install at all, and this extension asks for none of it back at runtime — so it would install cleanly and then fail every save with a network error. Lowering the floor means adding a `browser.permissions.request()` path, not just editing the number.

## Layout

```
manifest.json          the extension Firefox loads
icons/icon.svg
src/
  background.ts        fetches images past CORS, on the popup's behalf
  types.ts
  lib/
    ovx.ts             asks ovx for a token, over native messaging
    ov-api.ts          OpenViking's REST API — upload, then import
    settings.ts        profile, scopes, and what you picked last time
    extract.ts         tab → article, via Readability and Turndown
    images.ts          find, download and rewrite article images  (ported)
    metadata.ts        author and date from JSON-LD, OG, meta, microdata (ported)
    save.ts            what gets written, and where
    frontmatter.ts     the YAML header
    naming.ts          titles → slugs, folders → URIs
    url.ts             canonical source URLs
  popup/               the save form
  options/             profile picker and scopes
```

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
