import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { inboxAdmissionLabel } from "../src/inboxAdmission.ts";

test("all immutable admission kinds display their actual reason", () => {
  const kinds = ["exact_sender", "vendor_address", "vendor_domain", "sent_to_vendor", "thread_follow", "gmail_user_label"] as const;
  const labels = ["watched sender", "vendor address", "vendor domain", "vendor recipient", "followed thread", "Gmail label"];
  kinds.forEach((kind, i) => assert.equal(inboxAdmissionLabel(kind, "Vendor", "a@b.com"), `Admitted by ${labels[i]}: Vendor`));
  assert.equal(inboxAdmissionLabel("gmail_user_label", null, "a@b.com"), "Admitted by Gmail label: selected label");
  assert.equal(inboxAdmissionLabel("exact_sender", null, "a@b.com"), "Admitted by watched sender: a@b.com");
});


test("production inbox admission segment renders every kind safely as text", () => {
  const source = readFileSync(new URL("../src/main.ts", import.meta.url), "utf8");
  const start = source.indexOf("if (item.admission !== null) {");
  const segment = source.slice(start, source.indexOf("const received =", start));
  assert.ok(start >= 0);
  for (const kind of ["exact_sender", "vendor_address", "vendor_domain", "sent_to_vendor", "thread_follow", "gmail_user_label"] as const) {
    const rendered: unknown[] = [];
    const display = "<script>untrusted vendor name</script>";
    runInNewContext(segment, {
      item: { admission: { kind, display_name: display }, sender: "a@b.com" },
      inboxAdmissionLabel,
      document: { createElement: () => ({ textContent: "" }) },
      senderIdentity: { append: (node: { textContent: string }) => rendered.push(node.textContent) },
    });
    assert.deepEqual(rendered, [inboxAdmissionLabel(kind, display, "a@b.com")]);
  }
  assert.doesNotMatch(segment, /innerHTML/);
});
