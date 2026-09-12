/**
 * The sessions this server is currently holding.
 *
 * The cookie used to *be* the session: an encrypted blob carrying the identity
 * and the OpenViking token, which the server read back on every request and
 * kept no record of. That made signing out a suggestion. It cleared the cookie
 * in the browser that asked, and any copy taken beforehand — off a shared
 * machine, out of a synced profile, from a proxy log — kept working until it
 * expired, because there was nothing to check it against.
 *
 * So the session lives here and the cookie holds only an unguessable id.
 * Signing out deletes the entry, and every copy of that cookie stops working
 * in the same instant. The credential leaves the browser entirely, which is
 * the property the encryption was standing in for.
 *
 * The store is in memory, so a restart signs everybody out. That is the honest
 * trade for holding no database: it fails closed, and the cost is a sign-in
 * after a deploy. It also means **one process**. Two replicas each keep their
 * own sessions, and a person would be signed out every time the load balancer
 * sent them to the other one — running more than one needs a shared store,
 * not this.
 */

import { randomBytes } from "node:crypto";
import type { Viewer } from "../shared/schemas";

/** One live session. */
interface Entry {
  viewer: Viewer;
  /**
   * The OpenViking credential, when the sign-in produced one.
   *
   * Present under the two Vault modes, where the token the sign-in produced
   * *is* the credential — minted under `vault-userpass`, issued by Vault's
   * provider under `vault-oidc`. Absent under `oidc`, where the session carries
   * an identity and the key is resolved per request.
   */
  token?: string;
  /** Unix seconds this session ends at. */
  expiresAt: number;
}

/** A session read back out of the store. */
export interface Session {
  viewer: Viewer;
  token?: string;
  expiresAt: number;
}

function now(): number {
  return Math.floor(Date.now() / 1000);
}

/**
 * Most sessions held at once.
 *
 * A ceiling on memory, not a capacity plan: each entry holds an identity token
 * of roughly a kilobyte, so this is a few tens of megabytes at worst. Signing
 * in needs a valid Vault password, so reaching this honestly would take more
 * people than this dashboard has; reaching it dishonestly needs the passwords
 * anyway. It exists so that a bug or a flood costs memory that is bounded.
 */
const MAX_SESSIONS = 10_000;

/** How often the whole map is walked, at most. */
const SWEEP_INTERVAL_SECONDS = 60;

export class SessionStore {
  private readonly live = new Map<string, Entry>();

  /** When the map was last walked, so hot paths do not walk it again. */
  private sweptAt = 0;

  /**
   * Start a session and return its id.
   *
   * @param viewer - Who the session is for.
   * @param expiresAt - Unix seconds it ends at.
   * @param token - The OpenViking credential, when there is one.
   * @returns The id to put in the cookie.
   */
  create(viewer: Viewer, expiresAt: number, token?: string): string {
    this.sweep();
    // Expired entries are gone by now, so anything still here is live and the
    // oldest of them is the least bad thing to lose. Losing one signs somebody
    // out, which is why the cap is set far above any honest load.
    let evicted = 0;
    while (this.live.size >= MAX_SESSIONS) {
      const oldest = this.live.keys().next().value;
      if (oldest === undefined) break;
      this.live.delete(oldest);
      evicted += 1;
    }
    // Once, not once per eviction: a flood would otherwise bury the log in the
    // one message an operator needs to see.
    if (evicted > 0) {
      console.warn(
        `session store full at ${MAX_SESSIONS}: signed out ${evicted} of the oldest sessions to make room`,
      );
    }

    // 256 bits from the CSPRNG. The cookie is signed as well, but this is what
    // makes the id unguessable — a signature only proves we issued it.
    const id = randomBytes(32).toString("base64url");
    this.live.set(id, { viewer, token, expiresAt });
    return id;
  }

  /**
   * Read a session.
   *
   * @param id - The id from the cookie.
   * @returns The session, or null when it is unknown, dropped, or past its
   *   expiry. An expired entry is deleted on the way out rather than left for
   *   the next sweep.
   */
  read(id: string): Session | null {
    this.sweep();
    const entry = this.live.get(id);
    if (!entry) return null;
    if (entry.expiresAt <= now()) {
      this.live.delete(id);
      return null;
    }
    return { viewer: entry.viewer, token: entry.token, expiresAt: entry.expiresAt };
  }

  /**
   * End a session.
   *
   * Idempotent: signing out twice, or signing out with an id that was never
   * ours, is not an error worth reporting to whoever asked.
   *
   * @param id - The id from the cookie.
   */
  drop(id: string): void {
    this.live.delete(id);
  }

  /** How many sessions are live. Sweeps first, so the count means what it says. */
  size(): number {
    this.sweep(true);
    return this.live.size;
  }

  /**
   * Forget everything past its expiry.
   *
   * Throttled rather than run on a timer: a timer would have to be unref'd to
   * avoid holding the process open, and a walk on every read is a walk on every
   * request. Once a minute is often enough — `read` refuses an expired entry on
   * the spot regardless, so the sweep is about memory, not about correctness.
   *
   * The throttle also keeps `create` linear. Walking the whole map on every
   * insert made a burst of sign-ins quadratic.
   *
   * @param force - Walk now regardless. Used by `size`, whose whole job is to
   *   report a number that is currently true.
   */
  private sweep(force = false): void {
    const cutoff = now();
    if (!force && cutoff - this.sweptAt < SWEEP_INTERVAL_SECONDS) return;
    this.sweptAt = cutoff;
    for (const [id, entry] of this.live) {
      if (entry.expiresAt <= cutoff) this.live.delete(id);
    }
  }
}
