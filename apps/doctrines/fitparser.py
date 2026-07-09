"""EFT fit import/export.

EFT format:
    [ShipName, Fit name]
    Module Name
    Module Name, Charge Name
    Drone Name x5
    <blank lines separate sections>
"""
from __future__ import annotations

import re

from apps.sde.models import SdeType

_QTY_RE = re.compile(r"\sx(\d+)\s*$", re.IGNORECASE)


def _resolve(name: str) -> int | None:
    t = SdeType.objects.filter(name__iexact=name.strip()).values_list("type_id", flat=True).first()
    return t


def parse_eft(text: str) -> dict:
    """Parse EFT text into {ship_name, ship_type_id, fit_name, modules, unresolved}."""
    lines = [ln.rstrip() for ln in text.strip().splitlines()]
    if not lines or not lines[0].startswith("["):
        raise ValueError("EFT must start with '[ShipName, Fit name]'")

    header = lines[0].strip()[1:-1]
    ship_name, _, fit_name = header.partition(",")
    ship_name = ship_name.strip()
    fit_name = fit_name.strip() or f"{ship_name} fit"

    modules: list[dict] = []
    unresolved: list[str] = []
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        qty = 1
        m = _QTY_RE.search(line)
        if m:
            qty = int(m.group(1))
            line = _QTY_RE.sub("", line).strip()
        item_name = line.split(",")[0].strip()
        if not item_name or item_name.startswith("[") or item_name.lower() in {"empty"}:
            continue
        type_id = _resolve(item_name)
        if type_id is None:
            unresolved.append(item_name)
            continue
        modules.append({"type_id": type_id, "quantity": qty, "name": item_name})

    return {
        "ship_name": ship_name,
        "ship_type_id": _resolve(ship_name),
        "fit_name": fit_name,
        "modules": modules,
        "unresolved": unresolved,
    }


def export_eft(fit) -> str:
    """Reconstruct EFT text from a stored DoctrineFit."""
    ship_name = (
        SdeType.objects.filter(type_id=fit.ship_type_id).values_list("name", flat=True).first()
        or f"TypeID:{fit.ship_type_id}"
    )
    lines = [f"[{ship_name}, {fit.name}]"]
    for module in fit.modules or []:
        name = module.get("name") or (
            SdeType.objects.filter(type_id=module.get("type_id"))
            .values_list("name", flat=True)
            .first()
            or f"TypeID:{module.get('type_id')}"
        )
        qty = module.get("quantity", 1)
        lines.append(f"{name} x{qty}" if qty > 1 else name)
    return "\n".join(lines)
