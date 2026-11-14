// From desktop/: pnpm exec playwright-core install --only-shell chromium
// Then: node test-browser/common-header.mjs
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright-core";
import { createServer } from "vite";

const repo = fileURLToPath(new URL("../../", import.meta.url));
const output = await mkdtemp(join(tmpdir(), "ew-common-header-"));
const cases = [
  { width: 1524, height: 785, scale: 1 },
  { width: 1024, height: 768, scale: 1 },
  { width: 640, height: 520, scale: 1, focus: ["inbox-results", "inbox-senders-toggle", false, -24] },
  { width: 640, height: 520, scale: 2, focus: ["inbox-results", "inbox-senders-toggle"] },
  { width: 640, height: 520, scale: 2, focus: ["inbox-results", "inbox-clear", true] },
  { width: 640, height: 520, scale: 2, focus: ["inbox-sender-results", "inbox-sender-all"] },
  { width: 320, height: 520, scale: 2, focus: ["inbox-results", "inbox-senders-toggle"] },
  { width: 1024, height: 300, scale: 1, focus: ["inbox-reading-pane", "inbox-clear", true] },
];
const namedFont = "DejaVu Sans";
const fonts = [null, namedFont];

let server, browser;
try {
  const installedFont = execFileSync("fc-match", ["-f", "%{family}", namedFont], { encoding: "utf8" });
  assert.ok(installedFont.split(",").includes(namedFont), `This proof requires ${namedFont}, found ${installedFont}`);
  execFileSync(process.execPath, [join(repo, "scripts/prove_common_header.mjs"), output], {
    env: { ...process.env, HEADER_REQUIRE_MOUNTS: "1" },
    stdio: "inherit",
  });
  server = await createServer({
    root: output,
    configFile: false,
    server: { host: "127.0.0.1", port: 0, hmr: false, fs: { allow: [repo, output] } },
  });
  await server.listen();
  const origin = `http://127.0.0.1:${server.httpServer.address().port}`;
  browser = await chromium.launch();
  console.log(`Common-header and Filters browser proof on Chromium ${browser.version()}`);
  for (const { width, height, scale, focus, font } of fonts.flatMap(font => cases.map(test => ({ ...test, font })))) {
    const page = await browser.newPage({ viewport: { width, height } });
    page.setDefaultTimeout(10_000);
    const errors = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route("**/*", route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
    const name = `${font ?? "default font stack"} | ${width}×${height} at ${scale * 100}% text${focus ? `, ${focus[0]}` : ""}`;
    try {
      await page.goto(`${origin}/candidate.html`);
      await page.waitForFunction(() => window.fixtureReady);
      await page.evaluate(({ scale, font }) => {
        document.documentElement.style.fontSize = `${scale * 100}%`;
        if (font) document.documentElement.style.fontFamily = JSON.stringify(font);
      }, { scale, font });
      const viewport = await page.evaluate(() => [innerWidth, innerHeight]);
      assert.deepEqual(viewport, [width, height]);
      if (focus) {
        const { key } = await page.evaluate(args => window.prepareHeaderTab(...args), focus);
        await page.keyboard.press(key);
        await page.evaluate(() => window.checkHeaderTab());
      }
      await page.evaluate(async () => {
        await window.proveHeader();
        await window.proveFiltersLayout();
      });
      if (font === namedFont && width === 320 && scale === 2) {
        await page.evaluate(() => window.fixture.showView("inbox"));
        const trigger = page.locator("#inbox-filters-toggle");
        const panel = page.locator("#inbox-filter-form");
        const close = page.locator("#inbox-filters-close");
        await trigger.focus();
        await page.keyboard.press("Space");
        await panel.locator(":scope:popover-open").waitFor({ state: "visible" });
        await page.keyboard.press("Tab");
        assert.equal(await close.evaluate(button => document.activeElement === button), true);
        await page.keyboard.press("Escape");
        await panel.waitFor({ state: "hidden" });
        assert.equal(await trigger.evaluate(button => document.activeElement === button), true);
      }
      assert.deepEqual(errors, []);
      console.log(`PASS ${name}`);
    } catch (error) {
      throw new Error(`FAIL ${name}`, { cause: error });
    } finally {
      await page.close();
    }
  }
} finally {
  try {
    await browser?.close();
  } finally {
    try {
      await server?.close();
    } finally {
      await rm(output, { recursive: true, force: true });
    }
  }
}
