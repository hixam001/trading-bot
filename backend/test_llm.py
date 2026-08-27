import asyncio
import os
import sys

import config
from llm.client import build_main_client, MainGroqClient, DeepSeekClient, GroqClient


async def ping(label: str, client) -> None:
    print(f"Checking {label} health...")
    ok = await client.health()
    print(f"{label} health: {'OK' if ok else 'FAIL'}")
    if ok:
        print(f"Sending a ping prompt to {label}...")
        res = await client.complete_json(
            task="test_ping",
            system_prompt="You are a helpful assistant.",
            user_prompt=f"Say '{label} is ready'",
            json_mode=False,
        )
        if res and res.text:
            print(f"{label} response: {res.text.strip()}")
            print(
                f"  usage: in={res.input_tokens} out={res.output_tokens} "
                f"cache={res.cache_hit_tokens} latency={res.latency_ms:.0f}ms "
                f"cost=${res.estimated_cost_usd:.6f} peak={res.is_peak_window} "
                f"snapshot={res.pricing_snapshot_id}"
            )
        else:
            print(f"{label} failed to respond correctly. Reason: {res.degradation_reason if res else 'None'}")
    await client.aclose()


async def check():
    print(f"MAIN_LLM_PROVIDER: {config.MAIN_LLM_PROVIDER!r}")
    print(f"Main Groq Key present: {bool(config.GROQ_API_KEY)}")
    print(f"DeepSeek Key present: {bool(config.DEEPSEEK_API_KEY)}")
    print(f"Social Groq Key present: {bool(config.SOCIAL_LLM_API_KEY)}")

    # The main client exactly as thinker/narrator build it.
    main = build_main_client()
    print(f"Factory selected main client: {main.provider}:{main.model}")
    if main.api_key:
        await ping(f"Main ({main.provider})", main)
    else:
        print(f"Main ({main.provider}) skipped: no API key configured")
        await main.aclose()

    # Explicit DeepSeek check whenever a key exists, even if Groq is still
    # the configured main provider (pre-flip verification).
    if config.DEEPSEEK_API_KEY and main.provider != "deepseek":
        await ping("DeepSeek (explicit)", DeepSeekClient())

    if config.SOCIAL_LLM_API_KEY:
        await ping("Social Groq", GroqClient())
    else:
        print("Social Groq skipped: no API key configured")

if __name__ == "__main__":
    # Add the current dir to sys.path so we can import from backend if running from project root
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    asyncio.run(check())
