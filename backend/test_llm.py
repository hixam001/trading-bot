import asyncio
import os
import sys

import config
from llm.client import MainGroqClient, GroqClient

async def check():
    print(f"Main Groq Key present: {bool(config.GROQ_API_KEY)}")
    print(f"Social Groq Key present: {bool(config.SOCIAL_LLM_API_KEY)}")

    if config.GROQ_API_KEY:
        print("Checking Main Groq health...")
        dc = MainGroqClient()
        ok = await dc.health()
        print(f"Main Groq health: {'OK' if ok else 'FAIL'}")
        if ok:
            print("Sending a ping prompt to Main Groq...")
            res = await dc.complete_json(
                task="test_ping",
                system_prompt="You are a helpful assistant.",
                user_prompt="Say 'Main Groq is ready'",
                json_mode=False
            )
            if res and res.text:
                print(f"Main Groq response: {res.text.strip()}")
            else:
                print(f"Main Groq failed to respond correctly. Reason: {res.degradation_reason if res else 'None'}")
        await dc.aclose()
        
    if config.SOCIAL_LLM_API_KEY:
        print("Checking Social Groq health...")
        gc = GroqClient()
        ok = await gc.health()
        print(f"Social Groq health: {'OK' if ok else 'FAIL'}")
        if ok:
            print("Sending a ping prompt to Social Groq...")
            res = await gc.complete_json(
                task="test_ping",
                system_prompt="You are a helpful assistant.",
                user_prompt="Say 'Social Groq is ready'",
                json_mode=False
            )
            if res and res.text:
                print(f"Social Groq response: {res.text.strip()}")
            else:
                print(f"Social Groq failed to respond correctly. Reason: {res.degradation_reason if res else 'None'}")
        await gc.aclose()

if __name__ == "__main__":
    # Add the current dir to sys.path so we can import from backend if running from project root
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    asyncio.run(check())
