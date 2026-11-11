"""Adapt legacy canned HTTP clients to httpx's streaming response protocol.

Only test transport plumbing: production bounded_request consumes the actual
stream, enforces byte limits and parses the same canned body as before.
"""
from contextlib import asynccontextmanager
import inspect
import json

import httpx


class _CannedBytes(httpx.AsyncByteStream):
    def __init__(self, body):
        self.body = body

    async def __aiter__(self):
        yield self.body


class LegacyHTTPStreamClient:
    async def post(self, url, **kwargs):
        # Older search fixtures canned build_request/send rather than post.
        request = self.build_request("POST", url, **kwargs)
        return await self.send(request)

    @asynccontextmanager
    async def stream(self, method, url, **kwargs):
        fn = getattr(self, method.lower())
        parameters = inspect.signature(fn).parameters
        if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
            kwargs = {key: value for key, value in kwargs.items() if key in parameters}
        canned = await fn(url, **kwargs)
        text = getattr(canned, 'text', None)
        if isinstance(text, str) and text:
            body = text.encode()
        else:
            try:
                body = json.dumps(canned.json()).encode()
            except ValueError:
                body = b''
        response = httpx.Response(canned.status_code,
                                  headers=getattr(canned, 'headers', {}),
                                  stream=_CannedBytes(body),
                                  request=httpx.Request(method, url))
        try:
            yield response
        finally:
            await response.aclose()
