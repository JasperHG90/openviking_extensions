/**
 * The OpenViking client, against a real OpenViking.
 *
 * The unit suite stubs `fetch` and so can only prove the client sends what the
 * author believed it should. This proves the server agrees: that the endpoints
 * exist, the field names are right, the auth header is accepted, and a saved
 * page comes back out at a URI that is really there.
 *
 * Four things in this file were wrong before it existed, and are the reason it
 * does: `viking://~` is not a writable destination, a resource lands in a
 * directory OpenViking names, that directory's file is renamed from the
 * markdown's own title, and two imports to the same new destination replace
 * each other.
 */

import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { type Session, addResource, listFolders } from "../../src/lib/ov-api";
import { planArticleSave } from "../../src/lib/save";
import { type Live, listTree, startOpenViking, stopOpenViking } from "./harness";

let live: Live | null = null;
let session: Session;

/**
 * Retry until a condition holds.
 *
 * An import answers once the file is stored and before it is indexed, so a
 * listing taken immediately afterwards can still be empty. Polling states that
 * plainly instead of hiding it in a fixed sleep.
 */
/** Let an import settle before the next one touches the same tree. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 4000));

async function eventually<T>(
  read: () => Promise<T>,
  done: (value: T) => boolean,
  attempts = 20,
): Promise<T> {
  let last = await read();
  for (let i = 0; i < attempts && !done(last); i++) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    last = await read();
  }
  return last;
}

beforeAll(async () => {
  live = await startOpenViking();
  if (live) session = { url: live.url, token: live.key };
}, 240_000);

afterAll(async () => {
  await stopOpenViking();
}, 60_000);

/** Skip the body when there is no server, rather than fail. */
function needsServer(): boolean {
  if (!live) {
    console.warn(
      "no OpenViking: set $OV_TEST_URL and $OV_TEST_KEY, or make docker available",
    );
    return true;
  }
  return false;
}

const md = (title: string, body = "Body text.") =>
  new TextEncoder().encode(`---\ntitle: "${title}"\n---\n\n${body}\n`);

describe("talking to a real OpenViking", () => {
  it("is accepted by the token, on the header the client actually sends", async () => {
    if (needsServer()) return;
    // The client sends the credential as both X-API-Key and a bearer. If the
    // server took neither, this throws.
    await expect(listFolders(session, "viking://~")).resolves.toBeInstanceOf(Array);
  });

  it("rejects a bad token rather than answering anyway", async () => {
    if (needsServer()) return;
    await expect(
      listFolders({ url: session.url, token: "not-a-key" }, "viking://~"),
    ).rejects.toThrow();
  });

  it("resolves viking://~ to the token's own user", async () => {
    if (needsServer()) return;
    const entries = await listTree(live as Live, "viking://~");
    // The home alias is what lets the extension work without knowing a user
    // id: the server fills it in from the credential.
    expect(
      entries.every((uri) => uri.startsWith(`viking://user/${(live as Live).user}/`)),
    ).toBe(true);
  });
});

describe("where a save may actually be written", () => {
  it("refuses viking://~ itself, which is a home root and not a document folder", async () => {
    if (needsServer()) return;
    // Listing it shows why: memories/, sessions/, privacy/ live there. The
    // server says "to must target resource content". This is the finding that
    // moved the extension's default to viking://~/resources.
    await expect(
      addResource(session, {
        bytes: md("Rejected"),
        filename: "rejected.md",
        contentType: "text/markdown",
        to: "viking://~",
      }),
    ).rejects.toThrow(/resource content|INVALID_URI/i);
  });

  it("accepts the user scope the extension defaults to", async () => {
    if (needsServer()) return;
    const uri = await addResource(session, {
      bytes: md("Default Scope"),
      filename: "default-scope.md",
      contentType: "text/markdown",
      to: "viking://~/resources",
    });
    expect(uri).toContain(`viking://user/${(live as Live).user}/resources/`);
  });

  it("accepts the shared scope the extension offers", async () => {
    if (needsServer()) return;
    const uri = await addResource(session, {
      bytes: md("Shared Scope"),
      filename: "shared-scope.md",
      contentType: "text/markdown",
      to: "viking://resources",
    });
    expect(uri.startsWith("viking://resources/")).toBe(true);
  });
});

describe("what a saved page turns into", () => {
  it("reports where the file really is, not where the client guessed", async () => {
    if (needsServer()) return;
    // OpenViking wraps each imported document in a directory it names, and
    // renames the file from the markdown's own `title`. So `report.md` titled
    // "Quarterly Review" does not land at `<to>/report.md`, and a client that
    // composed that path would hand back a URI to nothing.
    const uri = await addResource(session, {
      bytes: md("Quarterly Review"),
      filename: "report.md",
      contentType: "text/markdown",
      to: "viking://~/resources",
    });

    // Indexing is asynchronous, so the tree is polled rather than read once.
    const written = await eventually(
      () => listTree(live as Live, "viking://~/resources"),
      (tree) => tree.includes(uri),
    );
    expect(written).toContain(uri);
    expect(uri).not.toContain("/report.md");
  });

  it("puts a PDF where it says it did", async () => {
    if (needsServer()) return;
    // Minimal but real: enough header for the server to treat it as a PDF.
    const pdf = new TextEncoder().encode(
      "%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n",
    );
    const uri = await addResource(session, {
      bytes: pdf,
      filename: "paper.pdf",
      contentType: "application/pdf",
      to: "viking://~/resources",
    });
    expect(
      await eventually(
        () => listTree(live as Live, "viking://~/resources"),
        (tree) => tree.includes(uri),
      ),
    ).toContain(uri);
  });

  it("lists a folder that a save created", async () => {
    if (needsServer()) return;
    await addResource(session, {
      bytes: md("In A Folder"),
      filename: "in-a-folder.md",
      contentType: "text/markdown",
      to: "viking://~/resources",
    });
    // What the popup's folder suggestions are built from. Polled: the import
    // answers before indexing finishes, so the directory appears a moment later.
    const folders = await eventually(
      () => listFolders(session, "viking://~/resources"),
      (found) => found.includes("in-a-folder"),
    );
    expect(folders).toContain("in-a-folder");
  });
});

