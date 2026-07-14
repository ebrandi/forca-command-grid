"""The enum-label leak: a translated label must NEVER reach ``Alert.context`` (doc 08 §11.1).

A ``TextChoices`` label (``get_health_display()``, ``get_status_display()``,
``get_timer_type_display()``…) is a ``gettext_lazy`` proxy. ``str()``-ing one into ``Alert.context``
— a JSONField — collapses it under whatever locale is active AT EMIT TIME. These call sites are
reached from REQUEST paths, so that locale is the **acting officer's** language, not English and not
the corp broadcast locale. ``rendering_i18n.render_for`` then interpolates context slots VERBATIM
(by design: a slot is a raw EVE/user datum and never passes through gettext), so a Japanese
officer's click would hand a Japanese word to every Portuguese recipient — and freeze it in the
English audit column too.

The fix, pinned here: the label lives in the SCAFFOLD msgid (one key per enum value, selected from
the RAW code) and the context carries codes/data only. Each test emits AS A JAPANESE OFFICER
(``translation.override("ja")``) and asserts:

* ``Alert.title`` / ``Alert.body`` (the audit columns) are ENGLISH;
* ``Alert.context`` holds only raw codes/EVE data — no Japanese, no label slot at all;
* ``render_for(alert, "pt-br")`` is PORTUGUESE with no Japanese anywhere in it;
* ``render_for(alert, "en")`` returns the original English.

Portuguese is injected into the live catalogue (like ``test_i18n_notification_scaffolds``) so the
proof does not depend on a shipped ``.mo``.
"""
from __future__ import annotations

import datetime as dt
import re

import pytest
from django.utils import timezone, translation
from django.utils.translation import trans_real

from apps.pingboard import messages
from apps.pingboard.models import Alert
from apps.pingboard.rendering_i18n import render_for
from core.i18n import config as i18n_config

pytestmark = pytest.mark.django_db

# Any CJK codepoint — the fingerprint of a leaked Japanese label.
_CJK = re.compile(r"[぀-ヿ一-鿿]")


def _msgid(proxy) -> str:
    with translation.override(None):
        return str(proxy)


def _translate(lang: str, key: str, subject: str, body: str) -> None:
    """Inject a ``lang`` translation for a scaffold's subject+body msgids."""
    sc = messages.SCAFFOLDS[key]
    cat = trans_real.translation(lang)._catalog
    if sc.subject:
        cat[_msgid(sc.subject)] = subject
    cat[_msgid(sc.body)] = body


@pytest.fixture
def locales():
    """Enable ja + pt-br and drop the mutated catalogues afterwards."""
    i18n_config.set_i18n_config(locales={"ja": True, "pt-br": True})
    try:
        yield
    finally:
        for lang in ("ja", "pt-br"):
            trans_real._translations.pop(lang, None)


def _assert_clean(alert, *, english_body: str, english_title: str, pt_body: str, pt_subject: str):
    """The shared contract: English audit columns, raw context, Portuguese re-render."""
    # 1) The audit columns are ENGLISH — the Japanese officer's locale never froze into them.
    assert alert.title == english_title
    assert alert.body == english_body
    assert not _CJK.search(alert.title)
    assert not _CJK.search(alert.body)

    # 2) The persisted context is raw codes/EVE data only — no label, no Japanese.
    for name, value in alert.context.items():
        assert not _CJK.search(str(value)), f"{name} leaked a translated label: {value!r}"

    # 3) A Portuguese recipient is re-rendered in PORTUGUESE, with no Japanese in it.
    with translation.override("pt-br"):
        subject_pt, body_pt = render_for(alert, "pt-br")
    assert body_pt == pt_body
    assert subject_pt == pt_subject
    assert not _CJK.search(body_pt) and not _CJK.search(subject_pt)

    # 4) English still renders the original English.
    with translation.override("en"):
        _, body_en = render_for(alert, "en")
    assert body_en == english_body


