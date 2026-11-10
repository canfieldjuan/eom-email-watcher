import assert from "node:assert/strict";
import test from "node:test";
import { observeCommonHeader } from "../src/commonHeader.ts";

test("document focus clearance tracks the actual wrapping header height", () => {
  const original = globalThis.ResizeObserver;
  let resized!: () => void;
  let observed: unknown;
  globalThis.ResizeObserver = class {
    constructor(callback: () => void) { resized = callback; }
    observe(element: unknown) { observed = element; }
  } as any;
  let height = 61.5;
  const properties = new Map<string, string>();
  const header = {
    ownerDocument: { documentElement: { style: { setProperty: (key: string, value: string) => properties.set(key, value) } } },
    getBoundingClientRect: () => ({ height }),
  };
  try {
    observeCommonHeader(header as any);
    assert.equal(observed, header);
    assert.equal(properties.get("--app-header-height"), "61.5px");
    height = 189;
    resized();
    assert.equal(properties.get("--app-header-height"), "189px", "wrapped or enlarged header changes native scroll clearance");
    height = 61.5;
    resized();
    assert.equal(properties.get("--app-header-height"), "61.5px", "clearance also shrinks when navigation unwraps");
    assert.equal(properties.size, 1, "no view offsets or content widths are changed");
  } finally {
    globalThis.ResizeObserver = original;
  }
});