describe("one resource per destination", () => {
  it("destroys the first document when two are imported to one place", async () => {
    if (needsServer()) return;
    // The reason every planned file gets a destination of its own. This is not
    // a merge and not a rename: the first document is gone.
    const to = "viking://~/resources/collision";
    await addResource(session, {
      bytes: md("Doomed"),
      filename: "doomed.md",
      contentType: "text/markdown",
      to,
    });
    // OpenViking locks a destination while it imports, and refuses a second
    // import that arrives before the first has let go.
    await settle();
    await addResource(session, {
      bytes: md("Survivor"),
      filename: "survivor.md",
      contentType: "text/markdown",
      to,
    });

    const tree = await eventually(
      () => listTree(live as Live, to),
      (found) => found.some((uri) => uri.endsWith("Survivor.md")),
    );
    expect(tree.some((uri) => uri.endsWith("Survivor.md"))).toBe(true);
    expect(tree.some((uri) => uri.endsWith("Doomed.md"))).toBe(false);
  });

  it("keeps both when each gets its own, which is what the planner does", async () => {
    if (needsServer()) return;
    const folder = "viking://~/resources/kept";
    const first = await addResource(session, {
      bytes: md("Kept One"),
      filename: "kept-one.md",
      contentType: "text/markdown",
      to: `${folder}/kept-one`,
    });
    await settle();
    const second = await addResource(session, {
      bytes: md("Kept Two"),
      filename: "kept-two.md",
      contentType: "text/markdown",
      to: `${folder}/kept-two`,
    });

    // Waits for the documents, not just the directories: a directory appears
    // as soon as the import is accepted, while the file inside it lands when
    // indexing finishes.
    const tree = await eventually(
      () => listTree(live as Live, folder),
      (found) =>
        found.some((uri) => uri.endsWith("Kept_One.md")) &&
        found.some((uri) => uri.endsWith("Kept_Two.md")),
    );
    expect(tree).toContain(first);
    expect(tree).toContain(second);
    // Each keeps its own document — which is exactly what sharing a
    // destination destroyed in the test above.
    expect(tree.some((uri) => uri.endsWith("Kept_One.md"))).toBe(true);
    expect(tree.some((uri) => uri.endsWith("Kept_Two.md"))).toBe(true);
  });

  it("plans a real article into a directory of its own", async () => {
    if (needsServer()) return;
    // End to end through the planner, so the layout it chooses is the one
    // actually exercised against the server.
    const plan = planArticleSave({
      article: {
        title: "Planned Clip",
        url: "https://x.example/planned",
        hostname: "x.example",
        markdown: "Body.",
      },
      title: "Planned Clip",
      scopeUri: "viking://~/resources",
      folder: "clips",
      tags: [],
      note: "",
      savedAt: new Date().toISOString(),
    });
    const file = plan.files[0];
    expect(file?.to).toBe("viking://~/resources/clips/planned-clip");

    const landed = await addResource(session, {
      bytes: new TextEncoder().encode(file?.text ?? ""),
      filename: file?.filename ?? "",
      contentType: "text/markdown",
      to: file?.to ?? "",
    });
    expect(landed).toContain("/clips/planned-clip");
  });
});

describe("the layout that killed storing images beside an article", () => {
  it("does not put two files imported to one destination side by side", async () => {
    if (needsServer()) return;
    // The extension used to upload `<slug>.md` and its pictures to the same
    // `to`, expecting `![](image-0.png)` to resolve. It cannot: each import
    // gets its own directory, so the picture is never a sibling of the
    // markdown that references it. Hence absolute image URLs instead.
    const article = await addResource(session, {
      bytes: md("Sibling Test", "Body ![a](image-0.png)"),
      filename: "sibling-test.md",
      contentType: "text/markdown",
      to: "viking://~/resources",
    });
    const image = await addResource(session, {
      bytes: new Uint8Array([
        0x89,
        0x50,
        0x4e,
        0x47,
        0x0d,
        0x0a,
        0x1a,
        0x0a,
        ...new Array(300).fill(0),
      ]),
      filename: "image-0.png",
      contentType: "image/png",
      to: "viking://~/resources",
    });

    // `root_uri` is the directory OpenViking made for each import, so these
    // being different *is* the finding: the picture has its own directory and
    // is not a sibling of the markdown that links it.
    expect(image).not.toBe(article);
    expect(article).toMatch(/\/sibling-test$/);
    expect(image).toMatch(/\/image-0$/);
  });
});
