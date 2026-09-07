 ▐▛███▛█   Claude Code v2.1.263
▝▜██████▀  Opus 5 · Claude Max
  ▝▝ ▝▝    ~/workspace

  ⎿  SessionStart:startup hook error
  ⎿  Failed with non-blocking status code: /bin/sh: 1: node: not found

❯ I have a user that wants to add files in a nomad job. He's asking if he can   
  use workload identity, or iuf he needds to use his single API key for         
  everything including headless apps?                                           
  ⎿  UserPromptSubmit hook error
  ⎿  Failed with non-blocking status code: /bin/sh: 1: node: not found

  Searched for 3 patterns, read 1 file, listed 2 directories, ran 7 shell commands (ctrl+o to expand)

● Read the source rather than answering from memory: the answer is in
  docs/openviking.md and deployments/applications/secrets.tf. (Note: main is at
  5a0199c; the per-person-account work is still on the unmerged
  loop/OV2-openviking-user-scoped-resources branch, so the below is the state on
  main.)

  Short answer: both, at different layers

  Workload identity gets the key. It can't replace the key.

  OpenViking runs auth_mode: "api_key" and resolves the caller from the key
  alone. There is no header and no claim that changes who it thinks you are
  (docs/openviking.md, "Who you are once inside"). oauth2-proxy already injects
  X-Forwarded-User/-Email and OpenViking reads none of them. So a Nomad job
  cannot present a WI JWT to OpenViking and be somebody.

  The mode that would accept a JWT — oidc — is off on purpose: Web Studio
  hard-refuses oidc and ldap, so turning it on trades away the whole browser
  half of the service. That fork is settled and written down.

  What the job should do

  Exactly what hermes does — that's the working pattern to copy:

  1. Terraform writes a copy of the key under the job's own KV prefix, e.g.
     default/<job_id>/openviking (deployments/applications/secrets.tf:388).
  2. The job declares vault {} and a template that renders it
     (deployments/applications/services/hermes.hcl:383, :401).
  3. Nomad's WI JWT → jwt-nomad → the nomad-workloads role → a policy granting
     read on secret/data/<namespace>/<job_id>/* only.

  So workload identity does the real work: no key in the jobspec, no key on disk
  in git, and another job reading that path gets a 403 and never starts. The
  copy-under-the-consumer step is not optional — the job cannot read
  default/openviking-users/jasper, that prefix is deliberately outside its
  grant.

  For an in-cluster job, talk to http://<openviking-host>:1933 directly, like
  hermes does. openviking-api.lab.orangecluster.nl is for callers outside the
  cluster; openviking. is browsers only and needs a Vault session cookie a job
  can't hold.

  He does not need his key for everything

  A headless app can have its own OpenViking user — that's one line in
  local.openviking_users (secrets.tf:274) plus the KV copy under the job's
  prefix. The deciding question is where the files have to land:

  - Into his own scope (viking://user/jasper), so he sees them in Studio: the
    job must hold his key. User scopes are isolated absolutely — measured, not
    assumed: jasper as ADMIN gets 403 on another user's scope and vice versa,
    and no role grants a cross-user read. This is why hermes holds jasper's key
    rather than its own.
  - Shared with the account (viking://resources): give the job its own user. It
    writes as itself and everyone in lab can read it.

  Two costs worth telling him up front:

  - Every key derives from one seed, so revoking the job's key means rotating
    the seed, which rotates every person's key too. Separate user, shared blast
    radius.
  - If OV2 lands (one account per person), viking://resources becomes a tree of
    one, so the "shared" option above disappears and the choice collapses to:
    borrow his key, or be invisible to him.

✻ Sautéed for 1m 58s · done 4:48 PM

※ recap: You asked whether a Nomad job that adds files to OpenViking can use workload identity or needs a personal API key; I answered from the repo. Next: decide whether the job writes into jasper's scope or a shared one. (disable recaps in /config)

❯ When openviking has OAuth turned on, how does the user authenticate with the  
  CLI, and with the API?                                                        
  ⎿  UserPromptSubmit hook error
  ⎿  Failed with non-blocking status code: /bin/sh: 1: node: not found

  Searched for 4 patterns, listed 1 directory, ran 3 shell commands (ctrl+o to expand)

● Two different things get called "OAuth" here, and they give opposite answers.
  All of this was measured during OV1 and is recorded in
  .loop/plans/OV1-openviking-service.md with upstream source links.

  1. auth_mode: "oidc" — Vault as the identity provider

  Browser: works. oauth2-proxy runs the authorization_code flow against Vault,
  then forwards the ID token upstream as Authorization: Bearer
  (OAUTH2_PROXY_PASS_AUTHORIZATION_HEADER, P6). OpenViking's OIDC plugin fetches
  Vault's discovery document and JWKS at request time and verifies the token.
  The user just logs into Vault and never handles a token.

  CLI: no route at all. auth_mode picks exactly one plugin. There is no chain
  and no API-key fallback, so under oidc the key in ~/.openviking/ovcli.conf is
  simply not accepted (P8, server/app.py). ov has no other credential to offer.

  API: works, but you can only get a token through a browser. The server accepts
  Authorization: Bearer <vault id token> fine. Minting one is the problem.
  Vault's lab provider advertises grant_types_supported: ["authorization_code"]
  and nothing else (P3, measured against its discovery document on 2026-09-05):

  - no device flow, so no ov login --device style pairing
  - no client_credentials, so no headless service login
  - no refresh grant, so the token dies in about an hour with no renewal

  The only way to hold one is to complete a browser redirect and copy the ID
  token out by hand, every hour. That is why OV1 recorded "nothing headless can
  call OpenViking" and settled on browser-only, then the follow-up moved the
  whole service to api_key.

  Two more consequences of oidc mode worth knowing:

  - No admin path. The OIDC plugin returns role=USER unconditionally and never
    calls map_role; root_api_key is not consulted (P7). Nobody can manage
    accounts or users.
  - Web Studio does not start. It hard-refuses serverMode === 'oidc'. So you get
    the authenticated session and no UI. docs/openviking.md:86 puts it plainly:
    under oidc the API works and the entire browser half of the service does
    not.

  2. OpenViking's own OAuth 2.1 — the ovat_ tokens

  This is not an alternative to the API key, it is a layer on top of it (P9). It
  is configured on AuthMode.API_KEY, and an ovat_ token resolves to the same
  identity, with the same permissions, as the API key that produced it. Vault
  cannot back it. It changes the credential format a client carries, mostly for
  MCP clients, and changes nothing about who OpenViking thinks you are or where
  the trust comes from.

  So if he turns this on, the CLI and API still start from his API key. He has
  just wrapped it.

  Bottom line

  The thing he probably wants, a Vault login that works from the CLI, exists on
  neither path. Vault's provider has no non-interactive grant, so no OAuth mode
  reaches a terminal or a Nomad job. Browsers can have OAuth; CLI and API
  callers hold the API key.

  The route that actually closes this is docs/openviking-dashboard.md: a
  dashboard we own holds each person's key server-side and resolves who they are
  from the Vault OIDC session the edge already establishes. That fixes the
  browser half. The CLI and API keep the key either way.

✻ Brewed for 1m 25s · done 5:52 PM

❯ - Web Studio does not start. It hard-refuses serverMode === 'oidc'. So you    
  get the authenticated session and no UI. docs/openviking.md:86 puts it        
  plainly: under oidc the API works and the entire browser half of the service  
  does not.                                                                     
                                                                                
  And how, pray, is a user to add e.g. resources if OAuth is turned on?         
  ⎿  UserPromptSubmit hook error
  ⎿  Failed with non-blocking status code: /bin/sh: 1: node: not found

  Searched for 2 patterns, ran 1 shell command (ctrl+o to expand)

❯ And the CLI doesn't work nor the web ui                                       
  ⎿  UserPromptSubmit hook error
  ⎿  Failed with non-blocking status code: /bin/sh: 1: node: not found

  Ran 1 shell command (ctrl+o to expand)

● Straight answer: they can't. Under auth_mode: "oidc" there is no usable way to
  add a resource. That is not a gap in the setup, it is the finding that killed
  the OIDC design.

  Every write surface, and what happens to each:

  Surface: Web Studio's add-resource form
  Under oidc: Refuses to start. serverMode === 'oidc' is an unsupported auth
  mode.
  ────────────────────────────────────────
  Surface: ov CLI
  Under oidc: Its only credential is an API key. One plugin, no chain, no
  fallback (P8), so the key is rejected.
  ────────────────────────────────────────
  Surface: WebDAV mount (/webdav/resources)
  Under oidc: Needs the same bearer token. Desktop clients offer Basic auth or a

  fixed header, not an hourly token. Whether the WebDAV router accepts a bearer
   at all under oidc was never tested.
  ────────────────────────────────────────
  Surface: /mcp
  Under oidc: Same credential problem as the CLI.
  ────────────────────────────────────────
  Surface: curl -H "Authorization: Bearer <id token>"
  Under oidc: Works. And only by hand.

  So the last route standing is a person completing a browser redirect against
  Vault, pulling the ID token out of the flow, and curling POST with it before
  it expires in about an hour, with no refresh grant to renew it (P3). Nobody
  adds a document library that way.

  And even then it lands badly: the OIDC plugin returns role=USER
  unconditionally, account_id defaults to the fixed string default, and
  root_api_key is not consulted (P7, P8). No admin path, and everyone piles into
  one account unless the identity mapping is configured.

  This is exactly how the design collapsed. OV1 settled Q3 as "ship browser
  access through the proxy, defer headless to its own ticket". Then Studio's
  refusal turned out to mean the browser half did not exist either, so the
  option OV1 had listed and rejected, auth_mode: "api_key" with humans gated at
  the proxy, became the only one left. That is what runs today.

  If he wants the Vault login to be what a person actually uses,
  docs/openviking-dashboard.md is the route: our own dashboard holds each
  person's key server-side and reads who they are from the headers oauth2-proxy
  already injects. The server stays on api_key; the user never sees a key or a
  token. Turning on OpenViking's own OAuth does not help, since it sits on top
  of api_key and resolves ovat_ tokens to the same identity the key would give
  (P9).

  One correction to my first answer: HEAD moved to fc76a61 during this session,
  so OV2 is merged now. One account per person is live, not on a branch.

✻ Crunched for 1m 10s · done 5:56 PM

※ recap: We've been answering how OpenViking authentication works for a Nomad job and for people. Conclusion so far: under OIDC nobody can add resources, so the API key stays. Next: tell your user that, or start the dashboard route. (disable recaps in /config)

❯ No, the last route standing is forking OV and adding it ourselves             
  ⎿  UserPromptSubmit hook error
  ⎿  Failed with non-blocking status code: /bin/sh: 1: node: not found

● You're right, I left that off. Let me check what the fork actually costs
  before agreeing it's cheap.

  Searched for 1 pattern, listed 1 directory, ran 14 shell commands (ctrl+o to expand)

● Fair. And checking it changes the picture — the premise in our doc is wrong.

  The fork is feasible

  At v0.4.17.1, web-studio/ ships as TypeScript/React source in the upstream
  repo (387 files under web-studio/), not a compiled bundle. Same for the CLI:
  crates/ov_cli/ is Rust source. So there is nothing stopping us patching
  either.

  But the thing we'd be forking around does not do what the doc says

  docs/openviking.md:80 says Web Studio "refuses to run against oidc and ldap
  outright". I read the actual file at the pinned tag. The guard appears in
  exactly one place:

  web-studio/src/routes/settings/route.tsx:198 — the settings page, not the app
  shell. Its only effect is at line 290: where the credential form would render,
  it renders a destructive alert instead, saying the mode is unsupported and
  pointing at ov and the upstream auth guide. Studio itself keeps rendering.

  Two more things I checked at the same tag:

  - The client does not require a key. lib/ov-client/client.ts:187-191 sets
    X-API-Key through setOptionalHeader, which deletes the header when the value
    is empty rather than refusing the request. So with no key configured,
    Studio still issues requests, just without that header.
  - Which means our proxy already supplies the credential. oauth2-proxy injects
    Authorization: Bearer <id token> on everything it forwards, including
    Studio's own XHRs. The OIDC plugin verifies exactly that.

  So the likely truth is that Studio mostly works under oidc and only its
  credential-entry page is dead, which is correct behavior, because under oidc
  there is no key to enter.

  What would actually break, and it is smaller

  - use-app-connection.tsx:243-247: with no apiKey, the role probe is skipped
    and connectionRole stays 'unknown'. Role-gated UI (users page, account
    switcher, admin surfaces) degrades or hides.
  - shouldRedirectToLoginOnApiError bounces to /settings on any 401. If the
    bearer does not arrive or does not verify, you land on the one page that has
    nothing to offer you. That is the failure the doc describes, but it is a
    consequence of the token not arriving, not of a hard refusal.
  - P7 still stands and is server-side: under oidc every caller is role USER, so
    the admin surfaces have nothing behind them anyway.

  So the decision is not fork-or-nothing

  Before forking anything, run the server with auth_mode: "oidc" behind the
  existing proxy and open Studio. That measurement is an afternoon and it
  decides between three very different outcomes: it already works, it needs a
  handful of lines in the settings route, or it needs real work.

  And weigh the fork shape honestly if it comes to that. ov-postgres is cheap to
  carry because it is a pip-installable adapter dropped into a derived image.
  Patching Studio means vendoring the frontend and rebuilding it on every
  upstream bump. Different burden, same repo.

  Two caveats on what I just told you: the "one file only" result is GitHub code
  search, which runs on the default branch. Every file I quoted, I fetched at
  ref=v0.4.17.1. And nobody has run this — I read code paths, I did not observe
  Studio under oidc.

  Want me to correct docs/openviking.md:80-86? The sentence there is
  load-bearing for the whole "why not Vault as the identity" section, and it is
  currently overstated.

✻ Crunched for 2m 54s · done 6:08 PM

❯ Ok So OAuth would be a rust change, or a python change?                       
  ⎿  UserPromptSubmit hook error
  ⎿  Failed with non-blocking status code: /bin/sh: 1: node: not found

  Ran 7 shell commands (ctrl+o to expand)

● Neither, for the part you'd expect. I read the actual source at v0.4.17.1.

  The Rust CLI already speaks OIDC

  crates/ov_cli/src/config.rs:92,100 carries two config fields:

  pub auth_mode: Option<String>,
  pub oidc_token: Option<String>,

  And base_client.rs:324-333 acts on them:

  // OIDC Bearer Token support
  if let (Some(auth_mode), Some(token)) = (&self.auth_mode, &self.oidc_token) {
      if auth_mode == "oidc" {
          ... headers.insert(AUTHORIZATION, "Bearer {token}")

  There is an LDAP Basic-auth branch right above it too. So the CLI can already
  authenticate against an OIDC-mode server. That corrects what I told you two
  turns ago: I said the CLI had no route under oidc. It has one, and it needs no
  fork.

  The gap is getting a token, and that is a Vault problem, not an OpenViking one

  There is no auth.rs or login.rs in crates/ov_cli/src/commands/. The CLI can
  send a bearer token; it cannot obtain one. You would hand-place a JWT in the
  config file.

  Writing an ov login would be a Rust change, and it would be a Rust change with
  nothing to call. Vault's lab provider advertises grant_types_supported: 
  ["authorization_code"] and nothing else, no device flow, no
  client_credentials, no refresh (P3, measured against its discovery document).
  So the token still comes from a browser round trip and still dies in an hour.
  Forking the CLI buys you a nicer way to paste an expiring token.

  The change worth making is Python, and it is server-side

  Two places, both in the Python server:

  What: One plugin, no chain, no fallback
  Where: openviking/server/app.py:102-131
  Buys: Let oidc and api_key coexist: Vault for browsers, keys for CLI and Nomad

  jobs. This removes the trade entirely.
  ────────────────────────────────────────
  What: Role is always USER, map_role never called
  Where: openviking/server/auth/plugins/oidc.py:121-144
  Buys: An admin path under oidc. Today there is none.

  The first one is the fork that solves your actual problem. It is also the
  cheapest burden shape: we already build a derived image and already carry
  openviking_extensions, so shipping a patched server is machinery that exists.
  One wrinkle to think through before committing: /health reports auth_mode, and
  Studio's detectServerMode branches on that single string, so a chained server
  has to decide what it calls itself.

  So: TypeScript for Studio (probably nothing, per last turn), Rust for a login
  command that Vault can't back, Python for the one change that pays.

  One thing I noticed while reading, unrelated but real. The JWT fallback at
  base_client.rs:335-347 fires when the key contains two or more dots. An
  OpenViking API key is base64url(account).base64url(user).base64url(secret) —
  exactly two dots. So ov is sending our API keys as Authorization: Bearer <key>
  on every request today, alongside X-API-Key. Harmless to the server, which
  reads X-API-Key, but it means the key lands in a second header that anything
  on the path may log.

✻ Worked for 1m 46s · done 6:12 PM

※ recap: We're deciding how a Nomad job and the CLI authenticate to OpenViking, and whether OAuth is worth forking upstream for. Next: choose whether to patch the Python server so oidc and api_key can coexist. (disable recaps in /config)
❯ Like this?                                                                    
                                                                                
  https://github.com/JasperHG90/memex/blob/main/packages/core/src/memex_core/se 
  rver/auth.py                                                                  
  ⎿  UserPromptSubmit hook error
  ⎿  Failed with non-blocking status code: /bin/sh: 1: node: not found

  Ran 7 shell commands (ctrl+o to expand)

● Yes — that shape exactly. And better than "like this": you already built it,
  reviewed it, and it's running.

  authenticate_request (auth.py:107-139) is precisely the thing OpenViking
  lacks: try X-API-Key, fall back to Authorization: Bearer against a verifier,
  both paths return one AuthContext. And oidc.py:100-133 _claims_to_context does
  the half OpenViking's plugin refuses — maps claims to a policy through grant
  rules with a default_policy fallback. OpenViking's OIDC plugin never calls
  map_role and returns role=Role.USER unconditionally (P7). Your memex code is
  the missing branch, written.

  And it does not need a fork

  I read openviking/server/auth/registry.py and app.py:95-131. The auth plugin
  system is a public extension point:

  class AuthPluginRegistry:
      def register(self, plugin_class: type[T]) -> type[T]:
          mode = plugin_class.auth_mode
          ...

  _initialize_auth calls get_registry(), then registers the three built-ins only
  if the mode is not already taken:

  if registry.get("api_key") is None:
      registry.register(ApiKeyAuthPlugin)

  So an external package can register a chained plugin under a new mode name, or
  claim api_key first and override the built-in outright. That is the same
  extension shape as ov-postgres: a pip-installable package in
  openviking_extensions, installed into the derived image we already build,
  selected by one line in ov.conf.json. A commit in a repo you own, not a
  vendored fork you re-apply on every upstream bump.

  The open packaging question is how the registration gets imported before
  _initialize_auth runs. ov-postgres already solved the equivalent problem for
  the vector backend, so the answer is probably sitting in your own extensions
  repo.

  The part that does not carry over

  In memex, AuthContext is a permission gate: policy, permissions, vault ids. In
  OpenViking the identity is the data path. account_id is the first segment of
  every stored blob (/local/<account_id>/...) and is ANDed into every vector
  query. So a bearer token has to resolve to the same (account_id, user_id) the
  API key resolves to, or one person sees two different trees depending on which
  credential they happened to use.

  Concretely: post-OV2 the account and the user are both the person's name,
  jasper. Vault's ID token sub is an entity ID, not that string. Vault's lab
  provider offers scopes ["email","groups","openid"] (P3), so email is the
  obvious candidate to map from, or a custom scope template. That mapping is the
  actual design work, and it is the one thing memex did not have to solve.

  One trap you already hit and documented in authenticate_request's docstring,
  which applies unchanged: never raise inside BaseHTTPMiddleware, it bypasses
  FastAPI's exception handlers and surfaces as a 500. OpenViking is FastAPI too.

  Want me to draft this as a ticket? The shape I'd propose: a chained auth
  plugin in openviking_extensions, claims-to-identity mapping as the named
  design fork, and the packaging question answered first since it decides
  whether this is a plugin or a fork after all.

✻ Churned for 1m 25s · done 6:31 PM

※ recap: You want OpenViking to accept Vault OIDC alongside API keys, and we found it needs no fork: a chained auth plugin can ship in your openviking_extensions repo. Next, say whether I should draft that as a ticket.

❯ Hold on, are you saying this can be shipped as an extension?                  
  ⎿  UserPromptSubmit hook error
  ⎿  Failed with non-blocking status code: /bin/sh: 1: node: not found

● Let me not overclaim. Two things decide it, and I only verified one. Checking
  both.

  Ran 5 shell commands (ctrl+o to expand)

● Yes — and more firmly than I put it last time, because upstream built this on
  purpose. But one thing I said is wrong.

  Custom auth modes are a documented extension point

  openviking/server/identity.py:66-74:

  class AuthMode(str, Enum):
      """Authentication modes for OpenViking server.

      Built-in modes. Custom modes are plain strings not in this enum.
      """

  The config field is a plain string, not the enum (config.py:335), and the
  loader has a branch written for exactly this:

  try:
      server_data["auth_mode"] = AuthMode(server_data["auth_mode"]).value
  except ValueError:
      # Custom auth mode — keep as string for plugin registration
      pass

  Registration is public: register_auth_plugin is a decorator on a module-level
  singleton, and its own docstring shows the usage. Your plugin also gets a
  validate_config(config) hook called at startup (config.py:586), so it can
  refuse a bad config the way the built-ins do.

  The correction: you cannot claim api_key

  Last turn I said you could register api_key first and override the built-in,
  because _initialize_auth guards with if registry.get("api_key") is None.
  validate_server_config runs earlier and undoes it (config.py:553-575):

  elif existing is not plugin_cls:
      logger.warning(
          "SECURITY: Auth mode %r was registered by %s but is being "
          "overridden by the built-in %s.", ...)
      registry._plugins[mode] = plugin_cls

  It detects the hijack, logs it as a security event, and forcibly restores the
  built-in. So the plugin has to live under a new name — vault_chain, say — and
  ov.conf.json sets auth_mode to that.

  What's still unresolved

  Nothing imports your module for you. Both _initialize_auth and
  validate_server_config import only the three built-ins, and I found no
  entry-point scan. So the decorator has to run before startup, and that is the
  one open question.

  The route that needs no upstream support: ship a thin console script in
  openviking_extensions that imports the plugin and then delegates to
  openviking_cli.server_bootstrap:main. We already control the derived image's
  command, so that is a line in Dockerfile.openviking and a line in the jobspec.
  Worth first checking how ov-postgres gets itself registered for the vector
  backend, since it faced the same problem and you own that answer.

  So the shape is: a plugin package next to ov-postgres, your memex
  authenticate_request chain as its resolver, and the claims-to-(account_id, 
  user_id) mapping as the design fork. No fork of OpenViking, no vendored
  frontend, no Rust.

  Still want that ticket drafted?

✻ Crunched for 1m 10s · done 6:53 PM

※ recap: Goal: let OpenViking users authenticate with Vault OIDC instead of API keys. We found it can ship as a custom auth plugin next to ov-postgres, no fork needed. Next: check how ov-postgres registers itself, then draft the ticket.

❯ https://github.com/JasperHG90/openviking_extensions/tree/main/packages/ov-pos 
  tgres                                                                         
  ⎿  UserPromptSubmit hook error
  ⎿  Failed with non-blocking status code: /bin/sh: 1: node: not found

  Ran 5 shell commands (ctrl+o to expand)

● That answers it, and it rules out the easy route.

  ov-postgres works by a mechanism the auth system doesn't have

  ov_postgres/__init__.py says how it's selected:

  "vectordb": {
    "backend": "ov_postgres.adapter.PgVectorCollectionAdapter",
    "custom_params": {"dsn": "..."}
  }

  That's a dotted import path, resolved at runtime by
  openviking/storage/vectordb_adapters/factory.py, which is one of the 36
  import_module sites in the repo. So ov-postgres never needs anyone to import
  it first. Config names the class, the factory imports it. No entry points, no
  glue — which is why the package has none.

  Auth has no equivalent. registry.get(effective_auth_mode) is a dict lookup on
  a mode name, and there is no import_module anywhere in app.py, config.py, or
  registry.py. I checked all three. So the class has to already be in the
  registry, and nothing in the startup path will import a third-party module to
  put it there.

  Upstream supports custom auth modes in config and gives no way to load one.
  That is an unfinished extension point, and the vectordb factory is their own
  house pattern for finishing it.

  So it's still an extension, plus one piece of glue

  Three ways to get the decorator to run, best first:

  1. A wrapper console script in the extension package. It imports the plugin,
     then calls openviking_cli.server_bootstrap:main. Ten lines. We already own
     the derived image's command, so it's one line in Dockerfile.openviking and
     one in the jobspec. No upstream change, nothing spooky.
  2. A .pth file in site-packages. Works, and makes every Python process in the
     image import the plugin. Avoid.
  3. A PR upstream adding a dotted-path escape hatch to the auth registry,
     mirroring the vectordb factory. Small, principled, and it retires our
     wrapper. Worth filing whether or not we wait for it.

  One trap to note before you write it: Dockerfile.openviking installs
  ov-postgres from a git URL pinned at ov-postgres-v0.2.0, where the repo had
  src/ at the root. main has since moved to packages/ov-postgres, so a second
  package from this repo needs the subdirectory fragment in its install URL.
  That is the same drift the Dockerfile's comment already records, coming back
  for the new package.

  So the full shape, with nothing left unverified except the claims mapping:

  - ov-auth-vault in openviking_extensions, registering auth_mode = 
    "vault_chain" via register_auth_plugin
  - the resolver: your memex authenticate_request chain, X-API-Key first,
    Authorization: Bearer second
  - a wrapper entrypoint so the plugin is imported before startup
  - the design fork: mapping Vault claims to the same (account_id, user_id) the
    API key derives, given sub is an entity ID and the provider offers email,
    groups, openid

  Say the word and I'll draft the ticket.

✻ Sautéed for 55s · done 7:06 PM

❯ How does (1) work?                                                            
  ⎿  UserPromptSubmit hook error
  ⎿  Failed with non-blocking status code: /bin/sh: 1: node: not found

  Searched for 1 pattern, ran 1 shell command (ctrl+o to expand)

● It's just "import the module, then start the server" — because registration is
  a side effect of import.

  register_auth_plugin is a decorator on a module-level singleton
  (_global_registry in registry.py). So the moment Python imports a module
  containing:

  @register_auth_plugin
  class VaultChainAuthPlugin(AuthPlugin):
      auth_mode = "vault_chain"

  the class is in the registry, process-wide, before anything else runs. The
  startup path then finds it by name at registry.get(effective_auth_mode).
  Nothing else is needed; the only problem is that nothing imports our module
  today.

  The wrapper

  One file in the extension package:

  # src/ov_auth_vault/serve.py
  import ov_auth_vault.plugin          # noqa: F401 — registers "vault_chain"
  from openviking_cli.server_bootstrap import main

  if __name__ == "__main__":
      main()

  main() reads sys.argv itself — it pre-parses --config and dispatches on
  sys.argv[1] for init, doctor and ingest (server_bootstrap.py:59-98). The
  wrapper must not touch argv, and this one doesn't: sys.argv[1:] arrives
  unchanged.

  Getting it into the image and invoked

  The install line mirrors the one already in Dockerfile.openviking, with the
  subdirectory fragment the new repo layout needs:

  RUN /usr/local/bin/python3.13 -m pip install --no-deps --upgrade \
      --target /app/.venv/lib/python3.13/site-packages \
      "ov-auth-vault @ git+https://github.com/JasperHG90/openviking_extensions@o
  v-auth-vault-v0.1.0#subdirectory=packages/ov-auth-vault"

  Then the job runs the wrapper instead of the image's default command. The
  jobspec's config block currently sets no command or args at all
  (openviking.hcl:67-71), so it inherits the base image's entrypoint. You'd add:

  hcl
  config {
    image        = "${openviking_image}"
    ports        = ["http"]
    network_mode = "host"
    command      = "/app/.venv/bin/python"
    args         = ["-m", "ov_auth_vault.serve"]
  }

  And ov.conf.json flips "auth_mode": "api_key" to "vault_chain".

  Two traps

  Use python -m, not a console script. The image installs with pip --target,
  which is a package-tree install; whether console scripts land somewhere on
  PATH depends on the pip version and is not worth betting on. -m needs no
  script shim, so the existing install pattern works unchanged.

  Read the base image's ENTRYPOINT before you replace it. I don't know what
  ghcr.io/volcengine/openviking:v0.4.17.1 sets, and if that entrypoint does
  setup beyond calling server_bootstrap, overriding command skips it. docker 
  inspect the base image and reproduce whatever it does. Embark's Dockerfile hit
  the same class of problem and its comment records the entrypoint's exec
  behavior; this one has to be checked, not assumed.

  The upside of this shape is that it's all in things we already own: a package
  in your extensions repo, two lines in a Dockerfile we already build, three in
  a jobspec we already template. The only upstream dependency is that custom
  mode strings keep working, which their own config loader has a branch for.

✻ Baked for 45s · done 7:08 PM