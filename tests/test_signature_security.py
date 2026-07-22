"""Combat Signatures — WS-9 ADVERSARIAL security + hardening suite.

This is the attacker's pass: it deliberately probes the seams the per-workstream suites do not —
in-flight render races, force-render flooding, forged ownership, quota TOCTOU, config tampering,
hostile Unicode, upstream (mirror) abuse, the anonymous public endpoint's edges, and the admin
surface. Where a hypothesis is already covered by an existing test, the note here says so rather
than duplicating it; where a real weakness was found, the fix lives in the signature modules and
the regression is proven below.

Numbered ``Hn`` markers map 1:1 to the WS-9 mission hypotheses. House style is inherited from the
sibling suites: hermetic (per-test ``MEDIA_ROOT``, mirror disabled, cache cleared, ``.delay``
patched), Postgres-backed (so ``SELECT … FOR UPDATE`` is real), and image assertions check PNG
structure + exact preset dimensions, never pixels.
"""
from __future__ import annotations

import io
import os

import pytest
import requests
import responses
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import Client, RequestFactory
from django.test.utils import CaptureQueriesContext, override_settings
from django.urls import reverse
from django.utils import timezone
from PIL import Image

from apps.killboard import (
    signature_assets,
    signature_pipeline,
    signature_public,
    signature_stats,
    signatures,
    tasks,
)
from apps.killboard.models import (
    CombatSignature,
    CombatSignatureSettings,
    PilotTrophy,
    SignatureBackground,
    TrophyDefinition,
)
from apps.killboard.signature_render import PRESETS, render_signature_png
from core import rbac
from tests._raffle_utils import enrol_pilot

pytestmark = pytest.mark.django_db

ACTIVE = CombatSignature.Status.ACTIVE
DISABLED = CombatSignature.Status.DISABLED
FROZEN = CombatSignature.Status.FROZEN
LIVE = CombatSignature.Mode.LIVE
SNAPSHOT = CombatSignature.Mode.SNAPSHOT
OK = CombatSignature.RenderStatus.OK
PENDING = CombatSignature.RenderStatus.PENDING


# --------------------------------------------------------------------------- #
#  Environment + builders (mirror the sibling suites)
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def env(settings, tmp_path, monkeypatch):
    """Per-test MEDIA_ROOT, no mirror network, clean cache, and a ``.delay`` recorder (returned)."""
    settings.MEDIA_ROOT = str(tmp_path)
    settings.EVE_IMAGE_MIRROR_DIR = ""
    cache.clear()
    calls: list[int] = []
    monkeypatch.setattr(
        tasks.signature_render_task, "delay",
        lambda signature_id, *a, **k: calls.append(signature_id),
    )
    return calls


def _bg(key="nebula-emberfront") -> SignatureBackground:
    bg, _created = SignatureBackground.objects.get_or_create(
        key=key, defaults={"name": "Ember", "enabled": True}
    )
    return bg


def _enable(**over) -> CombatSignatureSettings:
    cfg = CombatSignatureSettings.load()
    cfg.enabled = True
    for key, value in over.items():
        setattr(cfg, key, value)
    cfg.save()
    return cfg


def _config(components=("pilot_name", "kills"), **over) -> dict:
    cfg = {
        "components": list(components), "period": "30d", "featured_trophy_ids": [],
        "show_timestamp": False, "theme": "gold",
    }
    cfg.update(over)
    return cfg


def _sig(char, *, status=ACTIVE, mode=LIVE, size_preset="standard", layout="identity",
         components=("pilot_name", "kills"), name="Sig", config_version=1) -> CombatSignature:
    snap = timezone.now() if mode == SNAPSHOT else None
    return CombatSignature.objects.create(
        character=char, name=name, background=_bg(), layout=layout, size_preset=size_preset,
        mode=mode, status=status, config_version=config_version, snapshot_taken_at=snap,
        config=_config(components),
    )


def _form_data(bg, **over) -> dict:
    data = {
        "name": "My Sig", "layout": "identity", "size_preset": "standard",
        "background": str(bg.id), "language": "", "period": "30d", "theme": "gold",
        "components": ["pilot_name", "kills"],
    }
    data.update(over)
    return data