# --- campaigns.health_changed -------------------------------------------------
def test_campaign_health_alert_never_leaks_the_officers_locale(django_user_model, locales):
    from apps.campaigns import notify
    from apps.campaigns.models import Campaign

    key = "campaigns.health_changed.at_risk"
    _translate(
        "ja", key, "キャンペーン健全性 リスクあり: «{campaign_name}»",
        "キャンペーン «{campaign_name}» はリスクありです。 {link}",
    )
    _translate(
        "pt-br", key, "Saúde da campanha Em Risco: «{campaign_name}»",
        "A campanha «{campaign_name}» está agora Em Risco. {link}",
    )

    campaign = Campaign.objects.create(
        name="Op X", status=Campaign.Status.ACTIVE, health=Campaign.Health.AT_RISK,
        health_reasons=[],
    )

    # The emitting officer's UI is Japanese — this is the exact scenario the auditors reproduced.
    with translation.override("ja"):
        notify.health_changed(campaign)

    alert = Alert.objects.get(idempotency_key__startswith="campaigns:health:")
    assert alert.template_key == key
    assert "health_label" not in alert.context  # the label slot is gone entirely
    link = alert.context["link"]
    _assert_clean(
        alert,
        english_title="Campaign health At Risk: «Op X»",
        english_body=f"Campaign «Op X» is now At Risk. {link}",
        pt_body=f"A campanha «Op X» está agora Em Risco. {link}",
        pt_subject="Saúde da campanha Em Risco: «Op X»",
    )


def test_campaign_health_alert_with_reasons_uses_the_reasons_key(django_user_model, locales):
    from apps.campaigns import notify
    from apps.campaigns.models import Campaign

    key = "campaigns.health_changed.blocked.reasons"
    _translate(
        "ja", key, "キャンペーン健全性 ブロック: «{campaign_name}»",
        "キャンペーン «{campaign_name}» はブロックされました — {details}。 {link}",
    )
    _translate(
        "pt-br", key, "Saúde da campanha Bloqueada: «{campaign_name}»",
        "A campanha «{campaign_name}» está agora Bloqueada — {details}. {link}",
    )

    campaign = Campaign.objects.create(
        name="Op Y", status=Campaign.Status.ACTIVE, health=Campaign.Health.BLOCKED,
        health_reasons=[{"code": "milestone_missed", "label": "A milestone was missed",
                         "detail": ""}],
    )
    with translation.override("ja"):
        notify.health_changed(campaign)

    alert = Alert.objects.get(idempotency_key__startswith="campaigns:health:")
    assert alert.template_key == key
    assert set(alert.context) == {"campaign_name", "details", "link"}
    link = alert.context["link"]
    _assert_clean(
        alert,
        english_title="Campaign health Blocked: «Op Y»",
        english_body=f"Campaign «Op Y» is now Blocked — A milestone was missed. {link}",
        pt_body=f"A campanha «Op Y» está agora Bloqueada — A milestone was missed. {link}",
        pt_subject="Saúde da campanha Bloqueada: «Op Y»",
    )


# --- store.order_status -------------------------------------------------------
def test_store_order_status_dm_never_leaks_the_officers_locale(django_user_model, locales):
    from apps.store import services as store
    from apps.store.models import StoreOrder

    key = "store.order_status.ready"
    _translate("ja", key, "注文の更新: 準備完了", "{ship_name} の準備ができました。")
    _translate("pt-br", key, "Atualização do pedido: Pronto",
               "{ship_name} está pronto — combine a retirada.")

    buyer = django_user_model.objects.create(username="eve:buyer", language="pt-br")
    order = StoreOrder.objects.create(
        buyer=buyer, kind=StoreOrder.Kind.HULL, ship_type_id=17843,
        ship_name="Vexor Navy Issue", status=StoreOrder.Status.READY,
    )

    # An officer whose console is Japanese moves the order to READY.
    officer = django_user_model.objects.create(username="eve:officer", language="ja")
    with translation.override("ja"):
        store.notify_order_status(order, actor=officer)

    alert = Alert.objects.get(source_service="store")
    assert alert.template_key == key
    assert alert.context == {"ship_name": "Vexor Navy Issue"}  # no status_label at all
    _assert_clean(
        alert,
        english_title="Order update: Ready",
        english_body="Vexor Navy Issue is ready — coordinate pickup.",
        pt_body="Vexor Navy Issue está pronto — combine a retirada.",
        pt_subject="Atualização do pedido: Pronto",
    )


