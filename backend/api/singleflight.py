"""Bounded single-flight for fixed, argument-free public proof routes."""
import asyncio
from functools import wraps


def singleflight(fn):
    task = None

    @wraps(fn)
    async def wrapped():
        nonlocal task
        if task is None or task.done():
            async def run():
                nonlocal task
                try:
                    return await fn()
                finally:
                    task = None
            task = asyncio.create_task(run())
            # Consume exceptions if every shielded waiter has cancelled.
            task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(task)
    return wrapped
