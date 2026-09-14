import { svelte } from "@sveltejs/vite-plugin-svelte";
import { defineConfig } from "vitest/config";

/**
 * The fast offline suite.
 *
 * Two kinds of test share it. Most run in `node` and drive the Hono app or a
 * plain module directly. A few mount a Svelte component, and those declare
 * `@vitest-environment jsdom` in their own docblock rather than switching the
 * whole suite — a DOM the other three hundred tests do not need costs them
 * startup and would let one accidentally come to depend on `window`.
 *
 * The svelte plugin is here so a `.svelte` import compiles at all. `hot: false`
 * because there is nothing to hot-reload in a test run.
 *
 * Vitest is held at 3.x for this, and that is a real constraint rather than
 * housekeeping: 2.x bundles its own Vite 5 while this package builds on Vite 6,
 * so `vitest/config` typed the plugin list against a different copy of Vite than
 * the svelte plugin is compiled against, and tsc rejected the plugin with four
 * screens about two `PluginOption`s that read identically.
 */
export default defineConfig({
  plugins: [svelte({ hot: false })],
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
    // Fills in the handful of browser globals jsdom lacks. A no-op under node.
    setupFiles: ["tests/setup.ts"],
  },
  resolve: {
    /*
     * Load the browser build of Svelte, not the server one.
     *
     * Load-bearing, measured: without it all twelve component tests fail. Vitest
     * runs in node, so node resolution picks Svelte's SSR entry — which renders
     * to a string and has no events, no `mount`, and nothing for a click to do.
     */
    conditions: ["browser"],
  },
});