# --- operations.structure_timer ----------------------------------------------
def test_structure_timer_announce_never_leaks_the_officers_locale(django_user_model, locales):
    from apps.operations import services as ops
    from apps.operations.models import StructureTimer

    key = "operations.structure_timer.hull.hostile"
    _translate(
        "ja", key, "タイマー — {structure_name}",
        "⏰ **タイマー** — {structure_name} (船体/最終, 敵対（攻撃）)\n🕒 {timer_time} EVE · {system_name}",
    )
    _translate(
        "pt-br", key, "Timer — {structure_name}",
        "⏰ **Timer** — {structure_name} (Casco / Final, Hostil (ataque))\n"
        "🕒 {timer_time} EVE · {system_name}",
    )

    timer = StructureTimer.objects.create(
        name="Astrahus Alpha", system_name="Jita", exits_at=timezone.now() + dt.timedelta(days=1),
        timer_type=StructureTimer.TimerType.HULL, side=StructureTimer.Side.HOSTILE,
    )
    with translation.override("ja"):
        alert = ops.announce_structure_timer(timer)

    assert alert is not None
    alert.refresh_from_db()
    assert alert.template_key == key
    # No timer_type / timer_side slot survives; the date is pinned to English, not the officer's.
    assert set(alert.context) == {"structure_name", "timer_time", "system_name"}
    when = alert.context["timer_time"]
    _assert_clean(
        alert,
        english_title="Timer — Astrahus Alpha",
        english_body=(
            f"⏰ **Timer** — Astrahus Alpha (Hull / Final, Hostile (attack))\n"
            f"🕒 {when} EVE · Jita"
        ),
        pt_body=(
            f"⏰ **Timer** — Astrahus Alpha (Casco / Final, Hostil (ataque))\n"
            f"🕒 {when} EVE · Jita"
        ),
        pt_subject="Timer — Astrahus Alpha",
    )


def test_structure_timer_no_system_variant_selects_the_no_system_key(locales):
    from apps.operations import services as ops
    from apps.operations.models import StructureTimer

    timer = StructureTimer.objects.create(
        name="Raitaru Beta", system_name="", exits_at=timezone.now() + dt.timedelta(hours=6),
        timer_type=StructureTimer.TimerType.ARMOR, side=StructureTimer.Side.FRIENDLY,
    )
    with translation.override("ja"):
        alert = ops.announce_structure_timer(timer)

    assert alert is not None
    alert.refresh_from_db()
    assert alert.template_key == "operations.structure_timer.armor.friendly.no_system"
    assert not _CJK.search(alert.body)
    assert not any(_CJK.search(str(v)) for v in alert.context.values())
    when = alert.context["timer_time"]
    assert alert.body == (
        f"⏰ **Timer** — Raitaru Beta (Armor, Friendly (defend))\n🕒 {when} EVE"
    )


# --- the structural guard: no scaffold may carry a translated label -----------
def test_no_scaffold_carries_an_enum_label_slot():
    """``{health_label}`` / ``{status_label}`` / ``{timer_type}`` / ``{timer_side}`` are enum-label
    slots: an enum label is chrome and must live in the msgid, so no scaffold may interpolate one."""
    import string

    banned = {"health_label", "status_label", "timer_type", "timer_side"}
    offenders = []
    for key, sc in messages.SCAFFOLDS.items():
        for text in (_msgid(sc.subject) if sc.subject else "", _msgid(sc.body)):
            for _lit, field, _spec, _conv in string.Formatter().parse(text):
                if field in banned:
                    offenders.append(f"{key}: {{{field}}}")
    assert offenders == []


def test_every_health_and_timer_enum_value_has_a_scaffold():
    """Every value the call sites can select must resolve — otherwise the alert silently degrades
    to the verbatim English body and the recipient loses their language."""
    from apps.campaigns.models import Campaign
    from apps.operations.models import StructureTimer

    for level in Campaign.Health.values:
        if level == Campaign.Health.UNKNOWN:
            continue  # unknown health never notifies
        assert f"campaigns.health_changed.{level}" in messages.SCAFFOLDS
        assert f"campaigns.health_changed.{level}.reasons" in messages.SCAFFOLDS

    for ttype in StructureTimer.TimerType.values:
        for side in StructureTimer.Side.values:
            assert f"operations.structure_timer.{ttype}.{side}" in messages.SCAFFOLDS
            assert f"operations.structure_timer.{ttype}.{side}.no_system" in messages.SCAFFOLDS


