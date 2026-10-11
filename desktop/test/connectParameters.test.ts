import assert from "node:assert/strict";
import test from "node:test";
import { collectCapabilityParameters } from "../src/connectParameters.ts";
import type { ConnectCapabilityDeclaration } from "../src/connectTypes.ts";

const declaration = (parameters: ConnectCapabilityDeclaration["parameters"]): ConnectCapabilityDeclaration => ({
  id: "document.translate", version: "1.0", action: { label: "Translate", description: "" },
  accepts: [], produces: [], parameters, effects: { external: false, confirmation_required: false },
});

for (const [value, value_type] of [["", "string"], [0, "integer"], [false, "boolean"]] as const) {
  test(`fixed ${value_type} preserves falsy value without a prompt`, () => {
    const cap = declaration([{ name: "pin", value_type, required: true, label: "Pin", description: "", fixed_value: value }]);
    assert.deepEqual(collectCapabilityParameters(cap, {}, () => assert.fail("fixed values never prompt")), { pin: value });
  });
}
test("resume keeps admitted fixed intent rather than a rotated declaration", () => {
  const cap = declaration([{ name: "pin", value_type: "string", required: true, label: "Pin", description: "", fixed_value: "new" }]);
  assert.deepEqual(collectCapabilityParameters(cap, { pin: "old" }, () => assert.fail("no prompt")), { pin: "old" });
});
test("editable fields still prompt and required cancellation stops the action", () => {
  const cap = declaration([{ name: "target", value_type: "string", required: true, label: "Target", description: "" }]);
  assert.deepEqual(collectCapabilityParameters(cap, {}, () => "en"), { target: "en" });
  assert.equal(collectCapabilityParameters(cap, {}, () => null), null);
});
test("optional cancellation omits the field and integer parsing retains its boundary", () => {
  const cap = declaration([{ name: "count", value_type: "integer", required: false, label: "Count", description: "" }]);
  assert.deepEqual(collectCapabilityParameters(cap, {}, () => null), {});
  assert.deepEqual(collectCapabilityParameters(cap, {}, () => "0"), { count: 0 });
  assert.throws(() => collectCapabilityParameters(cap, {}, () => "9007199254740992"));
});
