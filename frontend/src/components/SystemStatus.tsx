// src/components/SystemStatus.tsx — FR-30: Ollama health + data backend indicator

import { useApi } from "../hooks/useApi";
import type { SystemStatus } from "../types";

export function SystemStatusBar() {
  const { data, error } = useApi<SystemStatus>("/api/system-status", 15_000);

  const ollama = data?.ollama;
  const backend = data?.data_backend ?? "…";

  return (
    <div className="flex items-center gap-4 text-xs font-mono">
      {/* Ollama status */}
      <div className="flex items-center gap-1.5">
        <span
          className={`status-dot ${
            !data
              ? "warn"
              : ollama?.ollama_reachable && ollama?.model_loaded
              ? "online"
              : "offline"
          }`}
        />
        <span className="text-text-muted">
          {!data
            ? "LLM checking…"
            : ollama?.ollama_reachable && ollama?.model_loaded
            ? `${ollama.model_name}`
            : ollama?.ollama_reachable
            ? "Model not loaded"
            : "LLM offline"}
        </span>
      </div>

      {/* Data backend badge */}
      <div className="flex items-center gap-1.5">
        <span
          className="px-1.5 py-0.5 rounded text-xs uppercase font-semibold tracking-wider"
          style={{
            background:
              backend === "mock"
                ? "hsl(217,50%,16%)"
                : backend === "birdeye"
                ? "hsl(142,40%,12%)"
                : "hsl(38,40%,12%)",
            color:
              backend === "mock"
                ? "hsl(217,91%,70%)"
                : backend === "birdeye"
                ? "hsl(142,71%,60%)"
                : "hsl(38,95%,70%)",
            border: `1px solid ${
              backend === "mock"
                ? "hsl(217,50%,25%)"
                : backend === "birdeye"
                ? "hsl(142,40%,22%)"
                : "hsl(38,60%,22%)"
            }`,
          }}
        >
          {backend}
        </span>
      </div>

      {error && (
        <span className="text-loss-text text-xs">API unreachable</span>
      )}
    </div>
  );
}
