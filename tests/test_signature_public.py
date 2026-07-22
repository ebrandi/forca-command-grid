"""Combat Signatures — WS-5 public delivery view tests (``GET/HEAD /s/<token>.png``).

Exercises the anonymous, login-free, cookie-free banner endpoint under the test settings: the
served / pending / unavailable tiers, exact cache + robots + nosniff headers, strong-ETag +
Last-Modified conditional 304s, HEAD (headers, no body), the constant-shape (byte-identical)
disabled-vs-unknown 404 (anti-enumeration), token rotation, a frozen owner's last image, strict
token-format validation with NO filesystem/DB probe on malformed input, the per-IP throttle, and
the membership-gate allowlist (a logged-in ex-member still loads the URL; a logged-in member keeps
the public — not no-store — cache header).

House style (test_signature_render.py / test_killboard_artifacts.py): image assertions check
structure (PNG format + exact preset dimensions), never pixels. Every artifact write goes to a
per-test MEDIA_ROOT and the mirror is disabled so nothing touches the network.
"""
from __future__ import annotations

import io
import os

import pytest
from PIL import Image

from apps.killboard import signature_pipeline, signatures
from apps.killboard.models import CombatSignature, SignatureBackground
from apps.killboard.signature_render import PRESETS, render_placeholder_png
from tests._raffle_utils import enrol_pilot

pytestmark = pytest.mark.django_db

ACTIVE = CombatSignature.Status.ACTIVE
FROZEN = CombatSignature.Status.FROZEN
DISABLED = CombatSignature.Status.DISABLED
LIVE = CombatSignature.Mode.LIVE
SNAPSHOT = CombatSignature.Mode.SNAPSHOT

# Two distinct, valid PNGs used as controlled on-disk artifacts (the delivery view serves the file
# bytes opaquely; their exact content lets a test assert byte equality and a size change).
PNG_A = render_placeholder_png("standard")
PNG_B = render_placeholder_png("wide")


@pytest.fixture(autouse=True)
def _no_network(settings):
    settings.EVE_IMAGE_MIRROR_DIR = ""


# --------------------------------------------------------------------------- #
#  Builders
# --------------------------------------------------------------------------- #
def _background() -> SignatureBackground:
    bg, _created = SignatureBackground.objects.get_or_create(
        key="nebula-emberfront", defaults={"name": "Ember", "enabled": True}
    )
    return bg


def _config(components=("pilot_name", "kills")) -> dict:
    return {
        "components": list(components), "period": "30d", "featured_trophy_ids": [],
        "show_timestamp": False, "theme": "gold",
    }


def _sig(char, *, status=ACTIVE, mode=LIVE, size_preset="standard",
         components=("pilot_name", "kills")) -> CombatSignature:
    from django.utils import timezone
    snap = timezone.now() if mode == SNAPSHOT else None
    return CombatSignature.objects.create(
        character=char, name="Sig", background=_background(), layout="identity",
        size_preset=size_preset, mode=mode, status=status, config=_config(components),
        snapshot_taken_at=snap,
    )


def _write_artifact(sig, data=PNG_A) -> str:
    path = signatures.artifact_path(sig.public_token)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def _body(resp) -> bytes:
    return b"".join(resp.streaming_content) if resp.streaming else resp.content


def _is_png(data: bytes, preset: str = "standard") -> bool:
    img = Image.open(io.BytesIO(data))
    return img.format == "PNG" and img.size == PRESETS[preset]


def _url(token: str) -> str:
    return f"/s/{token}.png"


