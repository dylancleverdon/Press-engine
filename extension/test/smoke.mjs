// End-to-end check of the extension against a running backend, using fake
// TikTok pages (no real TikTok traffic). Usage:
//   PRESS_URL=http://localhost:8000 PRESS_TOKEN=pe_... node extension/test/smoke.mjs
import { createRequire } from "node:module";
import path from "node:path";
import os from "node:os";
import fs from "node:fs";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");
const extDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const backend = process.env.PRESS_URL || "http://localhost:8000";
const token = process.env.PRESS_TOKEN;
if (!token) throw new Error("Set PRESS_TOKEN");

const PROFILE = `<!doctype html><html><head><title>Jazz Bot (@jazzbot) | TikTok</title></head><body>
  <h1 data-e2e="user-title">jazzbot</h1><h2 data-e2e="user-subtitle">Jazz Bot</h2>
  <strong data-e2e="followers-count">48.2K</strong>
  <h2 data-e2e="user-bio">Spiritual jazz edits. Mammal Hands forever. booking: jazzbot.mgmt@gmail.com</h2>
  <a data-e2e="user-link" href="https://www.tiktok.com/link/v2?aid=1988&lang=en&scene=bio_url&target=https%3A%2F%2Flinktr.ee%2Fjazzbot">linktr.ee/jazzbot</a>
</body></html>`;
const FEED = `<!doctype html><html><body><p>For you</p></body></html>`;

const userData = fs.mkdtempSync(path.join(os.tmpdir(), "pe-ext-"));
const ctx = await chromium.launchPersistentContext(userData, {
  channel: "chromium", // the full Chromium build: headless shell can't load extensions
  headless: true,
  args: [`--disable-extensions-except=${extDir}`, `--load-extension=${extDir}`],
});
let [worker] = ctx.serviceWorkers();
if (!worker) worker = await ctx.waitForEvent("serviceworker");
await worker.evaluate(([url, tok]) => chrome.storage.local.set({ backendUrl: url, token: tok }), [backend, token]);

await ctx.route("https://www.tiktok.com/**", (route) => {
  const url = route.request().url();
  const body = url.includes("/@jazzbot") ? PROFILE : FEED;
  route.fulfill({ status: 200, contentType: "text/html", body });
});

const page = await ctx.newPage();
await page.goto("https://www.tiktok.com/foryou");
await page.waitForTimeout(1500);
const hiddenOnFeed = await page.locator("#press-engine-capture .wrap").isHidden();

await page.goto("https://www.tiktok.com/@jazzbot");
const button = page.locator("#press-engine-capture #save");
await button.waitFor({ state: "visible", timeout: 5000 });
await button.click();
const toast = page.locator("#press-engine-capture #toast");
await toast.waitFor({ state: "visible", timeout: 10000 });
const message = await toast.textContent();
await page.screenshot({ path: path.join(os.tmpdir(), "pe-extension.png") });
await ctx.close();

const res = await fetch(`${backend}/api/people?q=Jazz%20Bot`, { headers: { Authorization: `Bearer ${token}` } });
const people = await res.json();
const found = people.items.find((p) => p.name === "Jazz Bot");
const detail = found ? await (await fetch(`${backend}/api/people/${found.id}`, { headers: { Authorization: `Bearer ${token}` } })).json() : null;
const result = {
  hiddenOnFeed,
  toast: message,
  saved: Boolean(found),
  identities: detail ? detail.identities.map((i) => `${i.type}:${i.value}`) : [],
  followers: detail ? detail.followers : null,
  found_by: found ? found.found_by : null,
};
console.log(JSON.stringify(result, null, 2));
const ok = hiddenOnFeed && result.saved && result.identities.includes("tiktok:jazzbot") &&
  result.identities.includes("linkinbio:linktr.ee/jazzbot") && result.identities.includes("email:jazzbot.mgmt@gmail.com");
process.exit(ok ? 0 : 1);
