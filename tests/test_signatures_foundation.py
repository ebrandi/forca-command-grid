"""Combat Signatures — WS-1 foundation unit tests.

Covers the domain surface in ``apps/killboard/signatures.py`` and the four persistence tables:
strict config-schema validation (allowlist, dedupe, caps, type checks), name sanitisation
(control/bidi stripping, whitespace collapse, length), the unguessable public token
(uniqueness + rotation), per-pilot active quotas, the snapshot DB check constraint, the
settings singleton, the lifecycle state machine (create/duplicate/disable/enable/snapshot/
freeze/unfreeze), the owner-scoped edit guard (LP-4 ceiling), and the token-guarded artifact
path helper. No renderer / Celery / view is exercised — those arrive in later workstreams.
"""
from __future__ import annotations

import os

import pytest
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.admin_audit.models import AuditLog
from apps.killboard import signatures
from apps.killboard.models import (
    CombatSignature,
    CombatSignatureSettings,
    SignatureBackground,
    SignatureScanState,
)
from tests._raffle_utils import enrol_pilot

pytestmark = pytest.mark.django_db


# --------------------------------------------------------------------------- #
#  Builders
# --------------------------------------------------------------------------- #
@pytest.fixture
def background(db):
    return SignatureBackground.objects.create(key="nebula-01", name="Nebula", enabled=True)


def _settings(**over):
    cfg = CombatSignatureSettings.load()
    for key, value in over.items():
        setattr(cfg, key, value)
    if over:
        cfg.save()
    return cfg


def _config(**over):
    base = {
        "components": ["pilot_name", "kills"],
        "period": "30d",
        "featured_trophy_ids": [],
        "show_timestamp": False,
        "theme": "gold",
    }
    base.update(over)
    return base


def _create(user, background, **over):
    kwargs = dict(
        name="My Signature", background=background,
        layout="identity", size_preset="standard", config=_config(),
    )
    kwargs.update(over)
    return signatures.create_signature(user, **kwargs)


# --------------------------------------------------------------------------- #
#  validate_config
# --------------------------------------------------------------------------- #
def test_validate_config_happy(background):
    cfg = _settings()
    out = signatures.validate_config(
        _config(components=["portrait", "pilot_name", "kills"], featured_trophy_ids=[1, 2]),
        settings=cfg, background=background, layout="identity", size_preset="standard",
    )
    assert out == {
        "components": ["portrait", "pilot_name", "kills"],
        "period": "30d",
        "featured_trophy_ids": [1, 2],
        "show_timestamp": False,
        "theme": "gold",
    }


def test_validate_config_rejects_non_dict(background):
    cfg = _settings()
    with pytest.raises(ValidationError):
        signatures.validate_config(
            ["not", "a", "dict"], settings=cfg, background=background,
            layout="identity", size_preset="standard",
        )


def test_validate_config_rejects_unknown_key(background):
    cfg = _settings()
    with pytest.raises(ValidationError):
        signatures.validate_config(
            _config(surprise=1), settings=cfg, background=background,
            layout="identity", size_preset="standard",
        )


def test_validate_config_rejects_unknown_component(background):
    cfg = _settings()
    with pytest.raises(ValidationError):
        signatures.validate_config(
            _config(components=["kills", "not_a_component"]), settings=cfg,
            background=background, layout="identity", size_preset="standard",
        )


def test_validate_config_rejects_duplicate_component(background):
    cfg = _settings()
    with pytest.raises(ValidationError):
        signatures.validate_config(
            _config(components=["kills", "kills"]), settings=cfg,
            background=background, layout="identity", size_preset="standard",
        )


def test_validate_config_rejects_too_many_components(background):
    cfg = _settings()
    too_many = [
        "portrait", "pilot_name", "corp", "alliance", "kills", "losses", "solo_kills",
        "final_blows", "isk_destroyed", "isk_lost", "isk_efficiency", "kd_ratio", "rank_title",
    ]  # 13 > 12
    with pytest.raises(ValidationError):
        signatures.validate_config(
            _config(components=too_many), settings=cfg, background=background,
            layout="identity", size_preset="standard",
        )


