// @vitest-environment node
import { afterEach, describe, expect, it } from "vitest";
import { createServer, loadConfigFromFile, type ProxyOptions, type ViteDevServer } from "vite";

/** The dev server (npm run dev) as vite.config.ts sets it up, its proxy pointed at a stand-in API that records
 * what reaches it. The API decides from a request's Host and X-Forwarded-For whether it came from this machine
 * (src/server/api/client_address.py), so the proxy must pass both on as they were, as nginx does. */
const running: ViteDevServer[] = [];

afterEach(async () => {
  await Promise.all(running.splice(0).map((server) => server.close()));
});

async function start(server: Parameters<typeof createServer>[0]): Promise<{ origin: string; listening: string }> {
  const vite = await createServer({ configFile: false, logLevel: "silent", ...server });
  running.push(vite);
  await vite.listen();
  const address = vite.httpServer?.address();
  if (!address || typeof address === "string") throw new Error("not listening");
  return { origin: `http://127.0.0.1:${address.port}`, listening: address.address };
}

async function devServerFor(apiOrigin: string) {
  const loaded = await loadConfigFromFile({ command: "serve", mode: "development" }, "vite.config.ts");
  const server = loaded!.config.server!;
  const proxy = server.proxy!["/v1"] as ProxyOptions;
  return start({ server: { ...server, strictPort: false, proxy: { "/v1": { ...proxy, target: apiOrigin } } } });
}

describe("the dev server", () => {
  it("listens on this machine only, and passes on the Host asked for and who asked", async () => {
    const seen: Record<string, string | undefined>[] = [];
    const api = await start({
      server: { host: "127.0.0.1", strictPort: false },
      plugins: [{
        name: "stand-in-api",
        configureServer(vite) {
          vite.middlewares.use((req, res) => {
            const { headers } = req as unknown as { headers: Record<string, string | undefined> }; // no Node types here
            seen.push({ host: headers.host, forwardedFor: headers["x-forwarded-for"] });
            res.end("{}");
          });
        },
      }],
    });
    const dev = await devServerFor(api.origin);
    expect(dev.listening).toBe("127.0.0.1");

    const sent = await fetch(`${dev.origin}/v1/setup`);
    expect(sent.status).toBe(200);
    expect(seen).toEqual([{ host: new URL(dev.origin).host, forwardedFor: "127.0.0.1" }]);
  });
});
