"""P2 — composed demand planning (apps.store.demand + surfaces).

Statistics over zero-filled weekly loss buckets, tagged/untagged attribution,
the four sources, (s, S) reorder math with the no-churn proof, the runout cover
projection, flags, the console/forecast integration, the DemandLine CRUD and
the weekly snapshot beat.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.doctrines.models import Doctrine, DoctrineFit
from apps.identity.models import RoleAssignment
from apps.killboard.models import Killmail
from apps.market.models import MarketLocation
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from apps.store.demand import MIN_WEEKS, demand_for_fits, planning_universe
from apps.store.models import (
    DemandConfig,
    DemandLine,
    DemandSnapshot,
    FitOffer,
    FitStock,
    FitStockEntry,
    ShipyardPolicy,
)
from core import rbac

pytestmark = pytest.mark.django_db

FEROX = 16227


# --- helpers -------------------------------------------------------------------
def _fit(name="Ferox Railgun", hull=FEROX, doctrine=None):
    doctrine = doctrine or Doctrine.objects.create(name=f"Doctrine {name}")
    return DoctrineFit.objects.create(doctrine=doctrine, name=name, ship_type_id=hull)


def _week_start_now():
    today = timezone.localdate()
    monday = today - timedelta(days=today.weekday())
    return timezone.make_aware(datetime.combine(monday, time.min))


_km_seq = iter(range(1, 100000))


def _km(weeks_ago: int, hull=FEROX, fit=None, n=1, **extra):
    """n home-corp victim losses landing squarely in the bucket `weeks_ago`
    complete weeks before the current week (1 = the most recent complete week)."""
    when = _week_start_now() - timedelta(weeks=weeks_ago) + timedelta(hours=12)
    for _ in range(n):
        Killmail.objects.create(
            killmail_id=next(_km_seq), killmail_hash="x", killmail_time=when,
            solar_system_id=30000142, victim_ship_type_id=hull,
            involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
            doctrine_fit=fit, **extra,
        )


def _avail(fit, *, atp=0, incoming=0, on_hand=0, lead_days=7, offer=None):
    return {fit.id: SimpleNamespace(
        atp=atp, incoming=incoming, on_hand=on_hand, lead_days=lead_days, offer=offer,
    )}


def _offer_ns(safety=0, reorder=None, target=None):
    return SimpleNamespace(safety_stock=safety, reorder_point=reorder, target_stock=target)


def _member(django_user_model, char_id, name):
    user = django_user_model.objects.create(username=f"eve:{char_id}", first_name=name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(
        character_id=char_id, user=user, name=name, is_main=True, is_corp_member=True)
    return user


def _officer(django_user_model, char_id, name):
    user = _member(django_user_model, char_id, name)
    RoleAssignment.objects.create(user=user, role=ensure_role(rbac.ROLE_OFFICER))
    return user


# --- statistics ------------------------------------------------------------------
def test_zero_filled_weeks_drive_sigma():
    """Quiet weeks count as zeros — σ over [6, 0×11] is √3, not 0."""
    fit = _fit()
    _km(12, fit=fit, n=6)  # all six losses in the oldest bucket
    d = demand_for_fits([fit], availability=_avail(fit))[fit.id]
    assert d.weeks_observed == 12
    assert d.rate_week_mean == Decimal("0.50")
    assert d.sigma_week == Decimal("1.7321")
    assert d.buckets[0] == 6 and sum(d.buckets[1:]) == 0


def test_band_none_below_min_weeks():
    fit = _fit()
    _km(3, fit=fit, n=2)  # history starts 3 weeks ago
    d = demand_for_fits([fit], availability=_avail(fit))[fit.id]
    assert d.weeks_observed == 3 < MIN_WEEKS
    assert d.sigma_week is None
    assert d.rate_week_hi == d.rate_week_mean  # no fabricated band
    assert "no_history" in d.flags


def test_tagged_and_untagged_attribution_kills_hull_double_count():
    """Tagged losses land on their fit; untagged hull losses split by tagged
    share — two fits on one hull no longer both claim the full hull rate."""
    doctrine = Doctrine.objects.create(name="Ferox Fleet")
    fit_a = _fit("A", doctrine=doctrine)
    fit_b = _fit("B", doctrine=doctrine)
    _km(12, hull=99999)  # anchor history depth on another hull
    _km(2, fit=fit_a, n=8)   # 8 tagged for A
    _km(2, fit=fit_b, n=2)   # 2 tagged for B
    _km(1, n=10)             # 10 untagged on the shared hull

    avail = {**_avail(fit_a), **_avail(fit_b)}
    out = demand_for_fits([fit_a, fit_b], availability=avail)
    a, b = out[fit_a.id], out[fit_b.id]
    total = a.rate_week_mean + b.rate_week_mean
    # 20 losses / 12 weeks ≈ 1.67; per-fit quantization to 0.01 may shave a cent —
    # the point is the split sums to the hull total instead of doubling it.
    assert Decimal("1.66") <= total <= Decimal("1.67")
    assert a.rate_week_mean > b.rate_week_mean  # proportional to tagged share
    assert {s.key for s in a.sources} == {"loss_tagged", "loss_untagged"}
    share = next(s for s in a.sources if s.key == "loss_untagged").detail["share"]
    assert share == 0.8


def test_npc_and_awox_losses_count():
    """The ship is destroyed either way — replacement demand is about the loss,
    not the killer (performance stats filter these; demand must not)."""
    fit = _fit()
    _km(6, fit=fit, is_npc=True)
    _km(5, fit=fit, is_awox=True)
    d = demand_for_fits([fit], availability=_avail(fit))[fit.id]
    assert sum(d.buckets) == 2


def test_untagged_even_split_when_no_tags():
    doctrine = Doctrine.objects.create(name="Ferox Fleet")
    fit_a = _fit("A", doctrine=doctrine)
    fit_b = _fit("B", doctrine=doctrine)
    _km(8, n=6)  # only untagged losses
    out = demand_for_fits([fit_a, fit_b], availability={**_avail(fit_a), **_avail(fit_b)})
    assert out[fit_a.id].rate_week_mean == out[fit_b.id].rate_week_mean


# --- reorder math ----------------------------------------------------------------
def test_reorder_s_and_order_up_to():
    """Known inputs → exact s and S; S strictly above s; replenishing to S clears
    the strict trigger (the no-churn proof); safety adds to the floor."""
    fit = _fit()
    for w in range(1, 13):
        _km(w, fit=fit, n=2)  # constant 2/wk → σ = 0
    offer = _offer_ns(safety=3)
    d = demand_for_fits(
        [fit], availability=_avail(fit, atp=5, lead_days=14, offer=offer)
    )[fit.id]
    assert d.rate_week_mean == Decimal("2.00") and d.sigma_week == Decimal("0")
    # s = ceil(2/7 × 14 + 0) + 3 = 7;  S = max(0, 7 + max(1, ceil(2))) = 9
    assert d.suggested_reorder == 7
    assert d.order_up_to == 9
    assert d.order_up_to > d.suggested_reorder  # S > s always
    # atp=5 < s=7 → would alert (strict); replenishing to S clears it
    assert 5 < d.suggested_reorder
    assert not (d.order_up_to < d.suggested_reorder)
    # qty = S − (atp + incoming) = 9 − 5 = 4
    assert d.suggested_order_qty == 4


def test_incoming_offsets_quantity_never_cover():
    fit = _fit()
    for w in range(1, 13):
        _km(w, fit=fit, n=2)
    d_no = demand_for_fits([fit], availability=_avail(fit, atp=7, incoming=0))[fit.id]
    d_in = demand_for_fits([fit], availability=_avail(fit, atp=7, incoming=5))[fit.id]
    assert d_in.suggested_order_qty == max(0, d_no.suggested_order_qty - 5)
    assert d_in.days_cover == d_no.days_cover  # incoming never buys cover


def test_dated_event_adds_to_order_quantity():
    fit = _fit()
    for w in range(1, 13):
        _km(w, fit=fit, n=2)
    base = demand_for_fits([fit], availability=_avail(fit, atp=0))[fit.id]
    DemandLine.objects.create(fit=fit, quantity=10,
                              needed_by=timezone.localdate() + timedelta(days=5))
    with_line = demand_for_fits([fit], availability=_avail(fit, atp=0))[fit.id]
    assert with_line.suggested_order_qty == base.suggested_order_qty + 10


# --- cover projection --------------------------------------------------------------
def test_cover_without_events_is_exact_division():
    fit = _fit()
    for w in range(1, 13):
        _km(w, fit=fit, n=7)  # 7/wk → 1/day
    d = demand_for_fits([fit], availability=_avail(fit, atp=10))[fit.id]
    assert d.days_cover == Decimal("10.00")


def test_dated_event_pulls_cover_in():
    fit = _fit()
    for w in range(1, 13):
        _km(w, fit=fit, n=7)  # 1/day
    DemandLine.objects.create(fit=fit, quantity=5,
                              needed_by=timezone.localdate() + timedelta(days=3))
    d = demand_for_fits([fit], availability=_avail(fit, atp=10))[fit.id]
    # 3 days of rate + 5 at day 3 leaves 2; 2 more days of rate → day 5.
    assert d.days_cover == Decimal("5.00")
    assert d.days_cover_lo is not None and d.days_cover_lo <= d.days_cover


def test_no_demand_renders_none_not_infinity():
    fit = _fit()
    _km(12, hull=99999)  # corp history exists on another hull; fit has none
    d = demand_for_fits([fit], availability=_avail(fit, atp=5))[fit.id]
    assert d.days_cover is None
    assert d.suggested_reorder is None
    assert d.suggested_order_qty == 0


# --- sources -----------------------------------------------------------------------
def _op(fit, days_ahead, *, min_pilots=20, status="planned", template=None, target_at=...):
    from apps.operations.models import Operation

    op = Operation.objects.create(
        name="CTA", status=status,
        target_at=(timezone.now() + timedelta(days=days_ahead)) if target_at is ... else target_at,
        recurring_template=template,
    )
    op.ship_slots.create(ship_name="Ferox", ship_type_id=fit.ship_type_id,
                         min_pilots=min_pilots, doctrine_fit=fit)
    return op


def test_ops_source_windows_on_target_at():
    fit = _fit()
    _km(12)  # anchor
    _op(fit, 7)                       # inside horizon → counts
    _op(fit, -3)                      # past target_at → nothing
    _op(fit, 5, target_at=None)       # no date → no event
    _op(fit, 90)                      # beyond horizon → nothing
    d = demand_for_fits([fit], availability=_avail(fit))[fit.id]
    ops = [s for s in d.sources if s.key == "ops"]
    assert len(ops) == 1
    assert ops[0].units == Decimal("2.0")  # 20 pilots × 10% attrition
    assert ops[0].detail["attrition_pct"] == 10
    assert len(d.events) == 1


def test_recurring_ops_excluded_by_default_included_when_armed():
    from apps.operations.models import OperationTemplate

    fit = _fit()
    _km(12, hull=99999)
    template = OperationTemplate.objects.create(name="Weekly CTA")
    _op(fit, 7, template=template)
    d = demand_for_fits([fit], availability=_avail(fit))[fit.id]
    assert not [s for s in d.sources if s.key == "ops"]  # already in loss history

    cfg = DemandConfig.active()
    cfg.include_recurring_ops = True
    cfg.save()
    d = demand_for_fits([fit], availability=_avail(fit), config=cfg)[fit.id]
    assert [s for s in d.sources if s.key == "ops"]


def test_template_spawn_carries_doctrine_fit():
    from apps.operations.models import OperationTemplate
    from apps.operations.services import _spawn_from_template

    fit = _fit()
    template = OperationTemplate.objects.create(name="Weekly CTA")
    template.slots.create(ship_name="Ferox", ship_type_id=fit.ship_type_id,
                          min_pilots=15, doctrine_fit=fit)
    op = _spawn_from_template(template, timezone.now() + timedelta(days=3))
    slot = op.ship_slots.get()
    assert slot.doctrine_fit_id == fit.id


def test_manual_lines_open_dated_undated_and_closed():
    fit = _fit()
    _km(12, hull=99999)
    DemandLine.objects.create(fit=fit, quantity=40,
                              needed_by=timezone.localdate() + timedelta(days=10))
    undated = DemandLine.objects.create(fit=fit, quantity=5, campaign_id=999999)  # dangling id OK
    closed = DemandLine.objects.create(fit=fit, quantity=100, status=DemandLine.Status.CLOSED)
    d = demand_for_fits([fit], availability=_avail(fit))[fit.id]
    manual = next(s for s in d.sources if s.key == "manual")
    assert manual.units == Decimal("45")  # closed line ignored
    assert closed.quantity == 100
    horizon_end = timezone.localdate() + timedelta(days=DemandConfig.active().horizon_days)
    assert (horizon_end, Decimal("5"), "manual") in d.events  # undated pins to horizon end
    assert undated.needed_by is None


def test_target_gap_uses_atp_plus_incoming_and_floors():
    fit = _fit()
    _km(12, hull=99999)
    offer = _offer_ns(target=10)
    d = demand_for_fits([fit], availability=_avail(fit, atp=4, incoming=2, offer=offer))[fit.id]
    gap = next(s for s in d.sources if s.key == "target_gap")
    assert gap.units == Decimal("4")  # 10 − (4+2)
    d2 = demand_for_fits([fit], availability=_avail(fit, atp=20, offer=offer))[fit.id]
    assert not [s for s in d2.sources if s.key == "target_gap"]  # floored at 0


# --- flags -------------------------------------------------------------------------
def _stocked(fit, qty=3):
    from apps.store.availability import manifest_hash

    loc = MarketLocation.objects.create(
        name="Staging", location_type=MarketLocation.LocationType.STRUCTURE,
        system_id=30000142)
    return FitStock.objects.create(
        doctrine_fit=fit, location=loc, quantity_on_hand=qty,
        manifest_hash=manifest_hash(fit))


def test_slow_mover_needs_quiet_ledger_too():
    fit = _fit()
    _km(12, hull=99999)  # corp history on another hull, none for this fit
    stock = _stocked(fit)
    d = demand_for_fits([fit], availability=_avail(fit, on_hand=3))[fit.id]
    assert "slow_mover" in d.flags

    # An outbound (consumed) entry inside the window clears the flag…
    entry = FitStockEntry.objects.create(
        stock=stock, kind=FitStockEntry.Kind.CONSUMED, delta=-1, balance_after=2)
    d = demand_for_fits([fit], availability=_avail(fit, on_hand=3))[fit.id]
    assert "slow_mover" not in d.flags

    # …but a mere receipt does NOT (stock arriving is not demand).
    entry.delete()
    FitStockEntry.objects.create(
        stock=stock, kind=FitStockEntry.Kind.RECEIPT, delta=5, balance_after=8)
    d = demand_for_fits([fit], availability=_avail(fit, on_hand=3))[fit.id]
    assert "slow_mover" in d.flags


def test_obsolete_and_upcoming_flags():
    from apps.readiness.models import DoctrineReadinessConfig

    fit = _fit()
    _km(12, hull=99999)
    fit.doctrine.status = Doctrine.Status.RETIRED
    fit.doctrine.save(update_fields=["status"])
    _stocked(fit)
    d = demand_for_fits([fit], availability=_avail(fit, on_hand=3))[fit.id]
    assert "obsolete" in d.flags

    fresh = _fit("Fresh")
    DoctrineReadinessConfig.objects.create(doctrine=fresh.doctrine, is_upcoming=True)
    d = demand_for_fits([fresh], availability=_avail(fresh))[fresh.id]
    assert "upcoming" in d.flags


def test_query_budget(django_assert_max_num_queries):
    fits = [_fit(f"F{i}") for i in range(4)]
    _km(6, fit=fits[0], n=3)
    DemandConfig.active()  # pre-create the singleton (first-ever miss costs one more)
    avail = {}
    for f in fits:
        avail.update(_avail(f))
    with django_assert_max_num_queries(7):
        demand_for_fits(fits, availability=avail)


def test_service_level_scales_the_band():
    """z comes from the fixed map — P95 widens the band over P90, P50 removes it."""
    fit = _fit()
    _km(12, fit=fit, n=6)  # mean 0.5, σ = 1.7321
    cfg = DemandConfig.active()
    bands = {}
    for level in ("0.50", "0.90", "0.95"):
        cfg.service_level = level
        cfg.save()
        d = demand_for_fits([fit], availability=_avail(fit), config=cfg)[fit.id]
        bands[level] = d.rate_week_hi
    assert bands["0.50"] == Decimal("0.50")               # z=0 → hi == mean
    assert bands["0.90"] == Decimal("2.72")               # 0.5 + 1.28 × 1.7321
    assert bands["0.95"] == Decimal("3.36")               # 0.5 + 1.65 × 1.7321
    assert bands["0.95"] > bands["0.90"] > bands["0.50"]


# --- surfaces ----------------------------------------------------------------------
def test_console_universe_includes_retired_with_stock(client, django_user_model):
    officer = _officer(django_user_model, 9901, "QM")
    fit = _fit()
    fit.doctrine.status = Doctrine.Status.RETIRED
    fit.doctrine.save(update_fields=["status"])
    _stocked(fit, qty=2)
    assert fit in planning_universe()
    client.force_login(officer)
    resp = client.get("/store/inventory/")
    assert resp.status_code == 200
    assert any(r["fit"].id == fit.id for r in resp.context["rows"])


def test_console_csv_has_demand_columns(client, django_user_model):
    officer = _officer(django_user_model, 9902, "QM")
    _fit()
    client.force_login(officer)
    resp = client.get("/store/inventory/?export=csv")
    head = resp.content.decode().splitlines()[0]
    for col in ("demand_week_mean", "demand_week_hi", "days_cover_lo",
                "suggested_reorder", "suggested_order_qty", "flags"):
        assert col in head


def test_suggested_alert_gated_by_config(client, django_user_model):
    officer = _officer(django_user_model, 9903, "QM")
    fit = _fit()
    for w in range(1, 13):
        _km(w, fit=fit, n=2)  # demand exists, atp 0 < s
    client.force_login(officer)
    rows = client.get("/store/inventory/").context["rows"]
    row = next(r for r in rows if r["fit"].id == fit.id)
    assert "suggested" not in row["alerts"]  # disarmed by default

    cfg = DemandConfig.active()
    cfg.use_suggested_reorder_alerts = True
    cfg.save()
    rows = client.get("/store/inventory/").context["rows"]
    row = next(r for r in rows if r["fit"].id == fit.id)
    assert "suggested" in row["alerts"]


def test_explicit_reorder_point_beats_suggestion(client, django_user_model):
    """An officer-set reorder point ALWAYS wins: while one exists the suggestion
    never fires — breached (explicit alert) or not (no alert at all)."""
    officer = _officer(django_user_model, 9906, "QM")
    fit = _fit()
    for w in range(1, 13):
        _km(w, fit=fit, n=2)
    offer = FitOffer.objects.create(fit=fit, reorder_point=50)
    cfg = DemandConfig.active()
    cfg.use_suggested_reorder_alerts = True
    cfg.save()
    client.force_login(officer)
    rows = client.get("/store/inventory/").context["rows"]
    row = next(r for r in rows if r["fit"].id == fit.id)
    assert "reorder" in row["alerts"] and "suggested" not in row["alerts"]

    # Unbreached explicit point (atp above it): the suggestion stays silent even
    # though atp is below the suggested s (lead 30 d → s = ceil(2/7 × 30) = 9 > 5).
    _stocked(fit, qty=5)
    offer.reorder_point = 2
    offer.lead_days = 30
    offer.save(update_fields=["reorder_point", "lead_days"])
    rows = client.get("/store/inventory/").context["rows"]
    row = next(r for r in rows if r["fit"].id == fit.id)
    assert row["a"].atp == 5
    assert row["d"].suggested_reorder > 5  # the suggestion alone would fire
    assert "reorder" not in row["alerts"] and "suggested" not in row["alerts"]


def test_demand_line_crud_officer_only_and_audited(client, django_user_model):
    from apps.admin_audit.models import AuditLog

    member = _member(django_user_model, 9904, "Pilot")
    officer = _officer(django_user_model, 9905, "QM")
    fit = _fit()

    client.force_login(member)
    resp = client.post(f"/store/inventory/fit/{fit.pk}/demand-line/", {"quantity": 10})
    assert resp.status_code in (302, 403)
    assert not DemandLine.objects.exists()

    client.force_login(officer)
    resp = client.post(f"/store/inventory/fit/{fit.pk}/demand-line/",
                       {"quantity": 40, "note": "deployment"})
    assert resp.status_code == 302
    line = DemandLine.objects.get()
    assert line.quantity == 40 and line.created_by == officer
    assert AuditLog.objects.filter(action="store.demand_line.add").exists()

    resp = client.post(f"/store/inventory/demand-line/{line.pk}/close/")
    line.refresh_from_db()
    assert line.status == DemandLine.Status.CLOSED and line.closed_by == officer
    assert AuditLog.objects.filter(action="store.demand_line.close").exists()
    # Closing again is a status-guarded no-op.
    client.post(f"/store/inventory/demand-line/{line.pk}/close/")
    assert DemandLine.objects.filter(status=DemandLine.Status.CLOSED).count() == 1


def test_fit_page_renders_demand_panel(client, django_user_model):
    officer = _officer(django_user_model, 9907, "QM")
    fit = _fit()
    for w in range(1, 13):
        _km(w, fit=fit, n=2)
    client.force_login(officer)
    resp = client.get(f"/store/inventory/fit/{fit.pk}/")
    assert resp.status_code == 200
    assert resp.context["d"].rate_week_mean == Decimal("2.00")
    body = resp.content.decode()
    assert "Suggested reorder point" in body or "reorder" in body


def test_fit_page_demand_matches_console_for_shared_hull(client, django_user_model):
    """The fit page computes over the planning universe, so untagged hull losses
    split across sibling fits exactly like the console — a single-fit universe
    would hand the viewed fit 100% and disagree."""
    officer = _officer(django_user_model, 9908, "QM")
    doctrine = Doctrine.objects.create(name="Ferox Fleet")
    fit_a = _fit("A", doctrine=doctrine)
    _fit("B", doctrine=doctrine)  # sibling on the same hull
    _km(12, hull=99999)
    _km(2, n=10)  # untagged on the shared hull → must split, not double

    client.force_login(officer)
    page_d = client.get(f"/store/inventory/fit/{fit_a.pk}/").context["d"]
    console_rows = client.get("/store/inventory/").context["rows"]
    console_d = next(r for r in console_rows if r["fit"].id == fit_a.id)["d"]
    assert page_d.rate_week_mean == console_d.rate_week_mean
    untagged = next(s for s in page_d.sources if s.key == "loss_untagged")
    assert untagged.detail["share"] == 0.5


def test_fit_page_hull_target_chip_and_campaign_name(client, django_user_model):
    from apps.campaigns.models import Campaign
    from apps.stockpile.models import Stockpile, StockpileItem

    officer = _officer(django_user_model, 9909, "QM")
    fit = _fit()
    sp = Stockpile.objects.create(name="Staging Hangar", kind=Stockpile.Kind.CORP)
    StockpileItem.objects.create(stockpile=sp, type_id=fit.ship_type_id,
                                 quantity_current=1, quantity_target=15)
    campaign = Campaign.objects.create(name="Winter Deployment")
    DemandLine.objects.create(fit=fit, quantity=40, campaign_id=campaign.pk)
    DemandLine.objects.create(fit=fit, quantity=5, campaign_id=999999)  # dangling

    client.force_login(officer)
    resp = client.get(f"/store/inventory/fit/{fit.pk}/")
    assert ("Staging Hangar", 15) in resp.context["hull_targets"]
    names = {ln.campaign_id: ln.campaign_name for ln in resp.context["demand_lines"]}
    assert names[campaign.pk] == "Winter Deployment"
    assert names[999999] is None  # dangling id resolves defensively


def test_template_edit_preserves_retired_doctrine_fit_link(client, django_user_model):
    """Editing an unrelated template field must not drop a slot's fit link just
    because its doctrine has since retired."""
    from apps.operations.models import OperationTemplate

    fit = _fit()
    template = OperationTemplate.objects.create(name="Weekly CTA", created_by=None)
    template.slots.create(ship_name="Ferox", ship_type_id=fit.ship_type_id,
                          min_pilots=15, doctrine_fit=fit)
    fit.doctrine.status = Doctrine.Status.RETIRED
    fit.doctrine.save(update_fields=["status"])

    user = django_user_model.objects.create(username="fc", is_superuser=True)
    client.force_login(user)
    resp = client.post(f"/operations/templates/{template.pk}/edit/", {
        "name": "Weekly CTA renamed", "type": "pvp",
        "slot_fit_id": [str(fit.pk)], "slot_ship": ["Ferox"], "slot_role": ["dps"],
        "slot_min": ["15"], "slot_max": ["0"], "slot_priority": ["1"],
    })
    assert resp.status_code == 302
    slot = template.slots.get()
    assert slot.doctrine_fit_id == fit.pk  # link survived the edit


# --- beat --------------------------------------------------------------------------
def test_snapshot_beat_idempotent_and_pruned():
    from apps.store.tasks import snapshot_demand

    fit = _fit()
    _km(4, fit=fit, n=2)
    ShipyardPolicy.active()
    old_week = timezone.localdate() - timedelta(weeks=30)
    DemandSnapshot.objects.create(fit=fit, week_start=old_week)

    assert snapshot_demand() >= 1
    assert snapshot_demand() >= 1  # idempotent upsert
    this_week = timezone.localdate() - timedelta(days=timezone.localdate().weekday())
    assert DemandSnapshot.objects.filter(fit=fit, week_start=this_week).count() == 1
    assert not DemandSnapshot.objects.filter(week_start=old_week).exists()  # pruned


# --- i18n --------------------------------------------------------------------------
def test_flag_labels_translate():
    from django.utils import translation

    from apps.store.demand import FLAG_LABELS

    with translation.override("pt-br"):
        label = str(FLAG_LABELS["slow_mover"])
    assert label  # resolves; translated catalogues carry the localized label
    with translation.override("en"):
        assert str(FLAG_LABELS["slow_mover"]) == "Slow mover"


def test_demand_policy_page_officer_only_and_audited(client, django_user_model):
    """The native console page for DemandConfig — the stock Django admin is
    disabled on the servers, so this is THE editing surface for the knobs."""
    from apps.admin_audit.models import AuditLog

    member = _member(django_user_model, 9910, "Pilot")
    officer = _officer(django_user_model, 9911, "QM")

    client.force_login(member)
    assert client.get("/store/inventory/demand-policy/").status_code == 403

    client.force_login(officer)
    resp = client.get("/store/inventory/demand-policy/")
    assert resp.status_code == 200

    resp = client.post("/store/inventory/demand-policy/", {
        "history_weeks": 16, "horizon_days": 45, "service_level": "0.95",
        "op_attrition_pct": 15, "slow_mover_days": 90,
        "include_untagged_losses": "on", "use_suggested_reorder_alerts": "on",
    })
    assert resp.status_code == 302
    cfg = DemandConfig.active()
    assert cfg.history_weeks == 16 and cfg.service_level == "0.95"
    assert cfg.use_suggested_reorder_alerts is True
    assert cfg.include_recurring_ops is False  # unchecked checkbox stays off
    assert AuditLog.objects.filter(action="store.demand_config_update").exists()

    # Out-of-bounds values are rejected server-side.
    resp = client.post("/store/inventory/demand-policy/", {
        "history_weeks": 2, "horizon_days": 45, "service_level": "0.95",
        "op_attrition_pct": 15, "slow_mover_days": 90,
    })
    assert resp.status_code == 200  # re-rendered with errors
    assert DemandConfig.active().history_weeks == 16  # unchanged
