"""Combat Signatures — WS-3 renderer, payload builder, font chain and mirror-fetch tests.

House style for image tests: assert STRUCTURE (exact dimensions, PNG format, no exception, text
staying inside the box), never pixel equality — the drawn pixels vary with the installed font. The
suite is kept fast by building one rich payload and re-rendering it across the background /
layout / preset sweeps rather than rebuilding per case.
"""
from __future__ import annotations

import datetime
import io
import os
from decimal import Decimal

import pytest
import responses
from django.test import override_settings
from django.utils import timezone

from apps.killboard import signature_assets, signature_stats
from apps.killboard.imagekit import (
    compact_isk,
    has_cjk,
    load_cjk_font,
    load_font,
    split_runs,
    text_width,
    truncate,
)
from apps.killboard.models import (
    CombatSignature,
    Killmail,
    KillmailParticipant,
    PilotTrophy,
    SignatureBackground,
    TrophyDefinition,
)
from apps.killboard.signature_assets import MUTED
from apps.killboard.signature_render import (
    _SUPPORTS,
    PRESETS,
    plan_layout,
    render_placeholder_png,
    render_signature_png,
)
from apps.sde.templatetags.eve import isk as isk_filter
from tests._raffle_utils import HOME_CORP, enrol_pilot

LAYOUTS = ("identity", "tactical", "minimal")
_GEN_AT = datetime.datetime(2026, 7, 22, 15, 0, tzinfo=datetime.UTC)


def _open(content: bytes):
    from PIL import Image

    return Image.open(io.BytesIO(content))


# --------------------------------------------------------------------------- #
#  A rich, hand-built payload (no DB) reused across the render sweeps.
# --------------------------------------------------------------------------- #
def _payload(**over) -> dict:
    payload = {
        "signature_id": 1,
        "background_key": "nebula-emberfront",
        "size_preset": "standard",
        "layout": "identity",
        "theme": "gold",
        "show_timestamp": True,
        "language": "en",
        "generated_at": _GEN_AT,
        "components": [
            "portrait", "pilot_name", "corp", "alliance", "rank_title", "rank_progress",
            "kills", "losses", "isk_destroyed", "isk_efficiency", "trophies_featured",
            "best_kill", "favourite_ship", "activity_period_label", "stats_timestamp",
        ],
        "labels": {
            "kills": "Kills", "losses": "Losses", "isk_destroyed": "ISK destroyed",
            "isk_efficiency": "Efficiency", "rank_title": "Rank", "rank_progress": "Progress",
            "trophies_featured": "Trophies", "best_kill": "Best kill", "favourite_ship": "Top hull",
            "corp": "Corp", "alliance": "Alliance", "stats_timestamp": "Updated",
        },
        "pilot_name": "Tocha Brandi",
        "portrait": {"path": None, "monogram": "TB"},
        "corp": {"id": 98000001, "name": "Forca", "ticker": "FRC", "logo_path": None},
        "alliance": {"id": 99000001, "name": "Alliance", "ticker": "ALLY", "logo_path": None},
        "rank_title": "Immortal of FORCA",
        "rank_progress": {"pct": 62.5, "current_title": "Killer", "next_title": "Marauder",
                          "to_next": 40, "is_maxed": False},
        "kills": 123, "losses": 12,
        "isk_destroyed": {"value": 1.2e9, "text": "1.20B"},
        "isk_efficiency": {"value": 95.6, "text": "95.6%"},
        "best_kill": {"value": 3.4e9, "text": "3.40B", "ship_name": "Rifter"},
        "favourite_ship": {"ship_name": "Jackdaw", "count": 9},
        "trophies_featured": [
            {"tier": "gold", "name": "First Blood", "color": "text-gold"},
            {"tier": "silver", "name": "Solo Ace", "color": "text-cyan"},
        ],
        "activity_period_label": "Last 30 days",
        "stats_timestamp": "22/07/2026 15:00",
    }
    payload.update(over)
    return payload


