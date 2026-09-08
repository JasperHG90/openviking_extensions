/**
 * The integration suite, kept out of `npm test`.
 *
 * These talk to a real Vault over the network. The unit suite has to stay fast
 * and offline — it runs on every commit — so the two are separate configs
 * rather than one config with a filter someone can forget.
 */
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/**/*.integration.ts"],
    // A real Vault answers in milliseconds, but a cold container does not.
    testTimeout: 30_000,
  },
});
