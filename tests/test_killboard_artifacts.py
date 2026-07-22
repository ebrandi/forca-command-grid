"""KB-39 (WS-D6) — shareable kill artifacts.

Covers: the Open Graph / Twitter-card meta on the anonymous-reachable pages (killmail detail,
public battle/campaign permalinks, the public Hall of Fame) and its ABSENCE on member-only pages
(no og:image leak below its tier); the server-rendered kill-card + CV-card PNGs (deterministic
dimensions, content type, cache hit, branding-version invalidation, access gate, throttle); the
biggest-loss-of-the-week weekly pick + officer override; and the OBS overlay (token required,
public-tier feed only, token regeneration).

Card pixel output varies with the installed font, so the card assertions check structure
(dimensions / mode / format / headers), never pixel equality.
"""
from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from apps.admin_audit.models import AuditLog
from apps.killboard import kotw, overlay
from apps.killboard.ingest import ingest_killmail
from apps.killboard.models import (
    BattleReport,
    CombatCampaign,
    KillboardStreamEvent,
    Killmail,
    KillmailParticipant,
)
from apps.market.models import MarketPrice
from core import rbac
from tests._raffle_utils import HOME_CORP, enrol_pilot, make_user

pytestmark = pytest.mark.django_db

RIFTER = 587
AUTOCANNON = 484


# --------------------------------------------------------------------------- #
#  Builders
# --------------------------------------------------------------------------- #
def _seed_prices() -> None:
    MarketPrice.objects.create(
        type_id=RIFTER, location=None, profile=MarketPrice.Profile.JITA_SELL,
        sell_min=Decimal("12000000"),
    )
    MarketPrice.objects.create(
        type_id=AUTOCANNON, location=None, profile=MarketPrice.Profile.JITA_SELL,
        sell_min=Decimal("500000"),
    )


def _kill_body(kid: int = 100001, *, time: str = "2026-06-20T12:00:00Z") -> dict:
    """A home KILL: victim (corp 99) in a Rifter; the home corp lands the final blow."""
    return {
        "killmail_id": kid, "killmail_time": time, "solar_system_id": 30002053,
        "victim": {"character_id": 2001, "corporation_id": 99, "ship_type_id": RIFTER,
                   "damage_taken": 1000,
                   "items": [{"item_type_id": AUTOCANNON, "flag": 27, "quantity_destroyed": 1}]},
        "attackers": [
            {"character_id": 3003, "corporation_id": HOME_CORP, "ship_type_id": RIFTER,
             "final_blow": True, "damage_done": 1000}],
    }


def _make_kill(kid: int = 100001, *, time: str = "2026-06-20T12:00:00Z") -> Killmail:
    _seed_prices()
    ingest_killmail(kid, f"h{kid}", body=_kill_body(kid, time=time))
    return Killmail.objects.get(killmail_id=kid)


def _make_loss(kid: int, cid: int, *, value: str, when: dt.datetime) -> Killmail:
    km = Killmail.objects.create(
        killmail_id=kid, killmail_hash=f"h{kid}", killmail_time=when,
        solar_system_id=30000142, victim_ship_type_id=RIFTER,
        total_value=Decimal(value), value_at_kill=Decimal(value), points=1,
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.VICTIM,
        victim_character_id=cid, victim_corporation_id=HOME_CORP, sec_band="lowsec",
    )
    KillmailParticipant.objects.create(
        killmail=km, role="attacker", seq=0, character_id=9999, corporation_id=99,
        ship_type_id=RIFTER, final_blow=True, damage_done=1000,
    )
    return km


def _week_when(iso_year=2026, iso_week=28) -> dt.datetime:
    start, _end = kotw._week_range(iso_year, iso_week)
    return start + dt.timedelta(days=1)


def _stream_event(km: Killmail, *, needs_srp=True, deviated=True) -> KillboardStreamEvent:
    return KillboardStreamEvent.objects.create(
        killmail=km, killmail_hash=km.killmail_hash, kill_time=km.killmail_time,
        home_role=km.home_corp_role, sec_band=km.sec_band or "lowsec",
        system_id=km.solar_system_id, ship_class="Frigate",
        victim_ship_type_id=km.victim_ship_type_id, victim_character_id=km.victim_character_id,
        victim_corporation_id=km.victim_corporation_id, total_value=km.total_value,
        is_solo=False, is_npc=False, is_awox=False, needs_srp=needs_srp, deviated=deviated,
    )


def _open(content: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(content))


# --------------------------------------------------------------------------- #
#  OG / Twitter meta
# --------------------------------------------------------------------------- #
def test_killmail_detail_has_og_meta_for_anonymous():
    km = _make_kill()
    r = Client().get(f"/killboard/{km.killmail_id}/")
    assert r.status_code == 200
    body = r.content.decode()
    assert 'property="og:title"' in body
    assert 'name="twitter:card" content="summary_large_image"' in body
    # og:image points at the public kill-card endpoint (absolute URL).
    assert f"/killboard/{km.killmail_id}/card.png" in body
    assert 'property="og:image"' in body