# --------------------------------------------------------------------------- #
#  Layouts × presets, backgrounds, edge states.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("preset", list(PRESETS))
def test_every_layout_and_preset_renders_exact_dimensions(layout, preset):
    png = render_signature_png(None, _payload(layout=layout, size_preset=preset))
    img = _open(png)
    assert img.format == "PNG"
    assert img.size == PRESETS[preset]


def test_every_committed_background_renders():
    # Every one of the ≥20 shipped designs loads and renders once at the standard preset. One
    # payload is reused; only the background key changes (fast).
    manifest = signature_assets.load_manifest()
    keys = [bg["key"] for bg in manifest["backgrounds"]]
    assert len(keys) >= 20
    for key in keys:
        img = _open(render_signature_png(None, _payload(background_key=key)))
        assert img.size == PRESETS["standard"], key


def test_missing_background_falls_back_to_flat_fill():
    img = _open(render_signature_png(None, _payload(background_key="does-not-exist")))
    assert img.size == PRESETS["standard"]


@pytest.mark.parametrize("name", [
    "X" * 60,                                   # long Latin
    "Длинное Имя Очень Длинного Пилота Флота",   # Cyrillic
    "非常に長い日本語のパイロットの名前です",          # CJK
    "混合 Mixed 名前 Name テスト",                  # mixed scripts
])
def test_long_and_non_latin_names_stay_within_bounds(name):
    img = _open(render_signature_png(None, _payload(pilot_name=name)))
    assert img.size == PRESETS["standard"]


def test_no_kill_pilot_payload_renders():
    payload = _payload(
        components=["pilot_name", "kills", "losses", "isk_destroyed", "rank_title"],
        kills=0, losses=0, isk_destroyed={"value": 0.0, "text": "0"},
    )
    img = _open(render_signature_png(None, payload))
    assert img.size == PRESETS["standard"]


def test_missing_portrait_uses_monogram():
    # A None portrait path must not raise — the monogram fallback is drawn instead.
    payload = _payload(components=["portrait", "pilot_name"], portrait={"path": None, "monogram": "TB"})
    img = _open(render_signature_png(None, payload))
    assert img.size == PRESETS["standard"]


def test_empty_trophies_render():
    payload = _payload(components=["pilot_name", "trophies_featured"], trophies_featured=[])
    img = _open(render_signature_png(None, payload))
    assert img.size == PRESETS["standard"]


@pytest.mark.parametrize("preset", list(PRESETS))
def test_placeholder_renders_per_preset(preset):
    img = _open(render_placeholder_png(preset))
    assert img.format == "PNG"
    assert img.size == PRESETS[preset]


# --------------------------------------------------------------------------- #
#  Visual-review regressions (name/tile fit + footer contrast) — via the trace
#  seam and composed-image luminance, never pixel equality.
# --------------------------------------------------------------------------- #
def _luma(rgb) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def _trace_role(payload, role):
    trace: list = []
    render_signature_png(None, payload, trace=trace)
    return [t for t in trace if t["role"] == role]


@pytest.mark.parametrize("preset", ["standard", "wide", "card"])
def test_twelve_char_name_is_not_truncated_identity(preset):
    # "Tocha Brandi" (12 chars) must render WHOLE in the identity name slot — the drawn string
    # equals the requested one (read from the layout trace, not the pixels).
    names = _trace_role(_payload(size_preset=preset, pilot_name="Tocha Brandi"), "name")
    assert names, "identity layout drew no name"
    assert names[0]["drawn"] == "Tocha Brandi"


def test_hull_name_fits_top_hull_tile_on_card():
    # "Jackdaw" must stay whole in the TOP HULL tile at card size — it shrinks a step rather than
    # ellipsizing.
    cells = _trace_role(
        _payload(size_preset="card", favourite_ship={"ship_name": "Jackdaw", "count": 9}),
        "tile_value:favourite_ship",
    )
    assert cells, "no top-hull tile was drawn"
    assert cells[0]["drawn"] == "Jackdaw"


