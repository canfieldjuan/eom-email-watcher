import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const config = JSON.parse(readFileSync(new URL("../src-tauri/tauri.conf.json", import.meta.url), "utf8"));

function directives(policy: unknown): Map<string, string[]> {
  assert.equal(typeof policy, "string", "a real CSP must protect the app");
  const entries = (policy as string).split(";").map((part) => part.trim().split(/\s+/)).filter(([name]) => name);
  assert.equal(new Set(entries.map(([name]) => name)).size, entries.length, "no duplicate directives");
  return new Map(entries.map(([name, ...sources]) => [name, sources]));
}

test("production CSP refuses executable markup and remote content while preserving local IPC", () => {
  const policy = directives(config.app.security.csp);
  for (const name of ["default-src", "object-src", "frame-src", "base-uri", "form-action"]) {
    assert.deepEqual(policy.get(name), ["'none'"], name);
  }
  for (const name of ["script-src", "style-src", "img-src", "font-src"]) {
    assert.deepEqual(policy.get(name), ["'self'"], `${name} may load only packaged assets`);
  }
  assert.deepEqual(new Set(policy.get("connect-src")), new Set(["ipc:", "http://ipc.localhost"]));
  assert.ok(!config.app.security.dangerousDisableAssetCspModification, "Tauri must retain its trusted asset processing");
  assert.ok(!config.app.security.assetProtocol?.enable, "mail cannot use an unrestricted local-file protocol");
});

test("development CSP adds only the configured Vite transport and never relaxes production", () => {
  const production = directives(config.app.security.csp);
  const development = directives(config.app.security.devCsp);
  assert.equal(config.build.devUrl, "http://localhost:1420");
  assert.deepEqual([...development.keys()].sort(), [...production.keys()].sort());
  for (const [name, sources] of production) {
    const allowed = name === "connect-src" ? [...sources, config.build.devUrl, "ws://localhost:1420"] : sources;
    assert.deepEqual(new Set(development.get(name)), new Set(allowed), name);
  }
});

test("the application stylesheet loads as a local asset during development too", () => {
  const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
  const main = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
  assert.match(html, /<link\s+rel="stylesheet"\s+href="\/src\/styles\.css"\s*\/?>/);
  assert.doesNotMatch(main, /import\s+["']\.\/styles\.css["']/,
    "Vite's JS CSS import injects inline styles, which the CSP refuses");
});
