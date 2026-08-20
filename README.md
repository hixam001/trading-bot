# Trading Bot

AI-assisted Solana memecoin **paper-trading** research system.

> ⚠️ **PAPER TRADING ONLY. No real funds. No wallet signing. No real transactions.**

## Project layout

```
trading-bot/
├── backend/          ← Python backend (FastAPI API + tick loop)
│   ├── config.py
│   ├── models.py
│   ├── deterministic_filter.py
│   ├── knowledge_base.py
│   ├── llm_scorer.py
│   ├── data_ingestion.py
│   ├── paper_trading_engine.py
│   ├── main.py          ← tick loop (run separately)
│   ├── learning_loop.py
│   ├── promotion_gate.py
│   ├── requirements.txt
│   ├── api/
│   │   ├── main.py      ← FastAPI app (uvicorn entry point)
│   │   ├── db.py
│   │   ├── websocket.py
│   │   └── routes/
│   ├── knowledge_base/
│   │   ├── static_knowledge.md
│   │   └── ingested/    ← operator-supplied files (gitignored)
│   └── tests/
├── frontend/         ← React + Vite + Tailwind v3 dashboard
└── .env              ← secrets (gitignored)
```

## Quick start

### 1. Copy env and fill in API keys

```bash
cp .env.example .env
# Edit .env — add BIRDEYE_API_KEY if using birdeye backend
```

### 2. Backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Start the FastAPI server (reads/writes SQLite)
uvicorn api.main:app --host 0.0.0.0 --port 8000 --reload

# In a second terminal — start the tick loop
python main.py
```

### 3. Frontend

```bash
cd frontend
npm install
npm run dev   # → http://localhost:5173
```

### 4. Run tests

```bash
cd backend
pytest tests/ -v
```

## Data backends

Set `DATA_BACKEND` in `.env`:

| Value | Description |
|-------|-------------|
| `mock` | Synthetic data, no API key needed (default) |
| `birdeye` | Real Solana data via BirdEye API |
| `coinstats` | **Not implemented** — intentional stub |

## Key configuration (`backend/config.py`)

| Setting | Default | Note |
|---------|---------|------|
| `PAPER_TRADING_ONLY` | `True` | Hardcoded — not env-configurable |
| `INITIAL_CASH_USD` | `$1,000` | Starting paper balance |
| `POSITION_SIZE_PCT` | `10%` | Per-trade allocation |
| `TAKE_PROFIT_PCT` | `+50%` | Exit threshold |
| `STOP_LOSS_PCT` | `-20%` | Stop threshold |
| `MAX_HOLD_HOURS` | `72h` | Force-close after this |

## LLM

Requires [Ollama](https://ollama.ai) running locally with `qwen3:8b` pulled:

```bash
ollama pull qwen3:8b
ollama serve
```