# --------------------------------------------------------------------------- #
#  Served tier — a rendered artifact exists.
# --------------------------------------------------------------------------- #
def test_anonymous_get_serves_png_without_cookie(client, settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _user, char = enrol_pilot(django_user_model, 9000)
    sig = _sig(char)
    _write_artifact(sig, PNG_A)

    resp = client.get(_url(sig.public_token))
    assert resp.status_code == 200
    assert resp["Content-Type"] == "image/png"
    assert _body(resp) == PNG_A
    assert _is_png(PNG_A, "standard")
    # The view never touches request.session, so no session cookie is ever set on the response.
    assert not resp.cookies


def test_served_headers_are_exact(client, settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _user, char = enrol_pilot(django_user_model, 9001)
    sig = _sig(char)
    _write_artifact(sig, PNG_A)

    resp = client.get(_url(sig.public_token))
    assert resp["Cache-Control"] == "public, max-age=300"
    assert resp["X-Content-Type-Options"] == "nosniff"
    assert resp["X-Robots-Tag"] == "noindex, nofollow"
    assert resp.has_header("ETag") and resp.has_header("Last-Modified")


def test_served_real_pipeline_render(client, settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _user, char = enrol_pilot(django_user_model, 9002)
    sig = _sig(char)
    assert signature_pipeline.render_one(sig.pk) == "rendered"

    resp = client.get(_url(sig.public_token))
    assert resp.status_code == 200
    assert _is_png(_body(resp), "standard")


# --------------------------------------------------------------------------- #
#  Conditional requests — ETag (precedence) + Last-Modified 304s.
# --------------------------------------------------------------------------- #
def test_if_none_match_returns_304(client, settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _user, char = enrol_pilot(django_user_model, 9010)
    sig = _sig(char)
    _write_artifact(sig, PNG_A)

    etag = client.get(_url(sig.public_token))["ETag"]
    resp = client.get(_url(sig.public_token), HTTP_IF_NONE_MATCH=etag)
    assert resp.status_code == 304
    assert resp.content == b""                       # a 304 carries no body
    assert resp["ETag"] == etag                      # …but retains the ETag
    assert resp["Cache-Control"] == "public, max-age=300"


def test_if_modified_since_returns_304(client, settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _user, char = enrol_pilot(django_user_model, 9011)
    sig = _sig(char)
    _write_artifact(sig, PNG_A)

    last_modified = client.get(_url(sig.public_token))["Last-Modified"]
    resp = client.get(_url(sig.public_token), HTTP_IF_MODIFIED_SINCE=last_modified)
    assert resp.status_code == 304


def test_modified_file_yields_fresh_etag(client, settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _user, char = enrol_pilot(django_user_model, 9012)
    sig = _sig(char)
    path = _write_artifact(sig, PNG_A)

    etag1 = client.get(_url(sig.public_token))["ETag"]
    _write_artifact(sig, PNG_B)                      # different size…
    future = os.stat(path).st_mtime + 5
    os.utime(path, (future, future))                 # …and a later mtime
    etag2 = client.get(_url(sig.public_token))["ETag"]
    assert etag1 != etag2


# --------------------------------------------------------------------------- #
#  HEAD — same headers, no body.
# --------------------------------------------------------------------------- #
def test_head_has_headers_but_no_body(client, settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _user, char = enrol_pilot(django_user_model, 9020)
    sig = _sig(char)
    _write_artifact(sig, PNG_A)

    get = client.get(_url(sig.public_token))
    head = client.head(_url(sig.public_token))
    assert head.status_code == 200
    assert head.content == b""
    assert head["Content-Type"] == "image/png"
    assert head["Cache-Control"] == get["Cache-Control"] == "public, max-age=300"
    assert head["X-Content-Type-Options"] == "nosniff"
    assert head["X-Robots-Tag"] == "noindex, nofollow"
    assert head["Content-Length"] == str(len(PNG_A))


# --------------------------------------------------------------------------- #
#  Pending tier — the row exists but its artifact has not been rendered yet.
# --------------------------------------------------------------------------- #
def test_pending_returns_placeholder_200_short_cache(client, settings, tmp_path,
                                                     django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _user, char = enrol_pilot(django_user_model, 9030)
    sig = _sig(char)                                 # no artifact on disk

    resp = client.get(_url(sig.public_token))
    assert resp.status_code == 200
    assert resp["Cache-Control"] == "public, max-age=60"
    assert resp["X-Robots-Tag"] == "noindex, nofollow"
    assert _is_png(_body(resp), sig.size_preset)


# --------------------------------------------------------------------------- #
#  Unavailable tier — disabled and unknown are byte-for-byte indistinguishable.
# --------------------------------------------------------------------------- #
def test_disabled_and_unknown_are_identical_404(client, settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _user, char = enrol_pilot(django_user_model, 9040)
    # A disabled signature with a NON-standard preset: the response must still use the fixed
    # unavailable preset, so its real preset can never leak (it would distinguish it from unknown).
    disabled = _sig(char, status=DISABLED, size_preset="compact")
    unknown_token = "unknowntoken1234567890"          # 22 chars, valid shape, no row

    r_dis = client.get(_url(disabled.public_token))
    r_unk = client.get(_url(unknown_token))
    assert r_dis.status_code == r_unk.status_code == 404
    assert _body(r_dis) == _body(r_unk)               # byte-identical placeholder
    for header in ("Cache-Control", "X-Content-Type-Options", "X-Robots-Tag", "Content-Type"):
        assert r_dis[header] == r_unk[header]
    assert r_dis["Cache-Control"] == "public, max-age=60"


def test_rotated_token_old_404_new_200(client, settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    user, char = enrol_pilot(django_user_model, 9050)
    sig = _sig(char)
    _write_artifact(sig, PNG_A)
    old = sig.public_token

    new = signatures.rotate_token(user, sig)
    assert new != old
    assert client.get(_url(old)).status_code == 404        # old token → no row → unavailable
    r_new = client.get(_url(new))
    assert r_new.status_code == 200                        # new token → pending placeholder
    assert r_new["Cache-Control"] == "public, max-age=60"


def test_frozen_owner_keeps_last_artifact(client, settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _user, char = enrol_pilot(django_user_model, 9060)
    sig = _sig(char, status=FROZEN)
    _write_artifact(sig, PNG_A)

    resp = client.get(_url(sig.public_token))
    assert resp.status_code == 200
    assert _body(resp) == PNG_A


# --------------------------------------------------------------------------- #
#  Token-format validation — traversal / malformed → 404 with NO fs/DB probe.
# --------------------------------------------------------------------------- #
def test_malformed_tokens_404_without_filesystem_probe(client, settings, tmp_path, monkeypatch):
    settings.MEDIA_ROOT = str(tmp_path)
    probed: list[str] = []
    real = signatures.artifact_path
    monkeypatch.setattr(signatures, "artifact_path",
                        lambda token: (probed.append(token), real(token))[1])

    malformed = [
        "short",                    # too short
        "a" * 19,                   # 19 chars — under the 20 floor
        "a" * 25,                   # 25 chars — over the 24 ceiling
        "aaaaaaaaaa!aaaaaaaaa",     # 20 chars but a disallowed character
        "bad.name.here.foo",        # dots are not in the token charset
    ]
    for token in malformed:
        assert client.get(_url(token)).status_code == 404
    # A traversal never even routes to the view (the <str> converter rejects slashes).
    assert client.get("/s/../../etc/passwd.png").status_code == 404
    # The strict format gate rejected every one before any path resolution.
    assert probed == []


# --------------------------------------------------------------------------- #
#  Throttle — the Django fallback is per-IP rate limited.
# --------------------------------------------------------------------------- #
def test_throttle_returns_429_after_limit(client, settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.SIGNATURE_PUBLIC_RATE = 2
    _user, char = enrol_pilot(django_user_model, 9070)
    sig = _sig(char)
    _write_artifact(sig, PNG_A)
    url = _url(sig.public_token)

    assert client.get(url).status_code == 200
    assert client.get(url).status_code == 200
    third = client.get(url)
    assert third.status_code == 429
    assert third["Retry-After"] == "60"


# --------------------------------------------------------------------------- #
#  Membership-gate allowlist + the no-store override for logged-in viewers.
# --------------------------------------------------------------------------- #
def test_logged_in_non_member_is_served_not_redirected(client, settings, tmp_path,
                                                       django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    _owner_user, owner = enrol_pilot(django_user_model, 9080)
    sig = _sig(owner)
    _write_artifact(sig, PNG_A)
    # A logged-in NON-member (no member role, not in the corp) — e.g. an ex-member.
    outsider, _c = enrol_pilot(django_user_model, 9081, roles=(), is_corp_member=False)
    client.force_login(outsider)

    resp = client.get(_url(sig.public_token))
    assert resp.status_code == 200                    # allowlisted — not redirected to onboarding
    assert resp["Content-Type"] == "image/png"


def test_logged_in_member_keeps_public_cache(client, settings, tmp_path, django_user_model):
    settings.MEDIA_ROOT = str(tmp_path)
    user, char = enrol_pilot(django_user_model, 9090)
    sig = _sig(char)
    _write_artifact(sig, PNG_A)
    client.force_login(user)

    resp = client.get(_url(sig.public_token))
    assert resp.status_code == 200
    # The view sets Cache-Control explicitly, so the global authenticated `private, no-store`
    # never masks a public banner for a signed-in viewer.
    assert resp["Cache-Control"] == "public, max-age=300"
