// src/App.tsx — Main application layout

import { useState } from "react";
import { PaperTradingBanner } from "./components/PaperTradingBanner";
import { SystemStatusBar } from "./components/SystemStatus";
import { LiveFeed } from "./components/LiveFeed";
import { Holdings } from "./components/Holdings";
import { TradeJournal } from "./components/TradeJournal";
import { StatsDashboard } from "./components/StatsDashboard";
import { PromotionGatePanel } from "./components/PromotionGate";
import { KnowledgeBasePanel } from "./components/KnowledgeBase";

type Tab = "feed" | "holdings" | "journal" | "stats" | "gate" | "kb";

const TABS: { id: Tab; label: string; icon: string }[] = [
  { id: "feed", label: "Live Feed", icon: "◎" },
  { id: "holdings", label: "Holdings", icon: "◈" },
  { id: "journal", label: "Journal", icon: "◧" },
  { id: "stats", label: "Stats", icon: "◉" },
  { id: "gate", label: "Gate", icon: "◬" },
  { id: "kb", label: "Knowledge", icon: "◫" },
];

export default function App() {
  const [activeTab, setActiveTab] = useState<Tab>("feed");

  return (
    <div className="min-h-screen bg-bg font-sans">
      {/* FR-22: Persistent paper trading banner — always at top */}
      <PaperTradingBanner />

      {/* Main layout — offset by banner height */}
      <div className="pt-9 flex flex-col h-screen">
        {/* ── Top nav bar ───────────────────────────────────────────────── */}
        <header
          className="glass border-b border-border px-4 py-2.5 flex items-center justify-between shrink-0"
        >
          {/* Brand */}
          <div className="flex items-center gap-3">
            <div
              className="w-8 h-8 rounded-lg flex items-center justify-center font-mono font-bold text-sm"
              style={{
                background: "linear-gradient(135deg, hsl(142,50%,20%), hsl(142,30%,12%))",
                border: "1px solid hsl(142,40%,25%)",
                boxShadow: "0 0 12px hsl(142,50%,15%)",
              }}
            >
              <span className="text-gradient-green">AI</span>
            </div>
            <div>
              <h1 className="text-sm font-semibold text-text-primary leading-none">
                Trading Bot
              </h1>
              <p className="text-xs text-text-muted leading-none mt-0.5">
                Solana Memecoin Research
              </p>
            </div>
          </div>

          {/* System status */}
          <SystemStatusBar />
        </header>

        {/* ── Tab bar ───────────────────────────────────────────────────── */}
        <nav
          className="glass border-b border-border px-4 py-1.5 flex items-center gap-1 shrink-0 overflow-x-auto no-scrollbar"
        >
          {TABS.map((tab) => (
            <button
              key={tab.id}
              id={`tab-${tab.id}`}
              onClick={() => setActiveTab(tab.id)}
              className={`tab-btn flex items-center gap-1.5 whitespace-nowrap ${
                activeTab === tab.id ? "active" : ""
              }`}
            >
              <span className="opacity-60">{tab.icon}</span>
              {tab.label}
            </button>
          ))}
        </nav>

        {/* ── Content area ──────────────────────────────────────────────── */}
        <main className="flex-1 overflow-y-auto p-4">
          <div className="max-w-7xl mx-auto">
            {activeTab === "feed" && (
              <div className="h-[calc(100vh-10rem)]">
                <LiveFeed />
              </div>
            )}

            {activeTab === "holdings" && (
              <div className="h-[calc(100vh-10rem)]">
                <Holdings />
              </div>
            )}

            {activeTab === "journal" && (
              <div className="h-[calc(100vh-10rem)]">
                <TradeJournal />
              </div>
            )}

            {activeTab === "stats" && <StatsDashboard />}

            {activeTab === "gate" && (
              <div className="max-w-2xl mx-auto">
                <PromotionGatePanel />
              </div>
            )}

            {activeTab === "kb" && <KnowledgeBasePanel />}
          </div>
        </main>
      </div>
    </div>
  );
}