def test_validate_config_rejects_bad_period(background):
    cfg = _settings()
    with pytest.raises(ValidationError):
        signatures.validate_config(
            _config(period="daily"), settings=cfg, background=background,
            layout="identity", size_preset="standard",
        )


def test_validate_config_rejects_bad_theme(background):
    cfg = _settings()
    with pytest.raises(ValidationError):
        signatures.validate_config(
            _config(theme="rainbow"), settings=cfg, background=background,
            layout="identity", size_preset="standard",
        )


def test_validate_config_rejects_excessive_trophies(background):
    cfg = _settings(max_featured_trophies=3)
    with pytest.raises(ValidationError):
        signatures.validate_config(
            _config(featured_trophy_ids=[1, 2, 3, 4]), settings=cfg,
            background=background, layout="identity", size_preset="standard",
        )


def test_validate_config_rejects_non_int_trophy(background):
    cfg = _settings()
    # bool is a subclass of int but must not slip through as a trophy id.
    for bad in ([1.5], [True], ["x"]):
        with pytest.raises(ValidationError):
            signatures.validate_config(
                _config(featured_trophy_ids=bad), settings=cfg, background=background,
                layout="identity", size_preset="standard",
            )


def test_validate_config_rejects_nested_junk(background):
    cfg = _settings()
    with pytest.raises(ValidationError):
        signatures.validate_config(
            _config(components=[{"nested": "junk"}]), settings=cfg,
            background=background, layout="identity", size_preset="standard",
        )


def test_validate_config_rejects_non_bool_show_timestamp(background):
    cfg = _settings()
    with pytest.raises(ValidationError):
        signatures.validate_config(
            _config(show_timestamp="yes"), settings=cfg, background=background,
            layout="identity", size_preset="standard",
        )


def test_validate_config_rejects_bad_layout_and_preset(background):
    cfg = _settings()
    with pytest.raises(ValidationError):
        signatures.validate_config(
            _config(), settings=cfg, background=background,
            layout="bogus", size_preset="standard",
        )
    with pytest.raises(ValidationError):
        signatures.validate_config(
            _config(), settings=cfg, background=background,
            layout="identity", size_preset="bogus",
        )


def test_validate_config_rejects_disallowed_preset(background):
    cfg = _settings(allowed_size_presets=["compact"])
    with pytest.raises(ValidationError):
        signatures.validate_config(
            _config(), settings=cfg, background=background,
            layout="identity", size_preset="wide",
        )


def test_validate_config_rejects_disabled_background(background):
    cfg = _settings()
    background.enabled = False
    background.save(update_fields=["enabled"])
    with pytest.raises(ValidationError):
        signatures.validate_config(
            _config(), settings=cfg, background=background,
            layout="identity", size_preset="standard",
        )


# --------------------------------------------------------------------------- #
#  sanitize_name
# --------------------------------------------------------------------------- #
def test_sanitize_name_strips_control_chars():
    assert signatures.sanitize_name("Ab\x00\x07cd") == "Abcd"


def test_sanitize_name_strips_bidi_override():
    # U+202E RIGHT-TO-LEFT OVERRIDE is Unicode category Cf and must be dropped.
    assert signatures.sanitize_name("gg\u202eno re") == "ggno re"


def test_sanitize_name_collapses_whitespace():
    assert signatures.sanitize_name("  Red   Alert \t\n Squad  ") == "Red Alert Squad"


def test_sanitize_name_enforces_length():
    assert len(signatures.sanitize_name("x" * 60)) == 60
    with pytest.raises(ValidationError):
        signatures.sanitize_name("x" * 61)


def test_sanitize_name_rejects_empty():
    for raw in ("", "   ", "\x00\x00", 123):
        with pytest.raises(ValidationError):
            signatures.sanitize_name(raw)


# --------------------------------------------------------------------------- #
#  Tokens
# --------------------------------------------------------------------------- #
def test_tokens_are_unique(django_user_model, background):
    user, _ = enrol_pilot(django_user_model, 7001)
    a = _create(user, background)
    b = _create(user, background)
    assert a.public_token and b.public_token
    assert a.public_token != b.public_token


