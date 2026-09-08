/**
 * Entry point: check the configuration, then serve the API and the built
 * client from one process.
 */

import { readFile } from "node:fs/promises";
import { serve } from "@hono/node-server";
import { serveStatic } from "@hono/node-server/serve-static";
import { Hono } from "hono";
import { buildServices, createApp } from "./app";
import { loadConfig } from "./env";

const CLIENT_DIR = "./dist/client";

async function main(): Promise<void> {
  const config = loadConfig();

  if (config.OIDC_INSECURE_TLS || config.VAULT_SKIP_VERIFY) {
    // Node offers no per-request way to relax TLS for `fetch`, so this is
    // process-wide. It is loud on purpose: nothing should reach production
    // with it set.
    process.env.NODE_TLS_REJECT_UNAUTHORIZED = "0";
    console.warn(
      "WARNING: TLS verification is disabled for every outbound request " +
        "(OIDC_INSECURE_TLS or VAULT_SKIP_VERIFY is set). Do not run this in production.",
    );
  }

  const services = buildServices(config);
  const app = new Hono();

  app.get("/health", (c) => c.json({ status: "ok" }));
  app.route("/", createApp(services));

  /*
   * Asset filenames carry a content hash, so they can be cached hard and
   * forever. The shell must not be: it names those hashed files, and a browser
   * holding an old copy asks for assets that no longer exist — which looks
   * exactly like the app hanging on load.
   */
  app.use("/assets/*", async (c, next) => {
    await next();
    c.header("cache-control", "public, max-age=31536000, immutable");
  });
  app.use("/assets/*", serveStatic({ root: CLIENT_DIR }));
  app.use("/favicon.svg", serveStatic({ root: CLIENT_DIR }));

  // Anything left is a client-side route, so hand back the shell and let the
  // browser router decide what it means.
  const shell = await readFile(`${CLIENT_DIR}/index.html`, "utf8").catch(() => null);
  app.get("*", (c) => {
    if (!shell) {
      return c.text("Client is not built. Run `npm run build:client`.", 500);
    }
    c.header("cache-control", "no-store, must-revalidate");
    return c.html(shell);
  });

  serve({ fetch: app.fetch, hostname: config.HOST, port: config.PORT }, (info) => {
    console.log(
      `ov-dash listening on http://${config.HOST}:${info.port} ` +
        `(auth=${config.AUTH_MODE}, keys=${config.KEY_SOURCE}, ov=${config.OV_URL})`,
    );
    // Said at boot because it is a property of the deployment, not of the code:
    // sessions live in this process, so a second replica would sign people out
    // at random and a restart signs everyone out at once.
    console.log(
      "sessions are held in memory: run one process, and expect a restart to sign everybody out",
    );
  });
}

main().catch((error: unknown) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});