def _payload(name, *, preset="standard", layout="identity",
             components=("portrait", "pilot_name", "corp", "kills")) -> dict:
    """A hand-built render payload with a caller-controlled pilot name (the renderer is pure and
    reads only the payload — its ``signature`` argument is unused, so tests pass ``None``)."""
    return {
        "signature_id": 1, "background_key": "", "size_preset": preset, "layout": layout,
        "theme": "gold", "components": list(components), "show_timestamp": False, "language": "en",
        "generated_at": timezone.now(), "labels": {"kills": "Kills", "corp": "Corp"},
        "portrait": {"path": None, "monogram": (name[:1] or "?")},
        "pilot_name": name, "corp": {"ticker": "ABC", "name": "CorpName"}, "kills": 5,
    }


def _is_png(data: bytes, preset: str = "standard") -> bool:
    img = Image.open(io.BytesIO(data))
    return img.format == "PNG" and img.size == PRESETS[preset]


def _portrait_url(cid, size=256) -> str:
    return f"https://images.evetech.net/characters/{cid}/portrait?size={size}"


# =========================================================================== #
#  H1 — Rotate/edit/disable DURING an in-flight render (the stale-write race).
#  Fix: render_one re-reads token/config/status under select_for_update before
#  the write and drops stale bytes (signature_pipeline.render_one).
# =========================================================================== #
def _render_with_side_effect(monkeypatch, side_effect):
    """Patch the renderer so ``side_effect()`` fires exactly ONCE — after the (real) PNG is produced
    but BEFORE render_one's locked write — the exact window an attacker/owner would rotate or edit
    in. Firing once lets a follow-up recovery render (force_render) complete normally."""
    real = signature_pipeline.render_signature_png
    fired = {"done": False}

    def wrapped(sig, payload, **kw):
        png = real(sig, payload, **kw)
        if not fired["done"]:
            fired["done"] = True
            side_effect()
        return png

    monkeypatch.setattr(signature_pipeline, "render_signature_png", wrapped)


def test_h1_rotate_mid_render_does_not_resurrect_old_token(django_user_model, monkeypatch):
    _enable()
    user, char = enrol_pilot(django_user_model, 90101)
    sig = _sig(char)

    # A first good render establishes the artifact at the ORIGINAL token.
    assert signature_pipeline.render_one(sig.pk) == "rendered"
    old_token = CombatSignature.objects.get(pk=sig.pk).public_token
    old_path = signatures.artifact_path(old_token)
    assert os.path.exists(old_path)

    # Race a second render against a token rotation injected mid-render.
    cache.delete(signature_pipeline._debounce_key(CombatSignature.objects.get(pk=sig.pk)))
    _render_with_side_effect(
        monkeypatch,
        lambda: signatures.rotate_token(user, CombatSignature.objects.get(pk=sig.pk)),
    )
    result = signature_pipeline.render_one(sig.pk)

    assert result == "skipped_rotated"           # the stale bytes were dropped, not written
    sig.refresh_from_db()
    assert sig.public_token != old_token          # rotation took effect
    assert not os.path.exists(old_path)           # the rotated-away URL stays dead (404 by design)
    assert sig.dirty is True                       # left queued for a fresh render at the new token

    # Recovery: the render the rotate action enqueues produces the NEW token's file, not the old.
    assert signature_pipeline.force_render(sig.pk) == "rendered"
    assert os.path.exists(signatures.artifact_path(sig.public_token))
    assert not os.path.exists(old_path)


def test_h1_disable_mid_render_does_not_write_disabled_token(django_user_model, monkeypatch):
    _enable()
    user, char = enrol_pilot(django_user_model, 90102)
    sig = _sig(char)
    token = sig.public_token

    _render_with_side_effect(
        monkeypatch, lambda: signatures.disable(user, CombatSignature.objects.get(pk=sig.pk))
    )
    result = signature_pipeline.render_one(sig.pk)

    assert result == "skipped_rotated"            # disabled mid-render → bytes dropped
    sig.refresh_from_db()
    assert sig.status == DISABLED
    assert not os.path.exists(signatures.artifact_path(token))  # never resurrected


