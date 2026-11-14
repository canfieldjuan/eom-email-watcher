import assert from "node:assert/strict";
import test from "node:test";
import { positionInboxFilters } from "../src/inboxFilters.ts";

function place(headerBottom: number, triggerTop: number, triggerBottom: number, padding = 16) {
  const properties = new Map<string, string>();
  const viewport = { clientHeight: 520, clientWidth: 320 };
  const form = Object.assign(new EventTarget(), {
    ownerDocument: {
      documentElement: viewport,
      defaultView: Object.assign(new EventTarget(), {
        getComputedStyle: () => ({
          paddingTop: `${padding}px`, paddingBottom: `${padding}px`,
          borderTopWidth: "1px", borderBottomWidth: "1px",
        }),
      }),
    },
    style: { setProperty: (name: string, value: string) => properties.set(name, value) },
  });
  const trigger = Object.assign(new EventTarget(), {
    getBoundingClientRect: () => ({ top: triggerTop, bottom: triggerBottom, right: 304 }),
  });
  const header = { getBoundingClientRect: () => ({ bottom: headerBottom }) };
  const savedObserver = globalThis.ResizeObserver;
  globalThis.ResizeObserver = class { observe() {} } as unknown as typeof ResizeObserver;
  try {
    positionInboxFilters(
      form as unknown as HTMLFormElement, trigger as unknown as HTMLButtonElement,
      header as HTMLElement, {} as HTMLElement,
    );
    form.dispatchEvent(Object.assign(new Event("beforetoggle"), { newState: "open" }));
  } finally {
    globalThis.ResizeObserver = savedObserver;
  }
  return Object.fromEntries(properties);
}

test("Filters uses the full band below a wrapped header when neither side fits its border box", () => {
  assert.deepEqual(place(420.9375, 445.9375, 507.9375), {
    "--filter-top": "432.9375px",
    "--filter-bottom": "auto",
    "--filter-height": "75.0625px",
    "--filter-right": "16px",
  });
});

test("Filters retains the larger adjoining gap when the border box fits", () => {
  assert.deepEqual(place(300, 370, 455), {
    "--filter-top": "auto",
    "--filter-bottom": "162px",
    "--filter-height": "46px",
    "--filter-right": "16px",
  });
  assert.deepEqual(place(300, 350, 400), {
    "--filter-top": "412px",
    "--filter-bottom": "auto",
    "--filter-height": "96px",
    "--filter-right": "16px",
  });
});

test("an exact border-box fit retains the anchor and a larger CSS inset uses the band", () => {
  assert.equal(place(400, 458, 505)["--filter-height"], "34px");
  assert.equal(place(400, 458, 505, 17)["--filter-height"], "96px");
});
