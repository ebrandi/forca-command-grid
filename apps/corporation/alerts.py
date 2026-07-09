"""CORP-3 (roadmap 2.3) — structure fuel / sov-ADM alerts.

Leadership sets the thresholds (``StructureAlertConfig``: fuel days, ADM floor) and
this fires **one deduped officer digest** when a corp structure crosses the low-fuel
line or a sovereignty system drops below the ADM floor — replacing the old hard-coded
3-day / 3.0 flags that nobody was paged about. Reuses the shared ``pingboard.dedup``
machinery (one alert per distinct breach set, resets when everything is back above
threshold, retries on a suppressed emit). One digest, never per-structure.
"""
from __future__ import annotations

_EVENT_KEY = "corporation.infrastructure_alert"
_SIG_KEY = "corp_infra_alert:sig"


def infrastructure_breaches() -> list[dict]:
    """Structures under the fuel threshold + sov systems under the ADM floor.

    Each breach is ``{"key", "detail"}`` — ``key`` feeds the dedup signature (stable per
    structure/system), ``detail`` is the human digest line.
    """
    from apps.corporation.models import CorpStructure, StructureAlertConfig
    from apps.operations.models import SovStructure

    fuel_days, adm_floor = StructureAlertConfig.thresholds()
    breaches: list[dict] = []

    for s in CorpStructure.objects.exclude(fuel_expires__isnull=True).only(
        "structure_id", "name", "fuel_expires"
    ):
        days = s.fuel_days_left
        if days is None or days >= fuel_days:
            continue
        label = s.name or f"Structure {s.structure_id}"
        detail = (
            f"{label} is OUT OF FUEL." if days <= 0
            else f"{label} has {days:.1f} days of fuel left (alert under {fuel_days:g})."
        )
        breaches.append({"key": f"fuel:{s.structure_id}", "detail": detail})

    for sov in SovStructure.objects.only("solar_system_id", "system_name", "adm"):
        if sov.adm < adm_floor:
            label = sov.system_name or f"System {sov.solar_system_id}"
            breaches.append({
                "key": f"adm:{sov.solar_system_id}",
                "detail": f"{label} ADM is {sov.adm:.1f} (alert under {adm_floor:g}).",
            })

    return breaches


def scan_infrastructure_alerts() -> dict:
    """Fire one deduped officer digest when the fuel/ADM breach set changes."""
    from apps.pingboard.dedup import fire_on_change

    breaches = infrastructure_breaches()
    body = ""
    if breaches:
        lines = "\n".join(f"• {b['detail']}" for b in breaches)
        body = (
            "Corp infrastructure needs attention:\n\n" + lines +
            "\n\nRefuel the low structures and shore up the soft-ADM systems. This digest "
            "fires once per distinct set and resets when everything is back above threshold."
        )
    return fire_on_change(
        event_key=_EVENT_KEY, sig_key=_SIG_KEY,
        problems=[b["key"] for b in breaches],
        title="Corp infrastructure alert", body=body,
        source_service="corporation", source_prefix="corp_infra",
    )
