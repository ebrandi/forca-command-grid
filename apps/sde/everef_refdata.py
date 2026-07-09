"""Read packaged volumes from EVE Ref's reference-data bundle.

EVE Ref publishes a single ``reference-data-latest.tar.xz`` whose ``types.json`` keys
every type by id and carries a ``packaged_volume`` (repackaged m³) that the raw SDE
doesn't expose cleanly. We use it to size jump-freight loads accurately.

Pure parsing — no network, no DB — so it's easy to unit-test; the command
``import_everef_reference_data`` does the downloading and storing.
"""
from __future__ import annotations

import json
import tarfile
from collections.abc import Iterator
from typing import BinaryIO


def iter_type_volumes(fileobj: BinaryIO) -> Iterator[tuple[int, float]]:
    """Yield ``(type_id, packaged_volume)`` for every type that has one."""
    from core.netcap import MAX_DECOMPRESSED_BYTES, DataTooLarge

    with tarfile.open(fileobj=fileobj, mode="r:xz") as tar:
        member = next((m for m in tar.getmembers() if m.name.endswith("types.json")), None)
        if member is None:
            return
        if member.size and member.size > MAX_DECOMPRESSED_BYTES:
            raise DataTooLarge("reference-data types.json exceeded the decompressed ceiling")
        handle = tar.extractfile(member)
        if handle is None:
            return
        data = json.loads(handle.read())
        for tid, entry in data.items():
            pv = entry.get("packaged_volume") if isinstance(entry, dict) else None
            if pv is None:
                continue
            try:
                yield int(tid), float(pv)
            except (ValueError, TypeError):
                continue
