"""Streaming a stored file to a client, ranges and all.

What's streamed is a key in a Store (or a Cache: playback cuts), opened
before the answer starts, so its length is the file's as it was opened and a
file replaced meanwhile is never spliced: the reader keeps the one it opened.
"""

from __future__ import annotations

import io
from collections.abc import Iterator

from fastapi import Request
from fastapi.responses import StreamingResponse

from src.server.storage.store import Store

CHUNK = 64 * 1024


def stream_stored(store: Store, key: str, request: Request, media_type: str,
                  etag: str | None = None) -> StreamingResponse:
    """Stream `key`, honoring a single Range: `bytes=a-b`, `bytes=a-` or `bytes=-n`.

    A range starting past the end is a 416; a malformed one gets the whole
    file, and so does one whose If-Range names another version (`etag`).
    NotFound and Unavailable come from opening it, before any answer.
    """
    f = store.open(key)
    try:
        file_size = f.seek(0, io.SEEK_END)
    except BaseException:
        f.close()
        raise
    range_header = request.headers.get("range")
    start = 0
    end = file_size - 1
    status_code = 200
    headers: dict[str, str] = {"Accept-Ranges": "bytes"}
    if etag:
        headers["ETag"] = etag

    if_range = request.headers.get("if-range")
    if range_header and if_range and etag and if_range.strip() != etag:
        range_header = None  # the client holds another version: send this one whole
    if range_header:
        units, _, range_spec = range_header.partition("=")
        start_str, _, end_str = range_spec.split(",")[0].strip().partition("-")
        try:
            if units.strip().lower() != "bytes":
                raise ValueError(units)
            if start_str:
                start = int(start_str)
                if end_str:
                    end = min(int(end_str), file_size - 1)
            else:  # the last n bytes
                start = max(file_size - int(end_str), 0)
            if start >= file_size:
                f.close()
                return StreamingResponse(
                    iter(()), status_code=416,
                    headers={**headers, "Content-Range": f"bytes */{file_size}"},
                )
            if start > end:
                raise ValueError(range_header)
            status_code = 206
        except ValueError:
            start, end, status_code = 0, file_size - 1, 200

    content_length = end - start + 1
    headers["Content-Length"] = str(content_length)
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"

    def chunks() -> Iterator[bytes]:
        with f:
            f.seek(start)
            remaining = content_length
            while remaining > 0:
                chunk = f.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return StreamingResponse(chunks(), media_type=media_type, status_code=status_code, headers=headers)
