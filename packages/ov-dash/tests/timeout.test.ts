/**
 * The OpenViking timeout is in milliseconds.
 *
 * The Python SDK takes seconds and the TypeScript one does not, so dividing by
 * 1000 on the way in gave every call a 30ms budget. Nothing local noticed: a
 * stubbed fetch answers instantly, so only a real server ever timed out. These
 * tests put a delay in front of the stub, which is what makes the unit matter.
 */

import { afterEach, describe, expect, it, vi } from "vitest";
import { buildServices, createApp } from "../src/server/app";
import { loadConfig } from "../src/server/env";

function configWith(timeoutMs: string) {
  return loadConfig({
    OV_URL: "http://openviking:1933",
    SESSION_SECRET: "a".repeat(32),
    AUTH_MODE: "dev",
    DEV_USER: "jasper",
    KEY_SOURCE: "env",
    OV_API_KEY: "ov_test",
    OV_TIMEOUT_MS: timeoutMs,
  });
}

/** Answer OpenViking calls after `delayMs`, honouring the abort signal. */
function stubSlowOv(delayMs: number) {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      (_input: string | URL | Request, init?: RequestInit) =>
        new Promise((resolve, reject) => {
          const timer = setTimeout(
            () =>
              resolve(
                new Response(JSON.stringify({ status: "ok", result: [], time: 0 }), {
                  headers: { "content-type": "application/json" },
                }),
              ),
            delayMs,
          );
          init?.signal?.addEventListener("abort", () => {
            clearTimeout(timer);
            reject(new DOMException("aborted", "AbortError"));
          });
        }),
    ),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the OpenViking timeout budget", () => {
  it("lets a call that takes 200ms finish when the budget is 5000ms", async () => {
    stubSlowOv(200);
    const app = createApp(buildServices(configWith("5000")));
    const response = await app.request("/api/tree");
    // Divided by 1000 this budget is 5ms, and this call fails.
    expect(response.status).toBe(200);
  });

  it("gives up on that same call when the budget is 50ms", async () => {
    stubSlowOv(200);
    const app = createApp(buildServices(configWith("50")));
    const response = await app.request("/api/tree");
    expect(response.status).toBe(502);
  });

  it("defaults to a budget that survives a slow grep", () => {
    // Measured against v0.4.17.1: one grep term takes about twenty seconds, so
    // a 30s default timed out exact search while every other page looked fine.
    const config = loadConfig({
      OV_URL: "http://openviking:1933",
      SESSION_SECRET: "a".repeat(32),
      AUTH_MODE: "dev",
      KEY_SOURCE: "env",
      OV_API_KEY: "ov_test",
    });
    expect(config.OV_TIMEOUT_MS).toBeGreaterThanOrEqual(60_000);
  });
});
