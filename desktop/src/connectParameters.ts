import type { ConnectCapabilityDeclaration } from "./connectTypes.ts";

export function collectCapabilityParameters(
  capability: ConnectCapabilityDeclaration,
  initial: Record<string, string | number | boolean> = {},
  prompt: (message: string, value: string) => string | null = (message, value) => window.prompt(message, value),
): Record<string, string | number | boolean> | null {
  const values: Record<string, string | number | boolean> = {};
  for (const parameter of capability.parameters) {
    if (Object.hasOwn(parameter, "fixed_value")) {
      const fixed = Object.hasOwn(initial, parameter.name)
        ? initial[parameter.name]
        : parameter.fixed_value;
      if (fixed === undefined) throw new Error(`${parameter.label} has an invalid fixed value.`);
      values[parameter.name] = fixed;
      continue;
    }
    const omission = parameter.required
      ? "Cancel stops this action."
      : "Cancel leaves this optional value unset.";
    const raw = prompt(
      `${parameter.label}\n${parameter.description}\nExpected ${parameter.value_type}. ${omission}`,
      parameter.name in initial ? String(initial[parameter.name]) : "",
    );
    if (raw === null) {
      if (parameter.required) return null;
      continue;
    }
    if (parameter.value_type === "string") {
      values[parameter.name] = raw;
      continue;
    }
    if (parameter.value_type === "integer") {
      if (!/^-?(0|[1-9]\d*)$/.test(raw)) {
        throw new Error(`${parameter.label} must be a whole number.`);
      }
      const value = Number(raw);
      if (!Number.isSafeInteger(value)) {
        throw new Error(`${parameter.label} is outside the supported number range.`);
      }
      values[parameter.name] = value;
      continue;
    }
    const normalized = raw.trim().toLowerCase();
    if (normalized !== "true" && normalized !== "false") {
      throw new Error(`${parameter.label} must be true or false.`);
    }
    values[parameter.name] = normalized === "true";
  }
  return values;
}