def test_h1_edit_mid_render_does_not_clobber_new_config(django_user_model, monkeypatch):
    _enable()
    user, char = enrol_pilot(django_user_model, 90103)
    sig = _sig(char)

    def edit():
        signatures.update_signature(
            user, CombatSignature.objects.get(pk=sig.pk), name="Renamed", background=_bg(),
            layout="identity", size_preset="standard",
            config=_config(("pilot_name", "kills", "losses")),
        )

    _render_with_side_effect(monkeypatch, edit)
    result = signature_pipeline.render_one(sig.pk)

    assert result == "skipped_rotated"            # config_version moved under us → stale, dropped
    sig.refresh_from_db()
    assert sig.config_version == 2                 # the edit stands
    assert sig.dirty is True and sig.render_status == PENDING  # not falsely marked OK/clean


# =========================================================================== #
#  H2 — Unbounded manual regeneration (force-render flooding).
#  Fix: per-user SIGNATURE_REGENERATE_RATE throttle in signature_views.
# =========================================================================== #
def test_h2_regenerate_is_rate_limited_per_user(client, django_user_model, settings, env):
    settings.SIGNATURE_REGENERATE_RATE = 2
    _enable()
    user, char = enrol_pilot(django_user_model, 90201)
    client.force_login(user)
    sig = _sig(char)
    url = f"/killboard/signatures/{sig.pk}/regenerate/"

    for _ in range(2):
        assert client.post(url).status_code == 302
    # The 3rd within the window is refused: no render is enqueued and the pilot is told to wait.
    resp = client.post(url, follow=True)
    assert resp.status_code == 200
    assert env == [sig.pk, sig.pk]                 # exactly two force-renders enqueued, not three
    assert b"regenerating too quickly" in resp.content


def test_h2_regenerate_throttle_is_per_user_not_global(client, django_user_model, settings, env):
    settings.SIGNATURE_REGENERATE_RATE = 1
    _enable()
    u1, c1 = enrol_pilot(django_user_model, 90202)
    u2, c2 = enrol_pilot(django_user_model, 90203)
    s1, s2 = _sig(c1), _sig(c2)

    client.force_login(u1)
    assert client.post(f"/killboard/signatures/{s1.pk}/regenerate/").status_code == 302
    assert client.post(f"/killboard/signatures/{s1.pk}/regenerate/").status_code == 302  # u1 blocked
    client.force_login(u2)
    assert client.post(f"/killboard/signatures/{s2.pk}/regenerate/").status_code == 302  # u2 fresh
    assert env == [s1.pk, s2.pk]                    # u1's 2nd was throttled; u2's 1st went through


# =========================================================================== #
#  H3 — Featured-trophy ownership (forged ids). Two independent layers must hold.
# =========================================================================== #
def test_h3_forged_trophy_id_dropped_at_view_layer(client, django_user_model, env):
    _enable()
    user, char = enrol_pilot(django_user_model, 90301)
    _victim_u, victim = enrol_pilot(django_user_model, 90302)
    victim_trophy = TrophyDefinition.objects.create(slug="h3v", name="Solo Ace", tier="gold")
    PilotTrophy.objects.create(character_id=victim.character_id, definition=victim_trophy,
                               awarded_at=timezone.now())
    client.force_login(user)

    resp = client.post("/killboard/signatures/new/", _form_data(
        _bg(), components=["pilot_name", "trophies_featured"],
        featured_trophy_ids=[str(victim_trophy.id), "999999"],  # someone else's + nonexistent
    ))
    assert resp.status_code == 302
    sig = CombatSignature.objects.get(character=char)
    assert sig.config["featured_trophy_ids"] == []   # both forged ids filtered before the row saved


