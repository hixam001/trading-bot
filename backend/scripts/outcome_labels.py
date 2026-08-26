"""
Outcome Labels script for evaluating LLM predictive performance.
Retroactively joins decision_commits (thinker verdicts) to trade outcomes (realized PnL).
"""
import asyncio
import click
import logging

from api.db import get_db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

async def generate_outcome_labels(limit: int):
    try:
        async with get_db() as conn:
            cursor = await conn.execute(
                """
                SELECT 
                    dc.id as commit_id, dc.symbol, dc.mint_address, dc.verdict, dc.created_at,
                    t.realized_pnl_pct, t.exit_reason
                FROM decision_commits dc
                JOIN trades t ON t.mint_address = dc.mint_address
                WHERE t.is_open = 0
                ORDER BY dc.created_at DESC
                LIMIT ?
                """, (limit,)
            )
            rows = await cursor.fetchall()
            
        if not rows:
            print("No completed trades found with associated decision commits.")
            return

        true_positives = 0
        false_positives = 0
        
        print(f"{'Commit ID':<10} | {'Symbol':<10} | {'Verdict':<8} | {'PnL %':<10} | {'Outcome'}")
        print("-" * 65)
        for row in rows:
            commit_id = row["commit_id"]
            symbol = row["symbol"]
            verdict = row["verdict"]
            pnl_pct = row["realized_pnl_pct"] or 0.0
            
            outcome = "WIN" if pnl_pct > 0 else "LOSS"
            if verdict == "buy" and pnl_pct > 0:
                true_positives += 1
            elif verdict == "buy" and pnl_pct <= 0:
                false_positives += 1
                
            print(f"{commit_id:<10} | {symbol:<10} | {verdict:<8} | {pnl_pct:>+8.2f}% | {outcome}")
            
        total_buys = true_positives + false_positives
        precision = (true_positives / total_buys * 100) if total_buys > 0 else 0.0
        
        print("-" * 65)
        print(f"Total Evaluated: {len(rows)}")
        print(f"Model Buys: {total_buys} (True Positives: {true_positives}, False Positives: {false_positives})")
        print(f"Model Precision (Win Rate on Buys): {precision:.1f}%")

    except Exception as e:
        log.exception("Error generating outcome labels.")


@click.command()
@click.option("--limit", default=100, help="Number of recent closed trades to evaluate.")
def main(limit):
    asyncio.run(generate_outcome_labels(limit))

if __name__ == "__main__":
    main()
