import type { ConnectCapability, ConnectProviderIdentity } from "./connectTypes";

export interface RuleSummary {
  rule_id: string;
  version: number;
  enabled: boolean;
  system: boolean;
  valid: boolean;
  name: string | null;
  invalid_reason: string | null;
}
export interface RuleDetail { summary: RuleSummary; definition: unknown }
export interface RuleResult { rule: RuleDetail }
export interface RuleList { revision: number; rules: RuleSummary[] }
export interface CoiFields {
  name: string;
  mailbox: { provider: string; account_id: string };
  sender: string;
  subject: string;
  provider: ConnectProviderIdentity;
  confirmEach: boolean;
}

export function providerKey(provider: ConnectProviderIdentity): string {
  return JSON.stringify([provider.app_id, provider.version, provider.instance_id]);
}

export function isCoiProvider(item: ConnectCapability): boolean {
  return item.protocol_version === 2
    && item.capability.id === "certificate.extract" && item.capability.version === "1.0"
    && !item.capability.effects.external
    && item.capability.accepts.some((a) => a.media_type === "application/pdf"
      && Number.isSafeInteger(a.max_bytes) && a.max_bytes > 0)
    && item.capability.produces.includes("application/vnd.local-connect.certificate+json")
    && !item.capability.parameters.some((parameter) => parameter.required);
}

// This only expresses the form. Python prepares text and validates the rule schema.
export function coiDefinition(fields: CoiFields): Record<string, unknown> {
  return {
    name: fields.name,
    scope: fields.mailbox,
    trigger: { source_kind: "mail.message" },
    conditions: [
      { field: "sender", op: "equals", value: fields.sender },
      ...(fields.subject ? [{ field: "subject", op: "contains", value: fields.subject }] : []),
      { field: "attachment.media_type", op: "equals", value: "application/pdf" },
    ],
    action: {
      kind: "connect.invoke",
      capability: { id: "certificate.extract", version: "1.0" },
      provider: fields.provider,
      parameters: {},
    },
    confirm_each: fields.confirmEach,
  };
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function keys(value: unknown, names: string[]): value is Record<string, unknown> {
  return object(value) && Object.keys(value).length === names.length
    && names.every((name) => Object.hasOwn(value, name));
}

// Admission here is about lossless editability, not reimplementing the rule validator.
export function editableCoiFields(rule: RuleDetail): CoiFields | null {
  const d = rule.definition;
  if (!rule.summary.valid || rule.summary.system || !Number.isSafeInteger(rule.summary.version)
    || rule.summary.version < 1
    || !keys(d, ["name", "scope", "trigger", "conditions", "action", "confirm_each"])
    || typeof d.name !== "string" || typeof d.confirm_each !== "boolean"
    || !keys(d.scope, ["provider", "account_id"]) || typeof d.scope.provider !== "string"
    || !d.scope.provider || typeof d.scope.account_id !== "string" || !d.scope.account_id
    || !keys(d.trigger, ["source_kind"]) || d.trigger.source_kind !== "mail.message"
    || !keys(d.action, ["kind", "capability", "provider", "parameters"])
    || d.action.kind !== "connect.invoke"
    || !keys(d.action.capability, ["id", "version"])
    || d.action.capability.id !== "certificate.extract" || d.action.capability.version !== "1.0"
    || !keys(d.action.provider, ["app_id", "version", "instance_id"])
    || !Object.values(d.action.provider).every((v) => typeof v === "string" && v.length > 0)
    || !keys(d.action.parameters, []) || !Array.isArray(d.conditions)) return null;
  let sender: string | undefined;
  let subject: string | undefined;
  let pdf = false;
  for (const c of d.conditions) {
    if (!keys(c, ["field", "op", "value"]) || typeof c.value !== "string") return null;
    if (c.field === "sender" && c.op === "equals" && sender === undefined && c.value) sender = c.value;
    else if (c.field === "subject" && c.op === "contains" && subject === undefined && c.value) subject = c.value;
    else if (c.field === "attachment.media_type" && c.op === "equals" && c.value === "application/pdf" && !pdf) pdf = true;
    else return null;
  }
  if (!sender || !pdf) return null;
  return {
    name: d.name, mailbox: { provider: d.scope.provider, account_id: d.scope.account_id },
    sender, subject: subject ?? "", provider: d.action.provider as unknown as ConnectProviderIdentity,
    confirmEach: d.confirm_each,
  };
}

export class CoiRequestGate {
  busy = false;
  needsRefresh = false;

  async run<T>(action: () => Promise<T>, mutation = false): Promise<T> {
    if (this.busy || (mutation && this.needsRefresh)) throw new Error("Refresh saved rules before continuing.");
    this.busy = true;
    if (mutation) this.needsRefresh = true;
    try { return await action(); }
    finally { this.busy = false; }
  }

  refreshed(): void { this.needsRefresh = false; }
}