def test_h3_forged_trophy_id_dropped_at_render_layer(django_user_model):
    _enable()
    _u, char = enrol_pilot(django_user_model, 90303)
    _victim_u, victim = enrol_pilot(django_user_model, 90304)
    victim_trophy = TrophyDefinition.objects.create(slug="h3r", name="Solo Ace", tier="gold")
    PilotTrophy.objects.create(character_id=victim.character_id, definition=victim_trophy,
                               awarded_at=timezone.now())

    # Even if a forged id reached the stored config, the payload builder re-filters to EARNED.
    sig = _sig(char, components=("trophies_featured",))
    sig.config["featured_trophy_ids"] = [victim_trophy.id]
    sig.save(update_fields=["config"])
    payload = signature_stats.build_signature_payload(sig, fetch_assets=False)
    assert payload["trophies_featured"] == []        # never surfaces another pilot's trophy


# =========================================================================== #
#  H4 — Quota race (count-then-insert TOCTOU).
#  Fix: check_quota(lock=True) takes SELECT … FOR UPDATE on the owner row.
# =========================================================================== #
def test_h4_create_serialises_on_owner_row_lock(django_user_model):
    _enable()
    user, _char = enrol_pilot(django_user_model, 90401)
    with CaptureQueriesContext(connection) as ctx:
        signatures.create_signature(
            user, name="A", background=_bg(), layout="identity", size_preset="standard",
            config=_config(),
        )
    sql = " ".join(q["sql"].lower() for q in ctx.captured_queries)
    assert "for update" in sql                       # the serialisation lock is really emitted


def test_h4_duplicate_and_enable_also_lock(django_user_model):
    _enable()
    user, char = enrol_pilot(django_user_model, 90402)
    sig = _sig(char)
    with CaptureQueriesContext(connection) as ctx:
        signatures.duplicate_signature(user, sig)
    assert "for update" in " ".join(q["sql"].lower() for q in ctx.captured_queries)

    signatures.disable(user, sig)
    with CaptureQueriesContext(connection) as ctx:
        signatures.enable(user, sig)
    assert "for update" in " ".join(q["sql"].lower() for q in ctx.captured_queries)


def test_h4_quota_ceiling_still_holds(django_user_model):
    _enable(max_active_per_pilot=1)
    user, _char = enrol_pilot(django_user_model, 90403)
    signatures.create_signature(user, name="A", background=_bg(), layout="identity",
                                size_preset="standard", config=_config())
    with pytest.raises(ValidationError):
        signatures.create_signature(user, name="B", background=_bg(), layout="identity",
                                    size_preset="standard", config=_config())


# =========================================================================== #
#  H5 — Config tampering matrix. (Most rejections are proven in
#  test_signatures_foundation; these add the ADVERSARIAL angles it omits.)
# =========================================================================== #
def test_h5_valid_but_unsupported_component_is_accepted_then_dropped(django_user_model):
    cfg = _enable()
    # 'portrait' is a valid id but the minimal layout has no header slot for it: validation ACCEPTS
    # it (it is a real component), and it is dropped only at render — no crash, no leak.
    clean = signatures.validate_config(
        {"components": ["portrait", "pilot_name", "kills"], "period": "30d",
         "featured_trophy_ids": [], "show_timestamp": False, "theme": "gold"},
        settings=cfg, background=_bg(), layout="minimal", size_preset="standard",
    )
    assert "portrait" in clean["components"]
    from apps.killboard.signature_render import plan_layout
    assert "portrait" in plan_layout("minimal", "standard", clean["components"])["dropped"]


def test_h5_oversized_name_is_a_form_error(client, django_user_model):
    _enable()
    user, _char = enrol_pilot(django_user_model, 90501)
    client.force_login(user)
    resp = client.post("/killboard/signatures/new/", _form_data(_bg(), name="x" * 5000))
    assert resp.status_code == 400
    assert not CombatSignature.objects.filter(character_id=90501).exists()


def test_h5_too_many_components_is_a_form_error(client, django_user_model):
    _enable()
    user, _char = enrol_pilot(django_user_model, 90502)
    client.force_login(user)
    thirteen = list(signatures.COMPONENTS)[:13]
    assert len(thirteen) == 13
    resp = client.post("/killboard/signatures/new/", _form_data(_bg(), components=thirteen))
    assert resp.status_code == 400
    assert not CombatSignature.objects.filter(character_id=90502).exists()


