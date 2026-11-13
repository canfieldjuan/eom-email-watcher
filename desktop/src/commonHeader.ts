/** Keep native document scrolling/focus clear of the one wrapping, sticky header. */
export function observeCommonHeader(header: HTMLElement): void {
  const document = header.ownerDocument;
  const root = document.documentElement;
  const measure = () => root.style.setProperty(
    "--app-header-height", `${header.getBoundingClientRect().height}px`,
  );
  new ResizeObserver(measure).observe(header);
  measure();

  document.addEventListener("focusin", event => {
    const pane = event.target as HTMLElement;
    if (!pane.hasAttribute("data-header-focus-clearance")) return;
    const window = document.defaultView!;
    // Native Tab can leave the top of a partly visible scroll region under the header.
    window.requestAnimationFrame(() => {
      if (document.activeElement !== pane || !pane.matches(":focus-visible")) return;
      const clearance = Number.parseFloat(window.getComputedStyle(root).scrollPaddingTop);
      const overlap = clearance - pane.getBoundingClientRect().top;
      if (overlap > 0) window.scrollBy({ top: -Math.ceil(overlap), behavior: "instant" });
    });
  });
}