def test_base_og_block_empty_by_default():
    # A page that sets no `og` (the public killfeed) must not emit any OG meta — proves the
    # base.html block is an opt-in with no regression to other pages.
    r = Client().get("/killboard/")
    assert r.status_code == 200
    assert 'property="og:title"' not in r.content.decode()


def test_public_battle_slug_has_og_meta_but_private_404s():
    public = BattleReport.objects.create(
        title="Big fight", system_ids=[30002053], start_time=timezone.now(),
        end_time=timezone.now(), is_public=True,
    )
    private = BattleReport.objects.create(
        title="Secret", system_ids=[30002053], start_time=timezone.now(),
        end_time=timezone.now(), is_public=False,
    )
    c = Client()
    r = c.get(f"/killboard/battles/r/{public.slug}/")
    assert r.status_code == 200
    body = r.content.decode()
    assert 'property="og:title"' in body and 'name="twitter:card"' in body
    # A private report's slug 404s anonymously — no meta (or existence) leaks.
    assert c.get(f"/killboard/battles/r/{private.slug}/").status_code == 404


def test_public_campaign_slug_has_og_meta_but_member_404s():
    now = timezone.now()
    public = CombatCampaign.objects.create(
        name="Home defence", visibility=CombatCampaign.Visibility.PUBLIC,
        start_time=now, end_time=now,
    )
    member = CombatCampaign.objects.create(
        name="Members only", visibility=CombatCampaign.Visibility.MEMBER,
        start_time=now, end_time=now,
    )
    c = Client()
    r = c.get(f"/killboard/campaigns/r/{public.slug}/")
    assert r.status_code == 200
    assert 'property="og:title"' in r.content.decode()
    # A member-visibility campaign's public slug 404s anonymously — no meta leak.
    assert c.get(f"/killboard/campaigns/r/{member.slug}/").status_code == 404


def test_member_kotw_hall_emits_no_og_image_to_anonymous():
    # The member KOTW hall stays member-gated (WS-D3): an anonymous viewer is redirected to
    # login and never receives an og:image / kill-card URL.
    r = Client().get("/killboard/kotw/")
    assert r.status_code == 302
    assert "og:image" not in r.content.decode()


# --------------------------------------------------------------------------- #
#  Kill-card PNG
# --------------------------------------------------------------------------- #
def test_kill_card_png_dimensions_and_type():
    km = _make_kill()
    r = Client().get(f"/killboard/{km.killmail_id}/card.png")
    assert r.status_code == 200
    assert r["Content-Type"] == "image/png"
    img = _open(r.content)
    assert img.format == "PNG"
    assert img.size == (1200, 630)


def test_kill_card_gate_mirrors_public_detail():
    # The detail page is public, so the card is public too (anonymous 200); a missing killmail
    # 404s exactly like the detail page.
    km = _make_kill()
    c = Client()
    assert c.get(f"/killboard/{km.killmail_id}/card.png").status_code == 200
    assert c.get("/killboard/99999999/card.png").status_code == 404


def test_kill_card_cache_hit_on_second_call():
    km = _make_kill()
    c = Client()
    first = c.get(f"/killboard/{km.killmail_id}/card.png")
    assert first["X-Card-Cache"] == "miss"
    second = c.get(f"/killboard/{km.killmail_id}/card.png")
    assert second["X-Card-Cache"] == "hit"
    assert first.content == second.content


def test_kill_card_branding_change_invalidates_cache():
    from apps.killboard import branding

    km = _make_kill()
    c = Client()
    assert c.get(f"/killboard/{km.killmail_id}/card.png")["X-Card-Cache"] == "miss"
    assert c.get(f"/killboard/{km.killmail_id}/card.png")["X-Card-Cache"] == "hit"
    # A branding change bumps the card version, so the next render is a fresh miss.
    _clean, errors = branding.set_branding(
        {"display_name": "", "logo_url": "", "accent_color": "#ff3366", "footer_tagline": ""})
    assert not errors
    assert c.get(f"/killboard/{km.killmail_id}/card.png")["X-Card-Cache"] == "miss"


@override_settings(KILLBOARD_CARD_RATE=2)
def test_kill_card_is_throttled():
    km = _make_kill()
    c = Client()
    statuses = [c.get(f"/killboard/{km.killmail_id}/card.png").status_code for _ in range(3)]
    assert statuses == [200, 200, 429]


# --------------------------------------------------------------------------- #
#  Biggest loss of the week + override
# --------------------------------------------------------------------------- #
def test_loss_of_the_week_picks_the_biggest():
    when = _week_when()
    _make_loss(1, 7001, value="1000000000", when=when)
    _make_loss(2, 7002, value="9000000000", when=when)  # the whale loss
    row = kotw.loss_of_the_week(2026, 28)
    assert row is not None
    assert row["killmail"].killmail_id == 2 and row["character_id"] == 7002
    assert row["is_override"] is False