def test_isk_destroyed_label_fits_standard_identity_tile():
    # "ISK DESTROYED" must fit its tile label WHOLE on identity/standard — labels shrink a step
    # before ellipsizing, exactly like values do. Long translations may still ellipsize at the
    # floor size; the English catalogue must not.
    cells = _trace_role(_payload(), "tile_label:isk_destroyed")
    assert cells, "no isk_destroyed tile label was drawn"
    assert cells[0]["drawn"] == "ISK DESTROYED"


def test_minimal_name_keeps_gutter_before_stat_strip():
    # The minimal layout's name slot must END before the stat strip begins (with a gutter), even
    # for a slot-filling 60-char name — the regression was the slot width being measured from the
    # banner origin so long names ran into the first tile.
    payload = _payload(
        layout="minimal", size_preset="compact", background_key="warp-tunnel",
        components=["pilot_name", "kills", "losses", "isk_destroyed", "activity_period_label"],
        pilot_name="X" * 60,
    )
    names = _trace_role(payload, "name")
    assert names, "minimal layout drew no name"
    strip_x = 468 * 0.42  # the minimal layout's column boundary at the compact preset
    assert names[0]["x"] + names[0]["max_width"] <= strip_x - 8


def test_footer_strip_is_legible_over_background():
    # The opt-in timestamp footer must be readable on the ember background: its scrim keeps the band
    # dark and the text uses the muted-ink tone, giving a large contrast against it.
    payload = _payload()   # identity/standard over nebula-emberfront, show_timestamp on
    img = _open(render_signature_png(None, payload)).convert("RGB")
    w, h = img.size
    band = img.crop((0, h - 16, w, h))
    lums = sorted(_luma(px) for px in band.getdata())
    background_luma = lums[int(len(lums) * 0.2)]      # the scrim behind the text
    assert background_luma <= 60, background_luma      # scrim actually darkened the band
    assert _luma(MUTED) - background_luma >= 60        # strong text-vs-background contrast
    meta = _trace_role(payload, "meta")
    assert meta and tuple(meta[0]["fill"]) == tuple(MUTED)   # muted-ink tone, not the faint tone


# Header components that emit an assertable trace role when drawn (portrait→avatar, corp→corp).
_HEADER_ROLE = {"portrait": "avatar", "corp": "corp"}


@pytest.mark.parametrize("layout", sorted(_SUPPORTS))
def test_supported_header_components_are_drawn(layout):
    # Regression for a whole class: the tactical layout accepted portrait AND corp but drew neither
    # (portrait → blank left zone, corp → missing ticker). Every header component a layout SUPPORTS
    # must actually be drawn when selected — not silently dropped after the builder offered it.
    want = {comp: role for comp, role in _HEADER_ROLE.items() if comp in _SUPPORTS[layout]}
    payload = _payload(
        layout=layout, size_preset="standard",
        components=[*want, "pilot_name", "kills", "losses", "isk_destroyed"],
    )
    trace = []
    render_signature_png(None, payload, trace=trace)
    roles = {t["role"] for t in trace}
    missing = {comp for comp, role in want.items() if role not in roles}
    assert not missing, f"{layout} accepts but does not draw: {missing}"


def test_tactical_without_portrait_still_draws_rank_emblem():
    # The portrait fix must not regress the emblem path: with rank selected and no portrait, the
    # tactical left zone still shows the rank emblem (and no avatar).
    payload = _payload(
        layout="tactical", size_preset="standard",
        components=["pilot_name", "rank_title", "rank_progress", "kills", "losses"],
    )
    trace = []
    render_signature_png(None, payload, trace=trace)
    assert not any(t["role"] == "avatar" for t in trace)


def test_tall_stat_tiles_center_label_and_value():
    # A tall tile (minimal/card single-row strip) must not bottom-anchor the value and leave a gap
    # under the label — the label+value block is centred, so their vertical delta stays close to the
    # label size, never the full tile height.
    payload = _payload(
        layout="minimal", size_preset="card", background_key="warp-tunnel",
        components=["pilot_name", "kills", "losses", "isk_destroyed"],
    )
    trace = []
    render_signature_png(None, payload, trace=trace)
    label = next(t for t in trace if t["role"] == "tile_label:kills")
    value = next(t for t in trace if t["role"] == "tile_value:kills")
    gap = value["y"] - label["y"]
    # The block gap is label_size + a few px of leading — far less than the ~150px card tile height.
    assert 0 < gap <= label["size"] + 12, gap