def test_h5_negative_trophy_id_is_dropped(client, django_user_model):
    _enable()
    user, char = enrol_pilot(django_user_model, 90503)
    client.force_login(user)
    resp = client.post("/killboard/signatures/new/", _form_data(
        _bg(), components=["pilot_name", "trophies_featured"], featured_trophy_ids=["-5"],
    ))
    assert resp.status_code == 302
    assert CombatSignature.objects.get(character=char).config["featured_trophy_ids"] == []


def test_h5_forged_disabled_background_is_rejected(client, django_user_model):
    _enable()
    user, _char = enrol_pilot(django_user_model, 90504)
    disabled = SignatureBackground.objects.create(key="h5-off", name="Off", enabled=False)
    client.force_login(user)
    resp = client.post("/killboard/signatures/new/", _form_data(_bg(), background=str(disabled.id)))
    assert resp.status_code == 400                   # both the view filter and validate_config guard
    assert not CombatSignature.objects.filter(character_id=90504).exists()


def test_h5_non_list_and_nested_components_rejected():
    cfg = _enable()
    for bad in ("not-a-list", {"x": 1}, [["nested"]], [{"id": "portrait"}]):
        with pytest.raises(ValidationError):
            signatures.validate_config(
                {"components": bad, "period": "30d", "featured_trophy_ids": [],
                 "show_timestamp": False, "theme": "gold"},
                settings=cfg, background=_bg(), layout="identity", size_preset="standard",
            )


# =========================================================================== #
#  H6 — Unicode / render abuse. The renderer must never raise and must keep
#  exact preset dimensions; sanitisation must not yield an empty/hostile name.
# =========================================================================== #
_HOSTILE_NAMES = [
    "مرحبا بالعالم يا صديقي",          # RTL Arabic (unsupported glyphs → tofu, never a crash)
    "a" + "́" * 60,                # combining-acute flood
    "Z" + "͐" * 40,                # combining-above flood (zalgo)
    "​" * 30 + "Visible",          # zero-width flood ahead of visible text
    "テスト艦隊🎉😀🔥",                  # CJK + emoji mix
    'Evil "><img src=x> name',          # markup-looking (renderer draws it as literal text)
]


@pytest.mark.parametrize("name", _HOSTILE_NAMES)
@pytest.mark.parametrize("preset", list(PRESETS))
@pytest.mark.parametrize("layout", ["identity", "tactical", "minimal"])
def test_h6_hostile_names_render_exact_dims(name, preset, layout):
    png = render_signature_png(None, _payload(name, preset=preset, layout=layout))
    assert _is_png(png, preset)                      # never raises; dimensions are exactly the preset


def test_h6_hostile_character_name_survives_real_payload(django_user_model):
    _enable()
    _u, char = enrol_pilot(django_user_model, 90601, name="Z" + "͐" * 40 + "🔥")
    sig = _sig(char, components=("portrait", "pilot_name", "corp", "kills"))
    payload = signature_stats.build_signature_payload(sig, fetch_assets=False)
    assert _is_png(render_signature_png(sig, payload), "standard")


def test_h6_zero_width_only_name_is_rejected():
    # A name that is nothing but zero-width / bidi format chars must not sanitise to an empty label.
    with pytest.raises(ValidationError):
        signatures.sanitize_name("​‌‍⁦⁩" * 4)


def test_h6_zalgo_and_markup_name_escaped_in_embed_html():
    sig = _sig_for_alt('Evil "><img src=x> Ź')
    snippets = signatures.embed_snippets("https://forca.club/s/tok01234567890abcd.png",
                                         signatures.signature_alt_text(sig))
    html = snippets["html"]
    assert "<img src=x>" not in html                 # angle brackets neutralised
    assert "&lt;img" in html and "&quot;" in html    # attribute-escaped inside alt=""


def _sig_for_alt(name):
    class _S:
        pass
    s = _S()
    s.name = name
    return s


# =========================================================================== #
#  H7 — Upstream (mirror) abuse. The worker-side fetcher is the only network.
# =========================================================================== #
@responses.activate
def test_h7_rejects_svg_content_type(tmp_path):
    responses.add(responses.GET, _portrait_url(70001), body=b"<svg onload=alert(1)/>",
                  content_type="image/svg+xml", status=200)
    with override_settings(EVE_IMAGE_MIRROR_DIR=str(tmp_path)):
        assert signature_assets.ensure_portrait(70001) is None   # SVG (a script vector) refused
    assert not list(tmp_path.rglob("*.png")) and not list(tmp_path.rglob("*.svg"))


