// M3 retained local reads. Python owns visibility/selector semantics; the host
// strictly decodes these shapes. Identity null is an explicit legacy bucket.
export interface ThreadRef {
  provider: string;
  account_id: string;
  mailbox_identity_key: string | null;
  thread_key: string;
}

interface ThreadPaging {
  limit?: number;
  cursor?: string | null;
}

export type ThreadListQuery = ThreadPaging & { vendor_id: string } & (
  | { provider?: null; account_id?: null }
  | { provider: string; account_id: string }
);

interface ThreadMessageSelector extends ThreadPaging {
  vendor_id: string;
  thread_ref: ThreadRef;
  message_id: string;
}

export interface ThreadCopyQuery extends ThreadMessageSelector {
  copy_ref?: null;
}

export interface ThreadInventoryQuery extends ThreadMessageSelector {
  copy_ref: string;
}

export type ThreadCopiesQuery = ThreadCopyQuery | ThreadInventoryQuery;

export interface ThreadMessagesQuery extends ThreadPaging {
  vendor_id: string;
  thread_ref: ThreadRef;
}

export interface ThreadBodyQuery {
  vendor_id: string;
  thread_ref: ThreadRef;
  message_id: string;
}

export interface ThreadVendor {
  vendor_id: string;
  display_name: string;
}

export interface ThreadItem {
  thread_ref: ThreadRef;
  relation: "owned" | "linked";
  owner_vendor: ThreadVendor;
  subject: string;
  latest_received_at: string | null;
  message_count: number;
}

export interface ThreadPage {
  items: ThreadItem[];
  next_cursor: string | null;
}

export interface ThreadCopy {
  copy_ref: string;
  locations: string[];
  summary: string | null;
  attachment_count: number;
}

export interface ThreadInventory {
  part_id: string;
  filename: string;
  media_type: string;
  byte_size: number | null;
}

export interface ThreadCopyPage<Item> {
  thread_ref: ThreadRef;
  message_id: string;
  items: Item[];
  next_cursor: string | null;
}

export interface ThreadUnstoredBody {
  state: "pending" | "source_gone" | "outside_folders" | "removal_pending" | "mailbox_identity_unavailable";
}

export interface ThreadStoredBodyState {
  state: "stored";
  stored_chars: number;
  source_chars: number;
}

export type ThreadBodyState = ThreadStoredBodyState | ThreadUnstoredBody;
export type ThreadBody = (ThreadStoredBodyState & { text: string }) | ThreadUnstoredBody;

export interface ThreadContext {
  thread_ref: ThreadRef;
  relation: "owned" | "linked";
  owner_vendor: ThreadVendor;
  history_notice: string;
}

export interface ThreadMessage {
  message_id: string;
  subject: string;
  direction: "inbound" | "outbound";
  sender: string;
  sender_name: string | null;
  attributed_vendor: ThreadVendor | null;
  received_at: string | null;
  body_state: ThreadBodyState;
  copy_count: number;
}

export interface ThreadMessagePage {
  thread: ThreadContext;
  items: ThreadMessage[];
  next_cursor: string | null;
}

export interface ThreadBodyResponse {
  thread_ref: ThreadRef;
  message_id: string;
  body: ThreadBody;
}

export type ThreadCopiesResult = ThreadCopyPage<ThreadCopy> | ThreadCopyPage<ThreadInventory>;
export type ThreadReadInvoke = <T>(command: string, args: Record<string, unknown>) => Promise<T>;

export class ThreadReadClient {
  private readonly invoke: ThreadReadInvoke;

  constructor(invoke: ThreadReadInvoke) {
    this.invoke = invoke;
  }

  list(query: ThreadListQuery): Promise<ThreadPage> {
    return this.invoke<ThreadPage>("threads_list", { query });
  }

  messages(query: ThreadMessagesQuery): Promise<ThreadMessagePage> {
    return this.invoke<ThreadMessagePage>("threads_messages", { query });
  }

  body(query: ThreadBodyQuery): Promise<ThreadBodyResponse> {
    return this.invoke<ThreadBodyResponse>("threads_body", { query });
  }

  copies(query: ThreadCopyQuery): Promise<ThreadCopyPage<ThreadCopy>>;
  copies(query: ThreadInventoryQuery): Promise<ThreadCopyPage<ThreadInventory>>;
  copies(query: ThreadCopiesQuery): Promise<ThreadCopiesResult>;
  copies(query: ThreadCopiesQuery): Promise<ThreadCopiesResult> {
    // Forward the entire selector, including any runtime-invalid fields, so the
    // host can reject them. Never widen a query by selecting known keys here.
    return this.invoke<ThreadCopiesResult>("threads_copies", { query });
  }
}
