// src/hooks/useWebSocket.ts — WebSocket hook for live feed events

import { useCallback, useEffect, useRef, useState } from "react";
import type { FeedEvent } from "../types";

interface UseWebSocketResult {
  events: FeedEvent[];
  connected: boolean;
  error: string | null;
  clearEvents: () => void;
}

const WS_URL = `ws://${window.location.host}/ws/feed`;
const MAX_EVENTS = 200; // keep last 200 in memory

export function useWebSocket(): UseWebSocketResult {
  const [events, setEvents] = useState<FeedEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    try {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        setError(null);
      };

      ws.onmessage = (evt) => {
        try {
          const msg = JSON.parse(evt.data);
          if (msg.type === "feed_event" && msg.data) {
            setEvents((prev) => {
              const updated = [msg.data as FeedEvent, ...prev];
              return updated.slice(0, MAX_EVENTS);
            });
          }
        } catch {
          // Ignore malformed messages
        }
      };

      ws.onerror = () => {
        setError("WebSocket error");
      };

      ws.onclose = () => {
        setConnected(false);
        // Auto-reconnect with backoff
        reconnectTimer.current = setTimeout(connect, 3000);
      };
    } catch (e) {
      setError("Failed to connect");
      reconnectTimer.current = setTimeout(connect, 5000);
    }
  }, []);

  useEffect(() => {
    connect();
    return () => {
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  const clearEvents = useCallback(() => setEvents([]), []);

  return { events, connected, error, clearEvents };
}