@responses.activate
def test_h7_stream_cap_enforced_not_content_length(tmp_path):
    # 6 MB body with a LYING small Content-Length: the cap is on the STREAM, so the header is moot.
    oversized = b"\x89PNG\r\n\x1a\n" + b"\x00" * (6 * 1024 * 1024)
    responses.add(responses.GET, _portrait_url(70002), body=oversized, content_type="image/png",
                  status=200, headers={"Content-Length": "1024"})
    with override_settings(EVE_IMAGE_MIRROR_DIR=str(tmp_path)):
        assert signature_assets.ensure_portrait(70002) is None
    assert not list(tmp_path.rglob("*"))             # aborted BEFORE any file (incl. .tmp) is written


@responses.activate
def test_h7_redirect_to_other_host_is_not_followed(tmp_path):
    internal = "http://169.254.169.254/latest/meta-data/"
    responses.add(responses.GET, _portrait_url(70003), status=302, headers={"Location": internal})
    responses.add(responses.GET, internal, body=b"\x89PNG", content_type="image/png", status=200)
    with override_settings(EVE_IMAGE_MIRROR_DIR=str(tmp_path)):
        assert signature_assets.ensure_portrait(70003) is None   # 30x → non-200 → None, not followed
    assert len(responses.calls) == 1                 # the internal host was never requested
    assert responses.calls[0].request.url == _portrait_url(70003)


def test_h7_connection_reset_mid_stream_leaves_no_file(tmp_path, monkeypatch):
    class _Resp:
        status_code = 200
        headers = {"Content-Type": "image/png"}

        def iter_content(self, _n):
            yield b"\x89PNG\r\n"
            raise requests.exceptions.ConnectionError("peer reset mid-stream")

        def close(self):
            pass

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp())
    with override_settings(EVE_IMAGE_MIRROR_DIR=str(tmp_path)):
        assert signature_assets.ensure_portrait(70004) is None
    assert not list(tmp_path.rglob("*"))             # atomic: a partial stream leaves nothing behind


def test_h7_html_body_with_image_ctype_degrades_to_monogram(tmp_path):
    # A 200 that lied about content-type is written, but the renderer refuses to open a non-image
    # and falls back — a bad body can never crash a render (defence at the load seam).
    from apps.killboard.signature_render import _load_portrait
    fake = tmp_path / "characters" / "70005" / "portrait-256.png"
    fake.parent.mkdir(parents=True)
    fake.write_bytes(b"<html><body>404 Not Found</body></html>")
    assert _load_portrait(str(fake), 64) is None


@responses.activate
def test_h7_non_200_is_rejected(tmp_path):
    responses.add(responses.GET, _portrait_url(70006), body=b"nope", status=500,
                  content_type="text/plain")
    with override_settings(EVE_IMAGE_MIRROR_DIR=str(tmp_path)):
        assert signature_assets.ensure_portrait(70006) is None
    assert not list(tmp_path.rglob("*"))


# =========================================================================== #
#  H8 — Public endpoint edges (/s/<token>.png).
# =========================================================================== #
def test_h8_oversized_token_rejected_before_any_db_query():
    # A 10 KB token must be rejected by the format gate with ZERO filesystem/DB work.
    req = RequestFactory().get("/s/x.png")
    with CaptureQueriesContext(connection) as ctx:
        resp = signature_public.signature_png(req, token="A" * 10240)
    assert resp.status_code == 404
    assert len(ctx.captured_queries) == 0


def test_h8_traversal_token_404s_without_probe():
    req = RequestFactory().get("/s/x.png")
    with CaptureQueriesContext(connection) as ctx:
        resp = signature_public.signature_png(req, token="../../../../etc/passwd")
    assert resp.status_code == 404
    assert len(ctx.captured_queries) == 0
    # And the filename derivation itself refuses a traversal token outright.
    with pytest.raises(ValueError):
        signatures.artifact_path("../../etc/passwd")