def test_every_store_status_has_a_scaffold_and_an_english_title():
    from apps.store import services as store
    from apps.store.models import StoreOrder

    for status in StoreOrder.Status.values:
        if status == StoreOrder.Status.OPEN:
            continue  # the initial/reopened state never DMs
        assert store._STATUS_TEMPLATE[status] in messages.SCAFFOLDS
        assert store._STATUS_TITLE[status].startswith("Order update: ")


# --- English fidelity: the scaffold's English == the pre-migration f-string -----
# ``services._render_body`` checks SCAFFOLDS FIRST and DISCARDS the ``body=`` the call site passes,
# so ``Alert.body`` (the English audit column) is now the scaffold's English render. That is only
# acceptable if the scaffold's English is byte-identical to what the old f-string produced — these
# two tests pin that for every enum value, not just the ones an emit test happens to exercise.
def test_timer_scaffold_english_matches_the_legacy_fstring():
    from apps.operations.models import StructureTimer
    from apps.pingboard import rendering

    ctx = {"structure_name": "Astrahus Alpha", "timer_time": "Thu 09 Jul · 18:30",
           "system_name": "Jita"}
    with translation.override("en"):
        for ttype, tlabel in StructureTimer.TimerType.choices:
            for side, slabel in StructureTimer.Side.choices:
                # Exactly the f-string the call site used before the migration.
                legacy = (
                    f"⏰ **Timer** — {ctx['structure_name']} ({tlabel}, {slabel})\n"
                    f"🕒 {ctx['timer_time']} EVE"
                )
                key = f"operations.structure_timer.{ttype}.{side}"
                sc = messages.SCAFFOLDS[key]
                assert rendering.render(str(sc.body), ctx) == f"{legacy} · {ctx['system_name']}"
                no_sys = messages.SCAFFOLDS[f"{key}.no_system"]
                assert rendering.render(str(no_sys.body), {**ctx, "system_name": ""}) == legacy
                assert rendering.render(str(sc.subject), ctx) == "Timer — Astrahus Alpha"


def test_health_scaffold_english_matches_the_legacy_fstring():
    from apps.campaigns.models import Campaign
    from apps.pingboard import rendering

    ctx = {"campaign_name": "Op X", "details": "A milestone was missed",
           "link": "https://example.test/campaigns/1/"}
    with translation.override("en"):
        for level, label in Campaign.Health.choices:
            if level == Campaign.Health.UNKNOWN:
                continue
            plain = messages.SCAFFOLDS[f"campaigns.health_changed.{level}"]
            reasons = messages.SCAFFOLDS[f"campaigns.health_changed.{level}.reasons"]
            # Exactly the f-string the call site used before the migration.
            assert rendering.render(str(plain.body), ctx) == (
                f"Campaign «{ctx['campaign_name']}» is now {label}. {ctx['link']}"
            )
            assert rendering.render(str(reasons.body), ctx) == (
                f"Campaign «{ctx['campaign_name']}» is now {label} — {ctx['details']}. "
                f"{ctx['link']}"
            )
            assert rendering.render(str(plain.subject), ctx) == (
                f"Campaign health {label}: «{ctx['campaign_name']}»"
            )


def test_store_scaffold_subject_matches_the_legacy_english_title():
    from apps.store import services as store
    from apps.store.models import StoreOrder

    with translation.override("en"):
        for status, label in StoreOrder.Status.choices:
            if status == StoreOrder.Status.OPEN:
                continue
            sc = messages.SCAFFOLDS[store._STATUS_TEMPLATE[status]]
            # The legacy title was f"Order update: {order.get_status_display()}".
            assert str(sc.subject) == f"Order update: {label}"
            assert store._STATUS_TITLE[status] == f"Order update: {label}"


def test_health_label_map_matches_the_model_labels():
    """The frozen English audit label must stay byte-identical to the model's English label, so a
    rename of the ``TextChoices`` label can never silently drift the audit column."""
    from apps.campaigns.models import Campaign
    from apps.campaigns.notify import _HEALTH_LABEL_EN

    with translation.override("en"):
        for value, label in Campaign.Health.choices:
            if value == Campaign.Health.UNKNOWN:
                continue
            assert _HEALTH_LABEL_EN[value] == str(label)
