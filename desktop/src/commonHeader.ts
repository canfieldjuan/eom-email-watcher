/** Keep native document scrolling/focus clear of the one wrapping, sticky header. */
export function observeCommonHeader(header: HTMLElement): void {
  const measure = () => header.ownerDocument.documentElement.style.setProperty(
    "--app-header-height", `${header.getBoundingClientRect().height}px`,
  );
  new ResizeObserver(measure).observe(header);
  measure();
}