def test_h8_valid_charset_but_wrong_length_token_misses_cleanly(client):
    _enable()
    # 21 chars is inside the 20–24 regex window but no real token is 21 → a clean unavailable 404.
    resp = client.get("/s/" + "A" * 21 + ".png")
    assert resp.status_code == 404
    assert resp["Content-Type"] == "image/png"       # constant-shape placeholder, no crash


def test_h8_disabled_and_unknown_share_identical_headers(client, django_user_model):
    _enable()
    _u, char = enrol_pilot(django_user_model, 90801)
    sig = _sig(char, status=DISABLED)
    disabled = client.get(f"/s/{sig.public_token}.png")
    unknown = client.get("/s/" + "Zz09_-Zz09_-Zz09_-Zz0" + ".png")
    assert disabled.status_code == unknown.status_code == 404
    assert disabled.content == unknown.content        # byte-identical body (anti-enumeration)
    for h in ("Cache-Control", "X-Content-Type-Options", "X-Robots-Tag", "Content-Type"):
        assert disabled[h] == unknown[h], h           # ...and identical headers


def test_h8_range_request_keeps_hardened_headers(client, django_user_model):
    _enable()
    _u, char = enrol_pilot(django_user_model, 90802)
    sig = _sig(char)
    path = signatures.artifact_path(sig.public_token)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(render_signature_png(None, _payload("Ranged")))
    resp = client.get(f"/s/{sig.public_token}.png", HTTP_RANGE="bytes=0-9")
    # Whatever the range behaviour, the security posture must not be bypassed by a Range header.
    assert resp.status_code in (200, 206)
    assert resp["X-Content-Type-Options"] == "nosniff"
    assert resp["X-Robots-Tag"] == "noindex, nofollow"
    assert resp["Cache-Control"].startswith("public")


def test_h8_head_and_get_agree_on_etag(client, django_user_model):
    _enable()
    _u, char = enrol_pilot(django_user_model, 90803)
    sig = _sig(char)
    path = signatures.artifact_path(sig.public_token)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(render_signature_png(None, _payload("HeadGet")))
    get = client.get(f"/s/{sig.public_token}.png")
    head = client.head(f"/s/{sig.public_token}.png")
    assert get["ETag"] == head["ETag"]
    assert get["Cache-Control"] == head["Cache-Control"]
    assert head.content == b""                        # HEAD carries headers but no body


# =========================================================================== #
#  H9 — Admin surface: role gating, CSRF, GET-only public, maintenance registry.
# =========================================================================== #
def test_h9_officer_cannot_reach_director_endpoints(client, django_user_model):
    _enable()
    user, _char = enrol_pilot(django_user_model, 90901, roles=(rbac.ROLE_OFFICER,))
    client.force_login(user)
    assert client.get(reverse("admin_audit:signature_settings")).status_code == 403
    assert client.get(reverse("admin_audit:signature_backgrounds")).status_code == 403
    assert client.post(reverse("admin_audit:signature_settings"), {}).status_code == 403


def test_h9_public_endpoint_is_get_only(client, django_user_model):
    _enable()
    _u, char = enrol_pilot(django_user_model, 90902)
    sig = _sig(char)
    assert client.post(f"/s/{sig.public_token}.png").status_code == 405   # require_safe


def test_h9_action_endpoint_enforces_csrf(django_user_model):
    _enable()
    user, char = enrol_pilot(django_user_model, 90903)
    sig = _sig(char)
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    resp = csrf_client.post(f"/killboard/signatures/{sig.pk}/regenerate/")
    assert resp.status_code == 403                    # no csrf_exempt anywhere on the mutation POSTs


def test_h9_maintenance_runner_rejects_unknown_task(client, django_user_model):
    _enable()
    user, _char = enrol_pilot(django_user_model, 90904, roles=(rbac.ROLE_DIRECTOR,))
    client.force_login(user)
    resp = client.post(reverse("admin_audit:run_maintenance", args=["evil.injected.task"]))
    assert resp.status_code == 403                    # fixed registry; unknown key → PermissionDenied
