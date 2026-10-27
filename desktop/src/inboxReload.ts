export type InboxRefreshIntent =
  | { kind: "first" }
  | { kind: "span"; target: number };

type Request<Scope> = {
  epoch: number;
  intent: InboxRefreshIntent;
  scope: Scope | null;
};

function combine(a: InboxRefreshIntent, b: InboxRefreshIntent): InboxRefreshIntent {
  if (a.kind === "first") return b;
  if (b.kind === "first") return a;
  return { kind: "span", target: Math.max(a.target, b.target) };
}

/** Owns refresh intent, not page commits or user append cursors. */
export class InboxReload<Scope> {
  private pending: Request<Scope> | null = null;
  private active: Request<Scope> | null = null;
  private readonly options: {
    epoch: () => number;
    blocked: () => boolean;
    currentScope: (scope: Scope | null) => boolean;
    run: (intent: InboxRefreshIntent, scope: Scope | null, current: () => boolean) => Promise<boolean>;
  };

  constructor(options: InboxReload<Scope>["options"]) {
    this.options = options;
  }

  private current(request: Request<Scope>): boolean {
    return request.epoch === this.options.epoch() && this.options.currentScope(request.scope);
  }

  private synchronize(): void {
    if (this.pending && !this.current(this.pending)) this.pending = null;
    if (this.active && !this.current(this.active)) this.active = null;
  }

  private enqueue(request: Request<Scope>): void {
    if (this.pending) {
      request.intent = combine(this.pending.intent, request.intent);
      // An independent scope-clear obligation survives a superseded effect.
      // Never create an unscoped obligation from a scoped event alone.
      if (this.pending.scope === null) request.scope = null;
    }
    this.pending = request;
  }

  get running(): boolean {
    this.synchronize();
    return this.active !== null;
  }

  cancel(): void {
    this.pending = null;
    this.active = null;
  }

  request(intent: InboxRefreshIntent, scope: Scope | null = null): Promise<boolean> {
    this.synchronize();
    if (!this.options.currentScope(scope)) return Promise.resolve(false);
    const epoch = this.options.epoch();
    // A newer invalidation must also restore the span of an interrupted refresh.
    if (this.active) intent = combine(this.active.intent, intent);
    this.enqueue({ epoch, intent, scope });
    if (this.options.blocked() || this.active) return Promise.resolve(false);
    return this.start().then(committed =>
      committed && epoch === this.options.epoch() && this.options.currentScope(scope));
  }

  wake(): void {
    this.synchronize();
    if (this.options.blocked()) {
      if (this.active) {
        this.enqueue({ ...this.active });
        // The blocker invalidated the page generation. Its transport may finish,
        // but cannot hold up the settlement refresh or continue a span.
        this.active = null;
      }
      return;
    }
    if (this.pending && !this.active) void this.start();
  }

  private async start(): Promise<boolean> {
    const request = this.pending!;
    this.pending = null;
    this.active = request;
    const current = () => this.active === request && this.current(request);
    try {
      return await this.options.run(request.intent, request.scope, current);
    } finally {
      if (this.active === request) {
        this.active = null;
        // A failed attempt is not retried unless another request was recorded.
        this.wake();
      }
    }
  }
}
