import type { DropoutResponse, HealthStatus, SourceRecord } from "./types";

const API_BASE = (import.meta.env.VITE_API_URL || "").replace(/\/$/, "");

type ChatHandlers = {
  onRoute: (route: string, dataset: string) => void;
  onRetrieval: (records: SourceRecord[], latencyMs: number) => void;
  onAnalysis: (sql: string, rowCount: number) => void;
  onToken: (token: string) => void;
  onDone: (conversationId: string) => void;
  onError: (message: string) => void;
};

export async function getHealth(): Promise<HealthStatus> {
  const response = await fetch(`${API_BASE}/api/health`);
  if (!response.ok) throw new Error("Health check failed");
  return response.json();
}

export async function getDropoutAlerts(limit = 20): Promise<DropoutResponse> {
  const response = await fetch(`${API_BASE}/api/dropout/alerts?limit=${limit}`);
  if (!response.ok) throw new Error("Early-warning data is unavailable");
  return response.json();
}

export async function streamChat(
  message: string,
  conversationId: string | null,
  history: Array<{ role: "user" | "assistant"; text: string }>,
  handlers: ChatHandlers,
) {
  const response = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, conversation_id: conversationId, history }),
  });
  if (!response.ok || !response.body) throw new Error("The chat service is unavailable.");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() ?? "";
    for (const rawEvent of events) {
      const lines = rawEvent.split("\n");
      const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim();
      const data = lines.find((line) => line.startsWith("data:"))?.slice(5).trim();
      if (!event || !data) continue;
      const payload = JSON.parse(data);
      if (event === "route") handlers.onRoute(payload.route, payload.dataset);
      if (event === "retrieval") handlers.onRetrieval(payload.records, payload.latency_ms);
      if (event === "analysis") handlers.onAnalysis(payload.sql, payload.row_count);
      if (event === "token") handlers.onToken(payload.text);
      if (event === "done") handlers.onDone(payload.conversation_id);
      if (event === "error") handlers.onError(payload.message);
    }
  }
}
