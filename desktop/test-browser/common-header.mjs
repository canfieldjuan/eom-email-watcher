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
  { width: 640, height: 520, scale: 2, focus: ["inbox-sender-results", "inbox-sender-results", false, 8, "#fixture-sender-control"] },
  { width: 640, height: 520, scale: 2, focus: ["inbox-results", "inbox-results", false, 8, "#inbox-load-more"] },
  { width: 1024, height: 300, scale: 1, focus: ["inbox-reading-pane", "inbox-reading-pane", false, 8, "#fixture-reader-control"] },
];
const namedFont = "DejaVu Sans";
const fonts = [null, namedFont];

async function fixturePage(browser, origin, viewport) {
  const page = await browser.newPage({ viewport });
  page.setDefaultTimeout(10_000);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/*", route => new URL(route.request().url()).origin === origin ? route.continue() : route.abort());
  return { page, errors };
}

async function proveHeaderPaint(page) {
  const { x, y, hitHeader, overlapsContent } = await page.evaluate(async () => {
    window.fixture.showView("inbox");
    const app = document.querySelector("#app");
    const spacer = document.createElement("div");
    spacer.style.height = "1400px";
    app.append(spacer);
    scrollTo(0, 400);
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const header = document.querySelector(".app-header");
    const bounds = header.getBoundingClientRect();
    const x = Math.floor(bounds.right) - 20;
    const y = Math.floor(bounds.top + bounds.height / 2);
    // Later positioned content overlaps an unoccupied patch of the header.
    const marker = document.createElement("div");
    marker.id = "fixture-header-backdrop";
    Object.assign(marker.style, {
      position: "absolute", left: `${x - 8}px`, top: `${scrollY + y - 8}px`,
      width: "16px", height: "16px", background: "#f0f",
    });
    app.append(marker);
    return {
      x, y, hitHeader: header.contains(document.elementFromPoint(x, y)),
      overlapsContent: document.elementsFromPoint(x, y).includes(marker),
    };
  });
  assert.ok(overlapsContent, "The fixture content did not overlap the header");
  assert.ok(hitHeader, "Scrolled content received a hit above the sticky header");
  const pixel = async () => {
    const png = (await page.screenshot({ scale: "css" })).toString("base64");
    return page.evaluate(async ({ png, x, y }) => {
      const bitmap = await createImageBitmap(new Blob([Uint8Array.from(atob(png), char => char.charCodeAt(0))]));
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 1;
      const context = canvas.getContext("2d");
      context.drawImage(bitmap, x, y, 1, 1, 0, 0, 1, 1);
      bitmap.close();
      return Array.from(context.getImageData(0, 0, 1, 1).data);
    }, { png, x, y });
  };
  const first = await pixel();
  await page.locator("#fixture-header-backdrop").evaluate(marker => { marker.style.background = "#0f0"; });
  assert.deepEqual(await pixel(), first, "Changing content under the sticky header changed its painted pixel");
  console.log("PASS header stacking and opaque paint over scrolled content");
}

async function proveClassicScrollbar(browser, origin) {
  const { page, errors } = await fixturePage(browser, origin, { width: 1024, height: 1000 });
  try {
    await page.goto(`${origin}/candidate.html`);
    await page.waitForFunction(() => window.fixtureReady);
    const { before, during, after } = await page.evaluate(async () => {
      window.fixture.showView("inbox");
      document.querySelector("#inbox-list").replaceChildren();
      const root = document.documentElement;
      const sample = async () => {
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        const bounds = selector => {
          const { x, width } = document.querySelector(selector).getBoundingClientRect();
          return [x, width];
        };
        return {
          overflow: root.scrollHeight > root.clientHeight,
          scrollbar: innerWidth - root.clientWidth,
          header: bounds(".app-header"), nav: bounds(".view-tabs"),
        };
      };
      const before = await sample();
      const spacer = document.createElement("div");
      spacer.style.height = "1400px";
      document.querySelector("#app").append(spacer);
      const during = await sample();
      spacer.remove();
      return { before, during, after: await sample() };
    });
    assert.ok(!before.overflow && during.overflow && !after.overflow && during.scrollbar > 0,
      `The fixture did not add and remove a classic scrollbar: ${JSON.stringify({ before, during, after })}`);
    for (const result of [during, after]) {
      assert.deepEqual(result.header, before.header, "A classic scrollbar shifted the common header");
      assert.deepEqual(result.nav, before.nav, "A classic scrollbar shifted common navigation");
    }
    assert.deepEqual(errors, []);
    console.log(`PASS header and navigation gutters with a ${during.scrollbar}px classic scrollbar`);
  } finally {
    await page.close();
  }
}

async function proveOpenFiltersPlacement(page) {
  await page.evaluate(() => {
    window.fixture.showView("inbox");
    // Keep the observed Inbox and header heights stable while the viewport resizes.
    document.querySelector("#inbox-list").replaceChildren();
    const spacer = document.createElement("div");
    spacer.style.height = "800px";
    document.querySelector("#app").append(spacer);
    scrollTo(0, 0);
    document.querySelector("#inbox-sender").value = "unapplied draft";
  });
  const form = page.locator("#inbox-filter-form");
  const snapshot = () => form.evaluate(async panel => {
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const { top, bottom, left, right } = panel.getBoundingClientRect();
    return {
      top, bottom, left, right, width: document.documentElement.clientWidth, height: innerHeight,
      header: document.querySelector(".app-header").getBoundingClientRect().bottom,
      inboxHeight: document.querySelector("#inbox-view").getBoundingClientRect().height,
      y: scrollY, draft: document.querySelector("#inbox-sender").value,
      open: panel.matches(":popover-open"), calls: window.fixture.calls.length,
    };
  });
  await page.locator("#inbox-filters-toggle").focus();
  await page.keyboard.press("Space");
  const opened = await snapshot();
  await page.mouse.move(8, 8);
  await page.mouse.wheel(0, 160);
  await page.waitForFunction(y => scrollY > y, opened.y);
  const scrolled = await snapshot();
  assert.notDeepEqual([scrolled.top, scrolled.bottom], [opened.top, opened.bottom], "Filters placement did not update on window scroll");
  await page.setViewportSize({ width: 640, height: 380 });
  const resized = await snapshot();
  assert.notEqual(resized.height, scrolled.height, "The browser viewport did not resize");
  assert.equal(resized.inboxHeight, scrolled.inboxHeight, "The Inbox observer could mask the resize listener");
  assert.equal(resized.header, scrolled.header, "The header observer could mask the resize listener");
  assert.notDeepEqual([resized.top, resized.bottom], [scrolled.top, scrolled.bottom], "Filters placement did not update on window resize");
  for (const [phase, result] of Object.entries({ opened, scrolled, resized })) {
    assert.ok(result.open, `Filters closed during ${phase}`);
    assert.ok(result.top >= result.header && result.bottom <= result.height
      && result.left >= 0 && result.right <= result.width && result.bottom > result.top,
    `Filters escaped the viewport or covered the header during ${phase}: ${JSON.stringify(result)}`);
    assert.equal(result.draft, "unapplied draft");
    assert.equal(result.calls, opened.calls);
  }
  await page.keyboard.press("Escape");
  await form.waitFor({ state: "hidden" });
  console.log(`Open Filters stayed in bounds after window scroll and resize: ${JSON.stringify({
    font: await page.evaluate(() => getComputedStyle(document.documentElement).fontFamily),
    scroll: [opened.y, scrolled.y], height: [scrolled.height, resized.height],
  })}`);
}

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
    const { page, errors } = await fixturePage(browser, origin, { width, height });
    const name = `${font ?? "default font stack"} | ${width}×${height} at ${scale * 100}% text${focus ? `, ${focus[4] ?? focus[0]}` : ""}`;
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
      if (width === 640 && scale === 1) await proveOpenFiltersPlacement(page);
      if (!font && width === 1024 && height === 768) await proveHeaderPaint(page);
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
  await browser.close();
  browser = await chromium.launch({ ignoreDefaultArgs: ["--hide-scrollbars"] });
  await proveClassicScrollbar(browser, origin);
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
