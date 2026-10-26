/**
 * Server-Sent Events over fetch (so the Authorization header can be sent; EventSource cannot).
 * Reconnects with exponential backoff and resumes with Last-Event-ID. Events are persisted
 * server-side, so a reconnect replays anything missed; callers dedupe by sequence number.
 */
import { API_URL, authHeaders } from "./api";

export type SSEMessage = { id: string | null; event: string; data: string };

/** Incremental SSE parser: feed text chunks, get complete messages. */
export class SSEParser {
  private buffer = "";
  private id: string | null = null;
  private event = "message";
  private data: string[] = [];

  feed(chunk: string): SSEMessage[] {
    this.buffer += chunk;
    const out: SSEMessage[] = [];
    let idx: number;
    while ((idx = this.buffer.search(/\r?\n/)) !== -1) {
      const line = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + (this.buffer[idx] === "\r" ? 2 : 1));
      if (line === "") {
        if (this.data.length) out.push({ id: this.id, event: this.event, data: this.data.join("\n") });
        this.event = "message";
        this.data = [];
        continue;
      }
      if (line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);
      if (field === "data") this.data.push(value);
      else if (field === "event") this.event = value;
      else if (field === "id") this.id = value;
    }
    return out;
  }
}

export type StreamState = "connecting" | "open" | "reconnecting" | "closed";

export function connectRunStream(
  runId: string,
  opts: Parameters<typeof connectEventStream>[1],
): () => void {
  return connectEventStream(`/api/v1/runs/${runId}/stream`, opts);
}

/** Generic authenticated SSE connection with backoff and Last-Event-ID resume. */
export function connectEventStream(
  path: string,
  opts: {
    lastSequence: () => number;
    onMessage: (m: SSEMessage) => void;
    onState?: (s: StreamState) => void;
  },
): () => void {
  let stopped = false;
  let controller: AbortController | null = null;
  let attempt = 0;

  const run = async () => {
    while (!stopped) {
      controller = new AbortController();
      opts.onState?.(attempt === 0 ? "connecting" : "reconnecting");
      try {
        const res = await fetch(`${API_URL}${path}`, {
          headers: { ...authHeaders(), Accept: "text/event-stream", "Last-Event-ID": String(opts.lastSequence()) },
          signal: controller.signal,
          cache: "no-store",
        });
        if (res.status === 401 || res.status === 403 || res.status === 404) {
          opts.onState?.("closed");
          return;
        }
        if (!res.ok || !res.body) throw new Error(`stream failed: ${res.status}`);
        opts.onState?.("open");
        attempt = 0;
        const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
        const parser = new SSEParser();
        let ended = false;
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          for (const msg of parser.feed(value)) {
            if (msg.event === "stream.end") ended = true;
            opts.onMessage(msg);
          }
        }
        if (ended) {
          opts.onState?.("closed");
          return;
        }
      } catch (err) {
        if (stopped || (err instanceof DOMException && err.name === "AbortError")) return;
      }
      attempt += 1;
      const delay = Math.min(15000, 500 * 2 ** Math.min(attempt, 5)) * (0.5 + Math.random() / 2);
      await new Promise((r) => setTimeout(r, delay));
    }
  };
  void run();
  return () => {
    stopped = true;
    controller?.abort();
  };
}
