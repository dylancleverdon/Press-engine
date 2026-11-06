// Shapes returned by the Press Engine backend (see backend/press_engine/api/views.py).

export type PersonBrief = {
  id: number;
  name: string;
  kind: "press" | "creator" | "curator";
  status: string;
  taste_score: number;
  outlet: string | null;
  email: string | null;
  found_by: string | null;
  opted_out: boolean;
  needs_review: boolean;
};

export type Outreach = {
  id: number;
  channel: "email" | "dm";
  platform: string | null;
  to: string | null;
  to_kind: string | null;
  status: string;
  subject: string | null;
  body: string | null;
  queue_date: string | null;
  scheduled_at: string | null;
  sent_at: string | null;
  replied_at: string | null;
  reply_snippet: string | null;
  sent_by: string | null;
  error: string | null;
  person: PersonBrief;
  profile_url: string | null;
};

export type Today = {
  date: string;
  timezone: string;
  cap: number;
  sent_today: number;
  awaiting_approval: Outreach[];
  scheduled: Outreach[];
  sent: Outreach[];
  dm_queue: number;
  new_leads_24h: number;
  unread_replies: number;
  pending_merges: number;
  google_connected: boolean;
  sending_paused: boolean;
  warnings: string[];
};

export type InboundMessage = {
  id: number;
  kind: "reply" | "auto_reply" | "bounce";
  from: string | null;
  from_name: string | null;
  subject: string | null;
  snippet: string | null;
  received_at: string;
  read: boolean;
  opt_out: boolean;
  outreach_id: number | null;
  person: PersonBrief | null;
};

export type ThreadMessage = {
  id: string;
  from_name: string;
  from_address: string;
  is_me: boolean;
  date: string;
  subject: string | null;
  text: string;
};

export type Me = { id: number; name: string; role: "owner" | "member"; google_connected: boolean };

export type CaptureResult = { person_id: number; created: boolean; name: string; needs_review: boolean; message: string };