def test_rotate_token_changes_token(django_user_model, background):
    user, _ = enrol_pilot(django_user_model, 7002)
    sig = _create(user, background)
    old = sig.public_token
    new = signatures.rotate_token(user, sig)
    assert new != old
    sig.refresh_from_db()
    assert sig.public_token == new
    assert sig.dirty is True
    assert sig.render_status == CombatSignature.RenderStatus.PENDING


def test_rotate_token_deletes_old_artifact(django_user_model, background, tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)
    user, _ = enrol_pilot(django_user_model, 7003)
    sig = _create(user, background)
    old_path = signatures.artifact_path(sig.public_token)
    os.makedirs(os.path.dirname(old_path), exist_ok=True)
    with open(old_path, "wb") as fh:
        fh.write(b"png")
    signatures.rotate_token(user, sig)
    assert not os.path.exists(old_path)


# --------------------------------------------------------------------------- #
#  Quotas
# --------------------------------------------------------------------------- #
def test_quota_enforced(django_user_model, background):
    _settings(max_active_per_pilot=2)
    user, _ = enrol_pilot(django_user_model, 7100)
    _create(user, background)
    _create(user, background)
    with pytest.raises(ValidationError):
        _create(user, background)


def test_disabled_signatures_do_not_count_against_quota(django_user_model, background):
    _settings(max_active_per_pilot=1)
    user, _ = enrol_pilot(django_user_model, 7101)
    first = _create(user, background)
    signatures.disable(user, first)
    # Now under quota again — a second active signature is allowed.
    assert _create(user, background) is not None


# --------------------------------------------------------------------------- #
#  Snapshot constraint
# --------------------------------------------------------------------------- #
def test_snapshot_without_timestamp_is_rejected(django_user_model, background):
    _, char = enrol_pilot(django_user_model, 7200)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            CombatSignature.objects.create(
                character=char, name="bad", background=background,
                mode=CombatSignature.Mode.SNAPSHOT, snapshot_taken_at=None, config={},
            )


def test_snapshot_with_timestamp_is_allowed(django_user_model, background):
    _, char = enrol_pilot(django_user_model, 7201)
    sig = CombatSignature.objects.create(
        character=char, name="ok", background=background,
        mode=CombatSignature.Mode.SNAPSHOT, snapshot_taken_at=timezone.now(), config={},
    )
    assert sig.pk


# --------------------------------------------------------------------------- #
#  Settings singleton & scan cursor
# --------------------------------------------------------------------------- #
def test_settings_singleton_load_and_save():
    a = CombatSignatureSettings.load()
    b = CombatSignatureSettings.load()
    assert a.pk == b.pk
    a.max_active_per_pilot = 9
    a.save()
    assert CombatSignatureSettings.load().max_active_per_pilot == 9


def test_scan_state_singleton():
    a = SignatureScanState.load()
    b = SignatureScanState.load()
    assert a.pk == b.pk
    assert a.last_seq == 0


# --------------------------------------------------------------------------- #
#  Lifecycle
# --------------------------------------------------------------------------- #
def test_create_records_audit(django_user_model, background):
    user, _ = enrol_pilot(django_user_model, 7300)
    sig = _create(user, background)
    entry = AuditLog.objects.filter(action="signatures.create", target_id=str(sig.pk)).first()
    assert entry is not None
    assert entry.metadata["character_id"] == sig.character_id


def test_create_rejected_for_non_member(django_user_model, background):
    user, _ = enrol_pilot(django_user_model, 7301, is_corp_member=False)
    with pytest.raises(ValidationError):
        _create(user, background)


def test_duplicate_signature(django_user_model, background):
    user, _ = enrol_pilot(django_user_model, 7302)
    sig = _create(user, background, name="Original")
    copy = signatures.duplicate_signature(user, sig)
    assert copy.pk != sig.pk
    assert copy.public_token != sig.public_token
    assert copy.character_id == sig.character_id
    assert copy.config == sig.config
    assert copy.name != sig.name


