# 12 — THE FREE SOCIAL STACK (§47 + §51)

**Every social-facing input now has a free transport.** No paid key is
required anywhere in the read path; paid keys survive only as opt-in
shadow-week failover.

| Stage | Free transport | Paid fallback |
|---|---|---|
| Crowd conviction (fomo.fun board) | §47 Scrapling: curl-cffi Chrome-TLS impersonation (100% pass from a residential IP, p50 ~0.4s) → patchright stealth browser with Cloudflare solver | Firecrawl/ScrapingBee/… keys, if set |
| Web-search evidence (thinker's "Web (last 24h)" line) | §51 chain: **Brave Search API** (free $5 monthly credit ≈ 1,000 searches) → **self-hosted SearXNG** (keyless, unlimited) | Firecrawl `/v1/search`, if `FIRECRAWL_API_KEY` set |
| Realtime social read (attention classifier) | **Groq free tier** via `SOCIAL_LLM_*` (1,000 req/day free) — staged §51 so the budget covers it | any OpenAI-compatible endpoint |

## 1. Why Scrapling is NOT the search transport

Scrapling is a stealth *fetcher/parser* — it defeats TLS-fingerprint and
Cloudflare blocks (that is exactly why the fomo board uses it), but it has
no search index. The evidence stage needs **ranked result rows**
(title + description), which only a search API or metasearch engine
produces. The §51 chain exists because of that difference.

## 2. Setup

```bash
# 2a. Brave (primary, ~1,000 free searches/month, auto credit)
#     Get the key: https://api-dashboard.search.brave.com (free plan)
echo 'BRAVE_SEARCH_API_KEY=BSA…' >> .env

# 2b. SearXNG sidecar (keyless, unlimited) — compose runs it automatically:
docker compose up -d searxng
echo 'SEARXNG_URL=http://searxng:8080' >> .env    # inside the compose network
#     Bare-metal alternative: docker run -d --name searxng -p 127.0.0.1:8888:8080 \
#       -v $(pwd)/deploy/searxng/settings.yml:/etc/searxng/settings.yml:ro searxng/searxng
#     then SEARXNG_URL=http://127.0.0.1:8888

# 2c. Social read (free Groq tier) — the key you may already have:
echo 'SOCIAL_LLM_API_KEY=gsk_…' >> .env           # base/model defaults are Groq
```

Notes:
- **Public SearXNG instances do not work** — they disable `format=json`
  (403). That is why this repo ships `deploy/searxng/settings.yml`
  (`search.formats: [html, json]`, `limiter: false`) and a compose service.
- The sidecar is not published to the host; only the engine can reach it.
- The Firecrawl key can stay for one more shadow week, then be emptied:
  the chain treats it as the last-resort hop only.

## 3. Behavior contract (what the code guarantees)

- **Same evidence shape**: every hop normalizes to `{title, description}`
  rows, so the thinker's `Web (last 24h)` line is byte-identical to §48.
  The 24h window is preserved per hop: Brave `freshness=pd`, SearXNG
  `time_range=day`, Firecrawl `tbs=qdr:d`.
- **§34 benching everywhere**: a hop reporting credit/quota exhaustion
  (402/422) is benched 30m; a 429 gets a 75s backoff; two consecutive
  transport failures bench it — a dead hop costs a couple of timeouts
  ONCE, never one per candidate per tick.
- **Staged, cached, reused**: searches and social reads run ONLY for
  candidates that passed every rule (inside `gate_candidate_staged`);
  web evidence is cached per mint (hits 2h / misses 30m); a candidate that
  already carries `social_interest` is never re-read.
- **Hermetic**: mock runs and tests never touch any hop; empty keys disable
  stages fail-soft. All of this is pinned offline by `test_web_staging.py`
  (staging/cache/keying), `test_search_chain.py` (bench + fall-through
  semantics) and `test_social_staging.py` (staged social read) — fake
  clients, no network.

## 4. Free-budget arithmetic

- Search volume after §48 staging: only all-passed candidates (a handful
  per tick, cache-deduplicated) → well under Brave's ~1,000/month; SearXNG
  absorbs any overflow, keyless.
- Social reads after §51 staging: same population, 200-token outputs →
  far under Groq's free 1,000 req/day (the old un-staged head-of-board
  read could have burned ~11k/day).
