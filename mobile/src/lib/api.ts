import type { CaptureResult, InboundMessage, Me, Outreach, ThreadMessage, Today } from "./types";

export class ApiError extends Error {
  constructor(message: string, readonly status?: number) {
    super(message);
  }
}

export class ApiClient {
  constructor(private readonly baseUrl: string, private readonly token: string) {}

  private async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    if (!this.baseUrl || !this.token) throw new ApiError("Set the backend address and your token in Settings.");
    let res: Response;
    try {
      res = await fetch(`${this.baseUrl}${path}`, {
        ...init,
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${this.token}`, ...(init.headers ?? {}) },
      });
    } catch {
      throw new ApiError(`Can't reach ${this.baseUrl}. Is the backend running and reachable from this phone?`);
    }
    const body: unknown = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = body && typeof body === "object" && "detail" in body ? (body as { detail: unknown }).detail : null;
      const message = res.status === 401 ? "The backend rejected your token." : typeof detail === "string" ? detail : `HTTP ${res.status}`;
      throw new ApiError(message, res.status);
    }
    return body as T;
  }

  me() {
    return this.request<Me>("/api/me");
  }

  today() {
    return this.request<Today>("/api/today");
  }

  approve(id: number, edits?: { subject?: string; body?: string }) {
    return this.request<Outreach>(`/api/outreach/${id}/approve`, { method: "POST", body: JSON.stringify(edits ?? {}) });
  }

  skip(id: number) {
    return this.request<Outreach>(`/api/outreach/${id}/skip`, { method: "POST" });
  }

  dmQueue() {
    return this.request<Outreach[]>("/api/dm-queue");
  }

  markDmSent(id: number, body?: string) {
    return this.request<Outreach>(`/api/outreach/${id}/dm-sent`, { method: "POST", body: JSON.stringify({ body }) });
  }

  inbox() {
    return this.request<InboundMessage[]>("/api/inbox");
  }

  markRead(messageId: number) {
    return this.request<{ ok: boolean }>(`/api/inbox/${messageId}/read`, { method: "POST" });
  }

  thread(outreachId: number) {
    return this.request<{ outreach: Outreach; messages: ThreadMessage[] }>(`/api/outreach/${outreachId}/thread`);
  }

  draft(outreachId: number) {
    return this.request<{ draft: string }>(`/api/outreach/${outreachId}/draft`, { method: "POST" });
  }

  reply(outreachId: number, body: string) {
    return this.request<{ ok: boolean; person_status: string }>(`/api/outreach/${outreachId}/reply`, {
      method: "POST",
      body: JSON.stringify({ body }),
    });
  }

  capture(payload: { url: string; note?: string; email?: string }) {
    return this.request<CaptureResult>("/api/capture", { method: "POST", body: JSON.stringify({ ...payload, source: "share" }) });
  }

  registerDevice(expoPushToken: string, platform: string) {
    return this.request<{ ok: boolean }>("/api/devices", {
      method: "POST",
      body: JSON.stringify({ expo_push_token: expoPushToken, platform }),
    });
  }

  setNotifications(notifyReplies: boolean) {
    return this.request<{ ok: boolean }>("/api/me/notifications", {
      method: "POST",
      body: JSON.stringify({ notify_replies: notifyReplies }),
    });
  }
}