def test_disable_enable(django_user_model, background):
    user, _ = enrol_pilot(django_user_model, 7303)
    sig = _create(user, background)
    signatures.disable(user, sig)
    assert sig.status == CombatSignature.Status.DISABLED
    signatures.enable(user, sig)
    sig.refresh_from_db()
    assert sig.status == CombatSignature.Status.ACTIVE
    assert sig.dirty is True


def test_disable_removes_artifact(django_user_model, background, tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path)
    user, _ = enrol_pilot(django_user_model, 7304)
    sig = _create(user, background)
    path = signatures.artifact_path(sig.public_token)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"png")
    signatures.disable(user, sig)
    assert not os.path.exists(path)


def test_take_snapshot(django_user_model, background):
    user, _ = enrol_pilot(django_user_model, 7305)
    sig = _create(user, background)
    assert sig.mode == CombatSignature.Mode.LIVE
    signatures.take_snapshot(user, sig)
    sig.refresh_from_db()
    assert sig.mode == CombatSignature.Mode.SNAPSHOT
    assert sig.snapshot_taken_at is not None
    with pytest.raises(ValidationError):
        signatures.take_snapshot(user, sig)  # snapshot -> snapshot not allowed


def test_freeze_and_unfreeze(django_user_model, background):
    user, _ = enrol_pilot(django_user_model, 7306)
    sig = _create(user, background)
    signatures.freeze(sig)
    assert sig.status == CombatSignature.Status.FROZEN
    signatures.unfreeze(sig)
    sig.refresh_from_db()
    assert sig.status == CombatSignature.Status.ACTIVE
    assert sig.dirty is True


def test_freeze_with_revoke_disables_and_removes_artifact(
    django_user_model, background, tmp_path, settings
):
    settings.MEDIA_ROOT = str(tmp_path)
    user, _ = enrol_pilot(django_user_model, 7307)
    sig = _create(user, background)
    path = signatures.artifact_path(sig.public_token)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"png")
    signatures.freeze(sig, revoke=True)
    sig.refresh_from_db()
    assert sig.status == CombatSignature.Status.DISABLED
    assert not os.path.exists(path)


# --------------------------------------------------------------------------- #
#  Ownership guard
# --------------------------------------------------------------------------- #
def test_owner_member_can_edit(django_user_model, background):
    user, _ = enrol_pilot(django_user_model, 7400)
    sig = _create(user, background)
    assert signatures.can_edit(user, sig) is True
    signatures.require_edit(user, sig)  # no raise


def test_other_user_cannot_edit(django_user_model, background):
    owner, _ = enrol_pilot(django_user_model, 7401)
    sig = _create(owner, background)
    other, _ = enrol_pilot(django_user_model, 7402)
    assert signatures.can_edit(other, sig) is False
    with pytest.raises(ValidationError):
        signatures.require_edit(other, sig)


def test_non_member_owner_cannot_edit(django_user_model, background):
    user, char = enrol_pilot(django_user_model, 7403)
    sig = _create(user, background)
    char.is_corp_member = False
    char.save(update_fields=["is_corp_member"])
    assert signatures.can_edit(user, sig) is False


# --------------------------------------------------------------------------- #
#  Artifact path guard (threat model)
# --------------------------------------------------------------------------- #
def test_artifact_path_accepts_real_token(django_user_model, background):
    user, _ = enrol_pilot(django_user_model, 7500)
    sig = _create(user, background)
    path = signatures.artifact_path(sig.public_token)
    assert path.endswith(f"signatures/{sig.public_token}.png")


def test_artifact_path_rejects_traversal_and_bad_charset():
    for bad in ("../etc/passwd", "/abs/path", "bad!char", "short", "", "a" * 40):
        with pytest.raises(ValueError):
            signatures.artifact_path(bad)


def test_delete_artifact_tolerates_bad_token():
    assert signatures.delete_artifact("../etc/passwd") is False
    assert signatures.delete_artifact("does-not-exist-000000") is False
