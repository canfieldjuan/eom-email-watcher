import assert from "node:assert/strict";
import test from "node:test";
import { readFile } from "node:fs/promises";
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
  assert.deepEqual(collectCapabilityParameters(cap, { pin: "old" }, () => assert.fail("no prompt"), "requested"), { pin: "old" });
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

test("completed job uses current fixed declaration", () => {
  const cap = declaration([{ name: "pin", value_type: "string", required: true, label: "Pin", description: "", fixed_value: "new" }]);
  assert.deepEqual(collectCapabilityParameters(cap, { pin: "old" }, () => assert.fail("no prompt"), "completed"), { pin: "new" });
});

for (const status of [undefined, "failed", "completed", "requested", "accepted", "processing"]) {
  for (const [value, value_type] of [["", "string"], [0, "integer"], [false, "boolean"]] as const) {
    test(`prior ${status} with fixed ${value_type} selects the correct intent`, () => {
      const cap = declaration([
        { name: "pin", value_type, required: true, label: "Pin", description: "", fixed_value: value },
        { name: "editable", value_type: "string", required: true, label: "Editable", description: "" },
      ]);
      const active = ["requested", "accepted", "processing"].includes(status ?? "");
      assert.deepEqual(collectCapabilityParameters(cap, { pin: "old", editable: "saved" }, (_message, initial) => {
        assert.equal(initial, "saved"); return initial;
      }, status), { pin: active ? "old" : value, editable: "saved" });
    });
  }
}

test("production invocation caller carries the matched job status", async () => {
  const source = await readFile(new URL("../src/main.ts", import.meta.url), "utf8");
  const start = source.indexOf('invokeButton.addEventListener("click", async () => {');
  assert.ok(start >= 0);
  const body = source.slice(start, source.indexOf("          } catch (error)", start));
  const collection = body.slice(body.indexOf("            const previous ="));
  const run = new Function("selected", "attachment", "matchingCapabilityResult", "collectCapabilityParameters", `const capability = selected; let parameters; ${collection}; return parameters;`);
  const capability = { capability: declaration([{ name: "pin", value_type: "string", required: true, label: "Pin", description: "", fixed_value: "new" }]) };
  for (const status of ["completed", "failed", "requested", "accepted", "processing"]) {
    assert.deepEqual(run(capability, {}, () => ({ status, parameters: { pin: "old" } }), collectCapabilityParameters), {
      pin: ["requested", "accepted", "processing"].includes(status) ? "old" : "new",
    });
  }
});
