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
    ownerDocument: {
      documentElement: { style: { setProperty: (key: string, value: string) => properties.set(key, value) } },
      addEventListener() {},
    },
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

test("pane focus is corrected after native focus settles, without scrolling unrelated or departed targets", () => {
  const original = globalThis.ResizeObserver;
  globalThis.ResizeObserver = class { observe() {} } as any;
  let onFocus!: (event: { target: typeof pane }) => void;
  let keyboardFocus = false;
  let top = 182.375;
  const frames: (() => void)[] = [];
  const moves: ScrollToOptions[] = [];
  const pane = {
    hasAttribute: (name: string) => name === "data-header-focus-clearance",
    matches: (selector: string) => selector === ":focus-visible" && keyboardFocus,
    getBoundingClientRect: () => ({ top }),
  };
  const document = {
    activeElement: pane,
    documentElement: { style: { setProperty() {} } },
    addEventListener: (type: string, callback: typeof onFocus) => {
      assert.equal(type, "focusin");
      onFocus = callback;
    },
    defaultView: {
      requestAnimationFrame: (callback: () => void) => frames.push(callback),
      getComputedStyle: () => ({ scrollPaddingTop: "203.375px" }),
      scrollBy: (options: ScrollToOptions) => moves.push(options),
    },
  };
  try {
    observeCommonHeader({ ownerDocument: document, getBoundingClientRect: () => ({ height: 191.375 }) } as any);
    onFocus({ target: pane });
    keyboardFocus = true;
    frames.shift()!();
    assert.deepEqual(moves, [{ top: -21, behavior: "instant" }]);

    top = 215;
    onFocus({ target: pane });
    frames.shift()!();
    top = 182.375;
    keyboardFocus = false;
    onFocus({ target: pane });
    frames.shift()!();
    keyboardFocus = true;
    onFocus({ target: pane });
    document.activeElement = { ...pane };
    frames.shift()!();
    onFocus({ target: { ...pane, hasAttribute: () => false } });
    assert.equal(frames.length, 0);
    assert.equal(moves.length, 1);
  } finally {
    globalThis.ResizeObserver = original;
  }
});
