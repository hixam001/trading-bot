"""
Shadow Replay script for model regression testing.
Fetches recent decision_commits and re-runs the Thinker against the same snapshots.
"""
import asyncio
import json
import logging
from pprint import pprint

import click

from api.db import get_db
from models import Candidate
from llm.thinker import Thinker

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

async def run_shadow_replay(limit: int):
    thinker = Thinker()
    try:
        async with get_db() as conn:
            cursor = await conn.execute(
                "SELECT id, symbol, verdict, payload_json FROM decision_commits ORDER BY id DESC LIMIT ?", (limit,)
            )
            rows = await cursor.fetchall()
            
        if not rows:
            print("No decision commits found.")
            return

        matches = 0
        mismatches = 0
        
        for row in rows:
            commit_id = row["id"]
            symbol = row["symbol"]
            original_verdict = row["verdict"]
            payload = json.loads(row["payload_json"])
            
            candidate_dict = payload.get("candidate", {})
            if not candidate_dict:
                log.warning(f"Commit {commit_id} missing candidate payload.")
                continue
                
            c = Candidate(**candidate_dict)
            
            print(f"Replaying {symbol} (Commit {commit_id})...")
            new_think = await thinker.think(c)
            
            if new_think.verdict == original_verdict:
                matches += 1
                print(f"  [MATCH] Original: {original_verdict} | New: {new_think.verdict}")
            else:
                mismatches += 1
                print(f"  [MISMATCH] Original: {original_verdict} | New: {new_think.verdict}")
                print(f"  New Thesis: {new_think.thesis}")
                
        print(f"\nReplay Complete: {matches} matches, {mismatches} mismatches.")
    finally:
        await thinker.aclose()


@click.command()
@click.option("--limit", default=10, help="Number of recent decisions to replay.")
def main(limit):
    asyncio.run(run_shadow_replay(limit))

if __name__ == "__main__":
    main()
