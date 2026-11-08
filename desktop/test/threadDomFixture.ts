import assert from "node:assert/strict";
export class Element {
  tagName: string;
  children: Element[] = [];
  parent: Element | null = null;
  ownerDocument: any;
  dataset: Record<string, string> = {};
  attributes = new Map<string, string>();
  listeners = new Map<string, () => void>();
  textContent = ""; className = ""; id = ""; value = ""; type = "";
  hidden = false; disabled = false; tabIndex = 0; scrollTop = 0; scrollLeft = 0;
  constructor(tag: string, doc: any) { this.tagName = tag.toUpperCase(); this.ownerDocument = doc; }
  get childNodes() { return this.children; }
  get childElementCount() { return this.children.length; }
  get isConnected(): boolean { return this === this.ownerDocument.body || !!this.parent?.isConnected; }
  set innerHTML(_: string) { throw new Error("HTML sinks forbidden"); }
  append(...children: Element[]) { for (const child of children) {
    if (child.parent) child.parent.children = child.parent.children.filter(node => node !== child);
    child.parent = this; this.children.push(child);
  } }
  replaceChildren(...children: Element[]) { for (const child of this.children) child.parent = null; this.children = []; this.append(...children); }
  setAttribute(key: string, value: string) { this.attributes.set(key, value); }
  removeAttribute(key: string) { this.attributes.delete(key); }
  addEventListener(name: string, fn: () => void) { this.listeners.set(name, fn); }
  contains(other: Element) { return nodes(this).includes(other); }
  querySelectorAll(selector: string) {
    if (selector === "[data-thread-focus]") return nodes(this).filter(n => n.dataset.threadFocus !== undefined);
    if (selector === "[data-inbox-detail-focus]") return nodes(this).filter(n => n.dataset.inboxDetailFocus !== undefined);
    assert.equal(selector, "[data-inbox-thread-origin]"); return nodes(this).filter(n => n.dataset.inboxThreadOrigin !== undefined);
  }
  focus() { this.ownerDocument.activeElement = this; }
  click() { assert.equal(this.disabled, false); this.focus(); return this.listeners.get("click")?.(); }
}
export function nodes(root: Element): Element[] { return [root, ...root.children.flatMap(nodes)]; }
export function text(root: Element): string { return nodes(root).map(n => n.textContent).join("\n"); }
