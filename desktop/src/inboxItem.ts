import type { CalendarWriteOutcome } from "./calendarOutcome.ts";
import type { ConnectProviderIdentity, ConnectOutputMetadata } from "./connectTypes.ts";
import type { InboxAdmissionKind } from "./inboxAdmission.ts";
import type { InboxMessageRef } from "./threadReads.ts";

export interface InboxAttachment {
  part_id: string;
  attachment_id: string | null;
  filename: string;
  media_type: string;
  byte_size: number;
  capability_results: AttachmentCapabilityResult[];
  automation_fires: AutomationFireProjection[];
}

interface AutomationFireProjection {
  fire_id: string;
  rule_id: string;
  rule_version: number;
  state: string;
  state_version: number;
  reason: string | null;
  job_id: string | null;
  prepared_identity_sha256: string | null;
  updated_at: string;
}

interface ConnectWarning {
  code: string;
  message: string;
}

export interface ConnectSummary {
  summary_version: string;
  text: string;
  warnings: ConnectWarning[];
}

export interface AttachmentCapabilityResult {
  required_manifest_features?: string[];
  job_id?: string;
  capability_id: string;
  capability_version: string;
  protocol_version?: number;
  provider?: ConnectProviderIdentity;
  parameters?: Record<string, string | number | boolean>;
  status: "requested" | "accepted" | "processing" | "completed" | "failed";
  dispatch_state?: "waiting" | "dispatching" | "reconciling" | "provider_owned" | "terminal";
  queue_ahead?: number;
  next_attempt_at?: string | null;
  dispatch_error?: { code: string; message: string } | null;
  updated_at: string;
  summary: ConnectSummary | null;
  outputs?: ConnectOutputMetadata[];
  error: { code: string; message: string; retryable?: boolean } | null;
}

export interface InboxItem {
  message_id: string;
  thread_entry_ref: InboxMessageRef;
  provider: string;
  account_id: string;
  received_at: string;
  sender: string;
  sender_name: string | null;
  subject: string;
  status: string;
  category: string | null;
  priority: string | null;
  summary: string | null;
  action_required: number | null;
  suggested_action: string | null;
  deadline_text: string | null;
  deadline_iso: string | null;
  fallback_notified_at: string | null;
  notified_at: string | null;
  last_error: string | null;
  analysis_retryable: boolean | null;
  analysis_error_code: string | null;
  analysis_retry_after_seconds: number | null;
  body_truncated?: boolean | null;
  body_analyzed_chars?: number | null;
  body_source_chars?: number | null;
  attachments: InboxAttachment[];
  calendar_proposal: CalendarProposalPreview | null;
  calendar_write_outcome?: CalendarWriteOutcome | null;
  admission: InboxAdmission | null;
}

interface InboxAdmission {
  kind: InboxAdmissionKind;
  selector_id: string;
  display_name: string | null;
  admitted_at: string;
}

export interface CalendarProposalPreview {
  run_id: string;
  state:
    | "awaiting_confirmation"
    | "manual_review"
    | "declined"
    | "write_authorized"
    | "writing"
    | "unresolved"
    | "reconciling"
    | "completed"
    | "failed";
  state_version: number;
  proposal_version: number;
  proposal_sha256: string;
  status: "accepted" | "no_suggestions";
  provider: string;
  account_id: string;
  account_display_name: string;
  account_address: string | null;
  subject: string;
  attendees: string[];
  start: string | null;
  end: string | null;
  timezone: string | null;
  suggestion_reason: string | null;
  empty_reason: string | null;
  observed_at: string;
  expires_at: string | null;
  write_status: string | null;
  graph_event_id: string | null;
}
