"""Bounded download + decompression for third-party archive imports.

The EVE Ref importers fetch and decompress external archives. A malicious mirror or an
on-path attacker (TLS notwithstanding) could serve a small file that expands to many GB
and OOM the import worker. These helpers cap both the compressed download and the
decompressed read so a bomb fails fast instead of exhausting memory.
"""
from __future__ import annotations

import io

MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024          # compressed download ceiling (512 MB)
MAX_DECOMPRESSED_BYTES = 1024 * 1024 * 1024      # decompressed read ceiling (1 GB)
MAX_MEMBER_BYTES = 512 * 1024 * 1024             # single archive member ceiling (512 MB)


class DataTooLarge(Exception):
    """Raised when a download or decompressed stream exceeds its byte ceiling."""


def download_to_buffer(resp, *, max_bytes: int = MAX_DOWNLOAD_BYTES, chunk: int = 131072) -> io.BytesIO:
    """Stream a (streamed) requests response into a BytesIO, aborting past ``max_bytes``."""
    buf = io.BytesIO()
    total = 0
    for part in resp.iter_content(chunk_size=chunk):
        if not part:
            continue
        total += len(part)
        if total > max_bytes:
            raise DataTooLarge(f"download exceeded {max_bytes} bytes")
        buf.write(part)
    buf.seek(0)
    return buf


class CappedReader(io.RawIOBase):
    """Wrap a binary stream; raise ``DataTooLarge`` once total bytes read exceed the cap."""

    def __init__(self, fp, max_bytes: int = MAX_DECOMPRESSED_BYTES):
        self._fp = fp
        self._max = max_bytes
        self._n = 0

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        chunk = self._fp.read(len(b))
        if not chunk:
            return 0
        self._n += len(chunk)
        if self._n > self._max:
            raise DataTooLarge(f"decompressed data exceeded {self._max} bytes")
        b[: len(chunk)] = chunk
        return len(chunk)


def capped_text(binary_stream, *, max_bytes: int = MAX_DECOMPRESSED_BYTES, encoding: str = "utf-8"):
    """A text stream over a binary (e.g. bz2) stream, capped at ``max_bytes`` decompressed."""
    return io.TextIOWrapper(io.BufferedReader(CappedReader(binary_stream, max_bytes)), encoding=encoding)


# API/LLM JSON bodies are small; a few MB is already generous. This ceiling exists only to
# stop a hostile-but-allowlisted or self-hosted upstream (e.g. a compromised or misbehaving
# LLM endpoint) from returning a multi-GB body that ``requests`` would buffer whole into the
# worker's memory on ``.json()`` — an OOM. Callers obtain the response with ``stream=True``.
MAX_API_BODY_BYTES = 32 * 1024 * 1024  # 32 MB


def read_capped(resp, *, max_bytes: int = MAX_API_BODY_BYTES, chunk: int = 65536) -> bytes:
    """Read a *streamed* ``requests`` response body into bytes, aborting past ``max_bytes``.

    Bounds an API-client read so an oversized upstream reply can't exhaust worker memory
    before ``.json()``/``.text`` buffers it whole. Pass a response obtained with
    ``requests.<verb>(..., stream=True)``. Raises :class:`DataTooLarge` past the ceiling.
    """
    total = 0
    parts: list[bytes] = []
    for part in resp.iter_content(chunk_size=chunk):
        if not part:
            continue
        total += len(part)
        if total > max_bytes:
            raise DataTooLarge(f"response body exceeded {max_bytes} bytes")
        parts.append(part)
    return b"".join(parts)