def test_loss_override_survives_autopick(django_user_model):
    when = _week_when()
    _make_loss(1, 7001, value="9000000000", when=when)  # the auto-pick would choose this
    pinned = _make_loss(2, 7002, value="1000000000", when=when)
    officer = make_user(django_user_model, "lo", rbac.ROLE_OFFICER)
    kotw.set_loss_override(2026, 28, pinned, officer)
    row = kotw.loss_of_the_week(2026, 28)
    assert row["killmail"].killmail_id == 2 and row["is_override"] is True


def test_lotw_override_view_is_audited(django_user_model):
    user, _ = enrol_pilot(django_user_model, 7100, roles=(rbac.ROLE_OFFICER, rbac.ROLE_MEMBER))
    km = _make_loss(1, 7100, value="1000000000", when=_week_when())
    c = Client()
    c.force_login(user)
    r = c.post("/killboard/kotw/loss-override/",
               {"iso_year": "2026", "iso_week": "28", "killmail_id": str(km.killmail_id)})
    assert r.status_code == 302
    row = kotw.loss_of_the_week(2026, 28)
    assert row["killmail"].killmail_id == km.killmail_id and row["is_override"] is True
    assert AuditLog.objects.filter(action="killboard.lotw_override").exists()


def test_recent_losses_lists_completed_weeks():
    # A loss in the most recently completed ISO week shows in the hall list.
    iso_year, iso_week = kotw.last_completed_iso_week()
    when = _week_when(iso_year, iso_week)
    _make_loss(50, 8001, value="5000000000", when=when)
    losses = kotw.recent_losses()
    assert any(r["killmail"].killmail_id == 50 for r in losses)


# --------------------------------------------------------------------------- #
#  Public Hall of Fame
# --------------------------------------------------------------------------- #
def test_hall_is_public_with_og_meta():
    when = _week_when()
    _make_loss(60, 8100, value="7000000000", when=when)
    r = Client().get("/killboard/hall/")
    assert r.status_code == 200
    body = r.content.decode()
    assert 'property="og:title"' in body
    assert "Hall of Fame" in body


# --------------------------------------------------------------------------- #
#  CV card (member-gated)
# --------------------------------------------------------------------------- #
def test_cv_card_is_member_gated(django_user_model):
    # Anonymous → login redirect; a logged-in non-member → 404 (no card leak).
    anon = Client().get("/killboard/pilot/9001/cv/card.png")
    assert anon.status_code == 302
    outsider = make_user(django_user_model, "outsider")
    c = Client()
    c.force_login(outsider)
    assert c.get("/killboard/pilot/9001/cv/card.png").status_code == 404


def test_cv_card_renders_png_for_member(django_user_model):
    user, _ = enrol_pilot(django_user_model, 9002)
    c = Client()
    c.force_login(user)
    r = c.get("/killboard/pilot/9002/cv/card.png")
    assert r.status_code == 200 and r["Content-Type"] == "image/png"
    img = _open(r.content)
    assert img.format == "PNG" and img.size == (1200, 630)


# --------------------------------------------------------------------------- #
#  OBS overlay
# --------------------------------------------------------------------------- #
def test_overlay_page_requires_valid_token():
    overlay.regenerate_token()
    good = overlay.get_token()
    c = Client()
    assert c.get("/killboard/overlay/").status_code == 404            # no token
    assert c.get("/killboard/overlay/?token=wrong").status_code == 404
    ok = c.get(f"/killboard/overlay/?token={good}")
    assert ok.status_code == 200
    assert b"overlay" in ok.content.lower()


def test_overlay_feed_requires_token_and_is_public_tier():
    overlay.regenerate_token()
    good = overlay.get_token()
    km = _make_kill()
    _stream_event(km, needs_srp=True, deviated=True)
    c = Client()
    assert c.get("/killboard/overlay/feed/?token=wrong").status_code == 404
    r = c.get(f"/killboard/overlay/feed/?token={good}&after_seq=0")
    assert r.status_code == 200
    data = r.json()
    assert data["events"], "expected the public-tier event to be delivered"
    flags = data["events"][0]["flags"]
    # Member-only flags must NEVER appear in the anonymous overlay payload.
    assert "needs_srp" not in flags and "deviated" not in flags
    assert "solo" in flags and "npc" in flags


def test_overlay_regenerate_invalidates_old_token(django_user_model):
    overlay.regenerate_token()
    old = overlay.get_token()
    director, _ = enrol_pilot(django_user_model, 9500,
                              roles=(rbac.ROLE_DIRECTOR, rbac.ROLE_MEMBER))
    c = Client()
    c.force_login(director)
    r = c.post("/killboard/setup/overlay/", {"action": "regenerate"})
    assert r.status_code == 302
    new = overlay.get_token()
    assert new and new != old
    assert not overlay.token_valid(old) and overlay.token_valid(new)


def test_overlay_threshold_saved_by_director(django_user_model):
    director, _ = enrol_pilot(django_user_model, 9600,
                              roles=(rbac.ROLE_DIRECTOR, rbac.ROLE_MEMBER))
    c = Client()
    c.force_login(director)
    r = c.post("/killboard/setup/overlay/", {"threshold": "2500000000"})
    assert r.status_code == 302
    assert overlay.big_kill_threshold() == 2_500_000_000