# --------------------------------------------------------------------------- #
#  plan_layout — capacity + overflow reporting (payload-independent).
# --------------------------------------------------------------------------- #
def test_plan_layout_drops_unsupported_and_overflow():
    plan = plan_layout(
        "minimal", "compact",
        ["portrait", "rank_title", "kills", "losses", "solo_kills", "final_blows", "isk_destroyed"],
    )
    # minimal supports no portrait/rank slot, and its compact stat capacity is 3.
    assert "portrait" in plan["dropped"] and "rank_title" in plan["dropped"]
    assert plan["groups"]["stats"] == ["kills", "losses", "solo_kills"]
    assert "final_blows" in plan["dropped"] and "isk_destroyed" in plan["dropped"]


def test_plan_layout_identity_places_header_rank_stats():
    plan = plan_layout("identity", "card",
                       ["portrait", "pilot_name", "corp", "rank_title", "kills", "losses"])
    assert plan["groups"]["header"] == ["portrait", "pilot_name", "corp"]
    assert plan["groups"]["rank"] == ["rank_title"]
    assert plan["groups"]["stats"] == ["kills", "losses"]
    assert plan["dropped"] == []


# --------------------------------------------------------------------------- #
#  Font chain — DejaVu + Noto CJK per-glyph fallback.
# --------------------------------------------------------------------------- #
def test_compact_isk_parity_with_template_filter():
    for value in (0, 540, 12500, 340_000_000, 1_234_567_890, 9.9e12, -5_000_000, "x", None):
        assert compact_isk(value) == isk_filter(value)


def test_split_runs_degrades_to_primary_without_cjk():
    prim = load_font(20)
    runs = split_runs("日本語 abc", prim, None)   # cjk=None → the degradation path
    assert runs and all(font is prim for _seg, font in runs)


def test_cjk_text_measures_positive():
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    prim = load_font(20)
    assert text_width(draw, "日本語", prim, load_cjk_font(20)) > 0


def test_truncate_adds_ellipsis_and_leaves_short_text():
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    prim = load_font(40)
    out = truncate(draw, "X" * 200, prim, None, 100)
    assert out.endswith("…") and len(out) < 200
    assert truncate(draw, "Hi", prim, None, 1000) == "Hi"


@pytest.mark.skipif(not has_cjk(), reason="Noto Sans CJK not installed in this image")
def test_cjk_glyphs_route_to_cjk_face_when_present():
    prim, cjk = load_font(20), load_cjk_font(20)
    fonts = [font for _seg, font in split_runs("Aa日本語Bb", prim, cjk)]
    assert cjk in fonts               # the CJK run is drawn with the CJK face
    assert any(font is prim for font in fonts)   # Latin stays on the primary face


# --------------------------------------------------------------------------- #
#  Payload builder — privacy, windows, i18n, ISK, trophy filtering.
# --------------------------------------------------------------------------- #
def _make_signature(char, *, config, layout="identity", size_preset="standard", language=""):
    bg, _created = SignatureBackground.objects.get_or_create(
        key="nebula-emberfront", defaults={"name": "Ember", "enabled": True})
    return CombatSignature.objects.create(
        character=char, name="Sig", background=bg, layout=layout,
        size_preset=size_preset, language=language, config=config,
    )


def _base_config(**over):
    cfg = {"components": ["pilot_name"], "period": "30d", "featured_trophy_ids": [],
           "show_timestamp": False, "theme": "gold"}
    cfg.update(over)
    return cfg


@pytest.mark.django_db
def test_payload_carries_only_selected_components(django_user_model):
    _user, char = enrol_pilot(django_user_model, 7001)
    sig = _make_signature(char, config=_base_config(components=["pilot_name", "kills"]))
    payload = signature_stats.build_signature_payload(sig, fetch_assets=False)
    assert payload["pilot_name"] == char.name
    assert "kills" in payload
    # Everything NOT selected is absent — the privacy guarantee.
    for absent in ("losses", "corp", "alliance", "portrait", "rank_title", "rank_progress",
                   "best_kill", "favourite_ship", "trophies_featured", "isk_destroyed"):
        assert absent not in payload, absent


