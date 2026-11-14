/** Fit the modeless form into the visible space beside its trigger. */
export function positionInboxFilters(
  form: HTMLFormElement, trigger: HTMLButtonElement, header: HTMLElement, inbox: HTMLElement,
): void {
  const win = form.ownerDocument.defaultView!;
  const viewport = form.ownerDocument.documentElement;
  const gap = 12;
  const measure = () => {
    const anchor = trigger.getBoundingClientRect();
    const top = Math.max(0, header.getBoundingClientRect().bottom) + gap;
    const bottom = viewport.clientHeight - gap;
    const aboveBottom = Math.min(bottom, anchor.top - gap);
    const above = Math.max(0, aboveBottom - top);
    const belowTop = Math.max(top, Math.min(bottom, anchor.bottom + gap));
    const below = bottom - belowTop;
    const style = win.getComputedStyle(form);
    const minimum = [style.paddingTop, style.paddingBottom, style.borderTopWidth, style.borderBottomWidth]
      .reduce((height, inset) => height + Number.parseFloat(inset), 0);
    // A border box cannot shrink below its padding and borders. Use the full
    // band when neither adjoining gap can contain it.
    const useFullBand = Math.max(above, below) < minimum;
    const openBelow = useFullBand || below >= above;
    const panelTop = useFullBand ? top : belowTop;
    form.style.setProperty("--filter-top", openBelow ? panelTop + "px" : "auto");
    form.style.setProperty("--filter-bottom", openBelow ? "auto" : viewport.clientHeight - aboveBottom + "px");
    form.style.setProperty("--filter-height", Math.max(0, openBelow ? bottom - panelTop : above) + "px");
    form.style.setProperty("--filter-right", Math.max(gap, viewport.clientWidth - anchor.right) + "px");
  };

  const update = () => {
    if (form.matches(":popover-open")) measure();
  };
  trigger.addEventListener("click", () => {
    if (!form.matches(":popover-open")) trigger.focus({ preventScroll: true });
  });
  form.addEventListener("beforetoggle", event => {
    if ((event as ToggleEvent).newState === "open") measure();
  });
  const observer = new ResizeObserver(update);
  observer.observe(header);
  observer.observe(inbox);
  win.addEventListener("resize", update);
  win.addEventListener("scroll", update, { passive: true });
}
