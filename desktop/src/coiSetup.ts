import type { ConnectCapabilities, ConnectCapability } from "./connectTypes";
import {
  CoiRequestGate, coiControlState, coiDefinition, editableCoiFields, isCoiProvider, providerKey,
  type CoiFields, type RuleDetail, type RuleList, type RuleResult,
} from "./coiRules";

type Invoke = <T>(command: string, args?: Record<string, unknown>) => Promise<T>;
interface Account { provider: string; account_id: string; address: string | null; display_name: string; connected: boolean; active: boolean }
interface Sender { email: string; name: string | null; admission_active: boolean }
const mailboxKey = (account: { provider: string; account_id: string }) => JSON.stringify([account.provider, account.account_id]);

export function mountCoiSetup(root: HTMLElement, invoke: Invoke, errorMessage: (error: unknown) => string, onViewConnect?: () => void): { refresh: () => Promise<void> } {
  root.innerHTML = `
    <details class="coi-setup" open>
      <summary>COI rules</summary>
      <p>Choose which incoming certificates to extract. Check extracted values against the original PDF; dates do not verify coverage.</p>
      <div class="coi-rule-toolbar">
        <label>Saved rule<select data-field="saved"><option value="">New rule</option></select></label>
        <button type="button" data-action="refresh">Refresh rules and providers</button>
        <button type="button" data-action="new">New rule</button>
      </div>
      <p data-field="status" role="status" aria-live="polite">Open Expiry Ledger to load setup.</p>
      <p data-field="locked" hidden><span class="capability-locked">Automations locked</span> <button type="button" data-action="view-connect">View Connect</button></p>
      <form>
        <fieldset disabled>
          <div class="coi-rule-fields">
            <label>Rule name<input data-field="name" required maxlength="80" value="COI extraction" /></label>
            <label>Mailbox<select data-field="mailbox" required></select></label>
            <label>Watched sender<select data-field="sender" required></select></label>
            <label>Subject contains (optional)<input data-field="subject" maxlength="4096" /></label>
            <label>Certificate provider<select data-field="provider" required></select></label>
          </div>
          <label class="coi-confirm"><input data-field="confirm" type="checkbox" checked /> Confirm each matching attachment in the inbox</label>
          <p data-field="preview"></p>
          <button type="submit" data-action="save">Save enabled rule</button>
          <button type="button" data-action="toggle" hidden>Pause rule</button>
        </fieldset>
      </form>
      <p>Rules apply to new analyzed messages in the selected mailbox, not older mail. Pausing stops new matches; work already queued keeps its original rule. Unreadable files and uncertain values need manual review.</p>
    </details>`;
  const get = <T extends HTMLElement>(selector: string): T => {
    const item = root.querySelector<T>(selector);
    if (!item) throw new Error(`Missing COI setup element: ${selector}`);
    return item;
  };
  const saved = get<HTMLSelectElement>('[data-field="saved"]');
  const name = get<HTMLInputElement>('[data-field="name"]');
  const mailbox = get<HTMLSelectElement>('[data-field="mailbox"]');
  const sender = get<HTMLSelectElement>('[data-field="sender"]');
  const subject = get<HTMLInputElement>('[data-field="subject"]');
  const provider = get<HTMLSelectElement>('[data-field="provider"]');
  const confirm = get<HTMLInputElement>('[data-field="confirm"]');
  const preview = get<HTMLParagraphElement>('[data-field="preview"]');
  const status = get<HTMLParagraphElement>('[data-field="status"]');
  const locked = get<HTMLParagraphElement>('[data-field="locked"]');
  const fieldset = get<HTMLFieldSetElement>('fieldset');
  const save = get<HTMLButtonElement>('[data-action="save"]');
  const toggle = get<HTMLButtonElement>('[data-action="toggle"]');
  const refreshButton = get<HTMLButtonElement>('[data-action="refresh"]');
  const newButton = get<HTMLButtonElement>('[data-action="new"]');
  const gate = new CoiRequestGate();
  let accounts: Account[] = [];
  let senders: Sender[] = [];
  let capabilities: ConnectCapability[] = [];
  let current: RuleDetail | null = null;
  let loaded = false;
  let automationsActive: boolean | null = null;
  let editable = true;
  let diagnostic: string | null = null;

  function option(select: HTMLSelectElement, value: string, label: string, disabled = false): void {
    const item = document.createElement("option");
    item.value = value; item.textContent = label; item.disabled = disabled; select.append(item);
  }
  function choices(select: HTMLSelectElement, placeholder: string): void {
    select.replaceChildren(); option(select, "", placeholder);
  }
  function retain(select: HTMLSelectElement, value: string, label: string): void {
    if (!Array.from(select.options).some((item) => item.value === value)) option(select, value, label, true);
    select.value = value;
  }
  function message(text: string, failed = false): void {
    status.textContent = text; status.dataset.kind = failed ? "error" : "";
  }
  function selected(): { fields: CoiFields; capability: ConnectCapability } | null {
    const a = accounts.find((item) => mailboxKey(item) === mailbox.value && item.connected);
    const s = senders.find((item) => item.email === sender.value && item.admission_active);
    const c = capabilities.find((item) => providerKey(item.provider) === provider.value);
    if (!a || !s || !c || !name.value.trim()) return null;
    return {
      fields: { name: name.value, mailbox: { provider: a.provider, account_id: a.account_id },
        sender: s.email, subject: subject.value, provider: {
          app_id: c.provider.app_id, version: c.provider.version, instance_id: c.provider.instance_id,
        }, confirmEach: confirm.checked }, capability: c,
    };
  }
  function renderState(): void {
    fieldset.disabled = gate.busy || !loaded || !editable || gate.needsRefresh;
    saved.disabled = gate.busy || !loaded;
    newButton.disabled = gate.busy || !loaded || gate.needsRefresh;
    refreshButton.disabled = gate.busy;
    const choice = selected();
    const controls = coiControlState(automationsActive === true, current?.summary ?? null);
    save.disabled = !controls.saveEnabled || !choice || !editable || gate.needsRefresh;
    toggle.disabled = !controls.toggleEnabled;
    locked.hidden = automationsActive !== false;
    toggle.hidden = current === null || !editable;
    toggle.textContent = current?.summary.enabled ? "Pause rule" : "Resume rule";
    save.textContent = current ? "Save changes" : "Save enabled rule";
    if (!editable) preview.textContent = "This saved rule cannot be edited in this COI form. Its existing conditions remain unchanged.";
    else if (!choice) preview.textContent = "Select a connected mailbox, an active watched sender and an available certificate provider. Manage mailboxes in Settings and senders in Watchlist.";
    else {
      const account = accounts.find((item) => mailboxKey(item) === mailbox.value)!;
      const confirmation = confirm.checked || choice.capability.capability.effects.confirmation_required
        ? "Inbox confirmation is required before extraction."
        : "Matching PDFs will be extracted automatically.";
      preview.textContent = `${account.address ?? account.display_name}: PDFs from ${choice.fields.sender}${subject.value ? ` whose subject contains "${subject.value}"` : ""}, using ${choice.capability.provider.name}. ${confirmation} ${current ? (current.summary.enabled ? "This rule is enabled." : "This rule remains paused after editing.") : "Saving enables this rule."}${account.active ? "" : " Switch to this mailbox in Settings to receive its new mail."}`;
    }
  }
  function reset(): void {
    current = null; editable = true; saved.value = ""; name.value = "COI extraction";
    mailbox.value = ""; sender.value = ""; subject.value = ""; provider.value = ""; confirm.checked = true;
    renderState();
  }
  function showRule(rule: RuleDetail): void {
    current = rule;
    const fields = editableCoiFields(rule);
    editable = fields !== null;
    if (fields) {
      name.value = fields.name; subject.value = fields.subject; confirm.checked = fields.confirmEach;
      retain(mailbox, mailboxKey(fields.mailbox), `${fields.mailbox.provider}: ${fields.mailbox.account_id} (disconnected)`);
      retain(sender, fields.sender, `${fields.sender} (not currently watched)`);
      retain(provider, providerKey(fields.provider), `${fields.provider.app_id} ${fields.provider.version} (unavailable; explicitly select a replacement)`);
    }
    renderState();
  }
  async function loadSources(ruleId: string): Promise<void> {
    const [a, s, catalog, rules, entitlement] = await Promise.all([
      invoke<{ accounts: Account[] }>("mail_accounts_list"),
      invoke<Sender[]>("watchlist_list"),
      invoke<ConnectCapabilities>("connect_catalog"),
      invoke<RuleList>("automation_rules_list"),
      invoke<{ automations_active: boolean }>("connect_entitlement_status"),
    ]);
    automationsActive = entitlement.automations_active;
    accounts = a.accounts; senders = s; capabilities = catalog.items.filter(isCoiProvider); diagnostic = catalog.diagnostic?.code ?? null;
    choices(mailbox, "Select mailbox");
    for (const item of accounts) option(mailbox, mailboxKey(item), `${item.address ?? item.display_name}${item.connected ? "" : " (disconnected)"}`, !item.connected);
    choices(sender, "Select watched sender");
    for (const item of senders) option(sender, item.email, `${item.email}${item.admission_active ? "" : " (inactive)"}`, !item.admission_active);
    choices(provider, "Select certificate provider");
    for (const item of capabilities) option(provider, providerKey(item.provider), `${item.provider.name} ${item.provider.version} (${item.provider.instance_id})`);
    choices(saved, "New rule");
    for (const item of rules.rules) if (!item.system) option(saved, item.rule_id, `${item.name ?? "Invalid saved rule"} (${item.enabled ? "enabled" : "paused"})`);
    if (ruleId) {
      const result = await invoke<RuleResult>("automation_rules_get", { ruleId });
      saved.value = ruleId; showRule(result.rule);
    } else reset();
    loaded = true;
    gate.refreshed();
  }
  async function refresh(): Promise<void> {
    if (gate.busy) return;
    const ruleId = saved.value;
    const request = gate.run(() => loadSources(ruleId)); renderState();
    try {
      await request;
      message(capabilities.length ? "Rules and providers refreshed." : `No compatible certificate provider is available${diagnostic ? ` (${diagnostic})` : ""}. Open the provider and check the Connect entitlement, then refresh.`);
    } catch (error) { loaded = false; message(errorMessage(error), true); }
    renderState();
  }
  async function mutate(action: () => Promise<RuleResult>): Promise<void> {
    if (gate.busy || gate.needsRefresh) return;
    const request = gate.run(async () => {
      const result = await action();
      // Display only the engine's committed identity/version, then reload the source snapshot.
      current = result.rule;
      await loadSources(result.rule.summary.rule_id);
    }, true);
    renderState();
    try { await request; message("Saved rule reloaded from the engine."); }
    catch (error) {
      if (typeof error === "object" && error !== null && "code" in error && error.code === "automation_entitlement_required") {
        await refresh();
        message(errorMessage(error), true);
      } else message(`${errorMessage(error)} Refresh saved rules before continuing; the last change may already have been saved.`, true);
    }
    renderState();
  }
  saved.addEventListener("change", () => {
    if (!saved.value) { if (!gate.needsRefresh) { reset(); message("Configure a new COI rule."); } return; }
    void refresh();
  });
  newButton.addEventListener("click", () => { if (!gate.busy && !gate.needsRefresh) { reset(); message("Configure a new COI rule."); } });
  refreshButton.addEventListener("click", () => void refresh());
  get<HTMLButtonElement>('[data-action="view-connect"]').addEventListener("click", () => onViewConnect?.());
  get<HTMLFormElement>("form").addEventListener("input", renderState);
  get<HTMLFormElement>("form").addEventListener("submit", (event) => {
    event.preventDefault();
    const choice = selected();
    if (!choice || !editable || !loaded || automationsActive !== true) return;
    const editing = current;
    void mutate(async () => {
      // Recheck live selection before preparing; dispatch still owns final admission.
      const live = await invoke<ConnectCapabilities>("connect_catalog");
      if (!live.items.some((c) => isCoiProvider(c) && providerKey(c.provider) === providerKey(choice.fields.provider))) throw new Error("The selected provider is unavailable. Refresh and explicitly select a provider.");
      const prepared = await invoke<{ definition: unknown }>("automation_rules_prepare", { definition: coiDefinition(choice.fields) });
      return invoke<RuleResult>("automation_rules_put", {
        definition: prepared.definition,
        ...(editing ? { ruleId: editing.summary.rule_id, expectedVersion: editing.summary.version } : {}),
      });
    });
  });
  toggle.addEventListener("click", () => {
    if (!current || !editable || !loaded || (automationsActive !== true && !current.summary.enabled)) return;
    const rule = current.summary;
    void mutate(() => invoke<RuleResult>("automation_rules_set_enabled", {
      ruleId: rule.rule_id, expectedVersion: rule.version, enabled: !rule.enabled,
    }));
  });
  return { refresh };
}