@pytest.mark.django_db
def test_payload_period_windows_map_correctly(django_user_model):
    _user, char = enrol_pilot(django_user_model, 7002)
    # One home kill dated "now" — inside 7d/30d/this-month, outside last-month.
    km = Killmail.objects.create(
        killmail_id=51001, killmail_time=timezone.now(), solar_system_id=30000142,
        victim_ship_type_id=587, total_value=Decimal("1000000"), value_at_kill=Decimal("1000000"),
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.ATTACKER, is_npc=False,
    )
    KillmailParticipant.objects.create(
        killmail=km, role=KillmailParticipant.Role.ATTACKER, seq=0, character_id=char.character_id,
        corporation_id=HOME_CORP, ship_type_id=587, final_blow=True, damage_done=100,
    )
    recent = signature_stats.build_signature_payload(
        _make_signature(char, config=_base_config(components=["kills"], period="7d")),
        fetch_assets=False,
    )
    last_month = signature_stats.build_signature_payload(
        _make_signature(char, config=_base_config(components=["kills"], period="lastmonth")),
        fetch_assets=False,
    )
    assert recent["kills"] == 1
    assert last_month["kills"] == 0


@pytest.mark.django_db
def test_payload_labels_switch_with_language(django_user_model):
    _user, char = enrol_pilot(django_user_model, 7003)
    cfg = _base_config(components=["losses", "activity_period_label"])
    en = signature_stats.build_signature_payload(
        _make_signature(char, config=cfg, language="en"), fetch_assets=False)
    de = signature_stats.build_signature_payload(
        _make_signature(char, config=cfg, language="de"), fetch_assets=False)
    # "Losses" and the window label are already translated in the German catalogue.
    assert en["labels"]["losses"] == "Losses"
    assert de["labels"]["losses"] == "Verluste"
    assert en["activity_period_label"] != de["activity_period_label"]
    assert de["language"] == "de"


@pytest.mark.django_db
def test_payload_isk_text_matches_template_filter(django_user_model):
    _user, char = enrol_pilot(django_user_model, 7004)
    km = Killmail.objects.create(
        killmail_id=51002, killmail_time=timezone.now(), solar_system_id=30000142,
        victim_ship_type_id=587, total_value=Decimal("1234567890"),
        value_at_kill=Decimal("1234567890"),
        involves_home_corp=True, home_corp_role=Killmail.HomeRole.ATTACKER, is_npc=False,
    )
    KillmailParticipant.objects.create(
        killmail=km, role=KillmailParticipant.Role.ATTACKER, seq=0, character_id=char.character_id,
        corporation_id=HOME_CORP, ship_type_id=587, final_blow=True, damage_done=100,
    )
    payload = signature_stats.build_signature_payload(
        _make_signature(char, config=_base_config(components=["isk_destroyed"], period="all")),
        fetch_assets=False,
    )
    text = payload["isk_destroyed"]["text"]
    assert text == isk_filter(payload["isk_destroyed"]["value"])
    assert text == "1.23B"


@pytest.mark.django_db
def test_payload_featured_trophies_filtered_to_earned(django_user_model):
    _user, char = enrol_pilot(django_user_model, 7005)
    earned = TrophyDefinition.objects.create(
        slug="ws3-earned", name="First Blood", tier="gold")
    unearned = TrophyDefinition.objects.create(
        slug="ws3-unearned", name="Solo Ace", tier="silver")
    PilotTrophy.objects.create(character_id=char.character_id, definition=earned,
                               awarded_at=timezone.now())
    payload = signature_stats.build_signature_payload(
        _make_signature(char, config=_base_config(
            components=["trophies_featured"], featured_trophy_ids=[earned.id, unearned.id])),
        fetch_assets=False,
    )
    featured = payload["trophies_featured"]
    assert [t["name"] for t in featured] == ["First Blood"]   # the unearned id drops out
    assert featured[0]["tier"] == "gold"


