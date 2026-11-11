"""Bound external HTTP bodies before parsing, including decompression.

Limits apply to wire bytes and decoded bytes separately. Unsupported transfer
encodings fail closed. Redirects are not followed: credential scope stays with
exact configured destinations. Callers retain their existing request timeouts.
"""
from __future__ import annotations

import asyncio
import zlib
import httpx

MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class ResponseLimitError(httpx.TransportError):
    pass


async def bounded_request(client, method: str, url: str, *, max_bytes: int = MAX_RESPONSE_BYTES, total_timeout: float = 60.0, **kwargs):
    if total_timeout <= 0:
        raise ValueError('response timeout must be positive')
    try:
        async with asyncio.timeout(total_timeout):
            return await _bounded_request(client, method, url, max_bytes=max_bytes, **kwargs)
    except TimeoutError:
        raise httpx.ReadTimeout('response total time limit exceeded') from None


async def _bounded_request(client, method: str, url: str, *, max_bytes: int, **kwargs):
    if max_bytes <= 0:
        raise ValueError('response limit must be positive')
    headers = dict(kwargs.pop('headers', None) or {})
    # Ask for uncompressed data; nevertheless safely handle servers ignoring it.
    headers['Accept-Encoding'] = 'identity'
    kwargs['follow_redirects'] = False
    async with client.stream(method, url, headers=headers, **kwargs) as response:
        declared = response.headers.get('content-length')
        if declared is not None:
            try:
                size = int(declared)
            except ValueError:
                raise ResponseLimitError('invalid response content length') from None
            if size < 0 or size > max_bytes:
                raise ResponseLimitError('response wire byte limit exceeded')
        # Custom in-process transports may supply an already-buffered response.
        # Real HTTP transports stream raw bytes here. Avoid decoding twice.
        buffered = response.is_stream_consumed
        encoding = 'identity' if buffered else response.headers.get('content-encoding', 'identity').strip().lower()
        if encoding in ('', 'identity'):
            decoder = None
        elif encoding == 'gzip':
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == 'deflate':
            decoder = zlib.decompressobj()
        else:
            raise ResponseLimitError('unsupported response content encoding')
        wire_bytes = 0
        body = bytearray()
        async def chunks():
            if buffered:
                yield response.content
            else:
                async for chunk in response.aiter_raw(chunk_size=65536):
                    yield chunk
        try:
            async for chunk in chunks():
                wire_bytes += len(chunk)
                if wire_bytes > max_bytes:
                    raise ResponseLimitError('response wire byte limit exceeded')
                decoded = decoder.decompress(chunk, max_bytes - len(body) + 1) if decoder else chunk
                body.extend(decoded)
                if len(body) > max_bytes or (decoder and decoder.unconsumed_tail):
                    raise ResponseLimitError('response decoded byte limit exceeded')
            if decoder:
                if not decoder.eof or decoder.unused_data:
                    raise ResponseLimitError('incomplete or concatenated compressed response')
        except zlib.error:
            raise ResponseLimitError('invalid compressed response') from None
        result_headers = dict(response.headers)
        result_headers.pop('content-encoding', None)
        result_headers.pop('content-length', None)
        return httpx.Response(response.status_code, headers=result_headers,
                              content=bytes(body), request=response.request,
                              extensions=response.extensions)