@pytest.mark.django_db
def test_payload_renders_for_every_language(django_user_model):
    _user, char = enrol_pilot(django_user_model, 7006)
    cfg = _base_config(components=["pilot_name", "kills", "activity_period_label"])
    for code, _label in __import__("django.conf", fromlist=["settings"]).settings.LANGUAGES:
        sig = _make_signature(char, config=cfg, language=code)
        payload = signature_stats.build_signature_payload(sig, fetch_assets=False)
        assert payload["language"] == code
        img = _open(render_signature_png(sig, payload))
        assert img.size == PRESETS["standard"], code


# --------------------------------------------------------------------------- #
#  Portrait / logo mirror fetch (responses-mocked; the pattern from test_image_mirror).
# --------------------------------------------------------------------------- #
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_PORTRAIT_URL = "https://images.evetech.net/characters/2001/portrait"


def _portrait_path(root, size=256, ext="png"):
    return os.path.join(str(root), "characters", "2001", f"portrait-{size}.{ext}")


@responses.activate
def test_mirror_happy_path_writes_atomically(tmp_path):
    responses.add(responses.GET, _PORTRAIT_URL, body=PNG, content_type="image/png", status=200)
    with override_settings(EVE_IMAGE_MIRROR_DIR=str(tmp_path)):
        path = signature_assets.ensure_portrait(2001)
    assert path == _portrait_path(tmp_path)
    assert os.path.exists(path) and open(path, "rb").read() == PNG
    assert not os.path.exists(path + ".tmp")   # atomic: no partial/tmp file left behind


@responses.activate
def test_mirror_rejects_non_image_content_type(tmp_path):
    responses.add(responses.GET, _PORTRAIT_URL, body=b"<html>", content_type="text/html",
                  status=200)
    with override_settings(EVE_IMAGE_MIRROR_DIR=str(tmp_path)):
        assert signature_assets.ensure_portrait(2001) is None
    assert not os.path.exists(_portrait_path(tmp_path))
    assert not os.path.exists(_portrait_path(tmp_path, ext="jpg"))


@responses.activate
def test_mirror_rejects_oversized_upstream(tmp_path):
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (5 * 1024 * 1024 + 1024)
    responses.add(responses.GET, _PORTRAIT_URL, body=big, content_type="image/png", status=200)
    with override_settings(EVE_IMAGE_MIRROR_DIR=str(tmp_path)):
        assert signature_assets.ensure_portrait(2001) is None
    # Aborted before any write — no file and no leftover tmp.
    assert not os.path.exists(_portrait_path(tmp_path))
    assert not os.path.exists(_portrait_path(tmp_path) + ".tmp")


@responses.activate
def test_mirror_timeout_returns_none(tmp_path):
    import requests

    responses.add(responses.GET, _PORTRAIT_URL, body=requests.exceptions.ConnectTimeout())
    with override_settings(EVE_IMAGE_MIRROR_DIR=str(tmp_path)):
        assert signature_assets.ensure_portrait(2001) is None


@responses.activate
def test_mirror_refetch_policy(tmp_path):
    responses.add(responses.GET, _PORTRAIT_URL, body=PNG, content_type="image/png", status=200)
    with override_settings(EVE_IMAGE_MIRROR_DIR=str(tmp_path)):
        first = signature_assets.ensure_portrait(2001)
        assert first and len(responses.calls) == 1
        # A fresh cached copy is reused with no second HTTP call.
        signature_assets.ensure_portrait(2001)
        assert len(responses.calls) == 1
        # Age it past the 7-day window → a refetch happens.
        stale = os.path.getmtime(first) - (8 * 24 * 3600)
        os.utime(first, (stale, stale))
        signature_assets.ensure_portrait(2001)
        assert len(responses.calls) == 2


def test_mirror_rejects_non_numeric_id(tmp_path):
    with override_settings(EVE_IMAGE_MIRROR_DIR=str(tmp_path)):
        assert signature_assets.ensure_portrait("../etc/passwd") is None
        assert signature_assets.ensure_corp_logo(None) is None
