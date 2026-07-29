"""Security QA 2026-07-28 — regression tests for the adversarial review fixes.

Covers the reflected-XSS class where a request value is interpolated into a JavaScript
string literal inside an Alpine ``@click`` attribute. Django's autoescaping is *not* a
defence in that position: it turns ``'`` into ``&#x27;``, but the browser HTML-decodes an
attribute value BEFORE Alpine evaluates it as JavaScript, so the entity becomes a real
quote again and closes the string. The CSP does not save us either — the injected code
runs inside an existing directive (no new ``<script>``, so the nonce is irrelevant) and
Alpine's expression evaluator needs the ``'unsafe-eval'`` the policy already grants.

The fix is to normalise such values to their known enum domain in the view, so nothing
attacker-shaped can reach the template at all.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac


def _user(dj, uid, role=rbac.ROLE_MEMBER):
    u = dj.objects.create(username=f"qa0728-{uid}")
    RoleAssignment.objects.create(user=u, role=ensure_role(role))
    # A DIRECTOR grant is ceilinged to OFFICER unless the ACTIVE pilot is an in-game
    # Director (core.rbac.authority_ceiling), so a director fixture needs the seat too.
    EveCharacter.objects.create(character_id=uid, user=u, name=f"P{uid}",
                                is_main=True, is_corp_member=True,
                                is_corp_director=(role == rbac.ROLE_DIRECTOR))
    return u


# --- Reflected XSS via ?owner= in the stockpile assets page ------------------
# The template renders:  @click="... fetch('{% url ... %}?owner={{ owner }}&location=...')"
# so a payload of  ');alert(1);//  escapes to  &#x27;);alert(1);//  and breaks out once the
# browser decodes the attribute.
BREAKOUT = "');alert(1);//"

# The vulnerable expression only renders inside the per-location loop, so the summary must
# report at least one location. Patching the summary keeps the test about the injection
# rather than about seeding an ESI asset sync.
_ONE_LOCATION = {
    "locations": [{"location_id": 60003760, "name": "Jita IV-4", "system_id": 30000142,
                   "kind_display": "Station", "value": 1, "item_count": 1, "units": 1}],
    "total_value": 1,
    "as_of": None,
}


@pytest.mark.django_db
def test_assets_owner_cannot_break_out_of_the_alpine_click_expression(client, django_user_model):
    client.force_login(_user(django_user_model, 728001))
    with patch("apps.stockpile.assets.assets_summary", return_value=_ONE_LOCATION):
        resp = client.get(reverse("stockpile:assets"), {"owner": BREAKOUT})
    assert resp.status_code == 200
    body = resp.content.decode()
    # Guard the guard: if the injection point stops rendering, this test must fail loudly
    # rather than pass vacuously.
    assert "?owner=" in body and "fetch(" in body
    # Neither the raw payload nor its HTML-escaped form may appear: the escaped form is
    # exactly what the browser decodes back into a string-closing quote.
    assert BREAKOUT not in body
    assert "&#x27;);alert(1);//" not in body
    assert "alert(1)" not in body


@pytest.mark.django_db
def test_assets_owner_is_normalised_to_the_known_enum(client, django_user_model):
    """Any unrecognised ?owner= collapses to the safe default rather than being echoed."""
    client.force_login(_user(django_user_model, 728002))
    for bogus in ("wat", "corp; drop", "<script>", "mine'", ""):
        resp = client.get(reverse("stockpile:assets"), {"owner": bogus})
        assert resp.status_code == 200
        assert resp.context["owner"] in ("mine", "corp")


@pytest.mark.django_db
def test_assets_owner_corp_still_works_for_officers(client, django_user_model):
    """The normalisation must not break the legitimate corp view for an officer."""
    client.force_login(_user(django_user_model, 728003, role=rbac.ROLE_OFFICER))
    resp = client.get(reverse("stockpile:assets"), {"owner": "corp"})
    assert resp.status_code == 200
    assert resp.context["owner"] == "corp"


@pytest.mark.django_db
def test_assets_owner_corp_still_downgrades_for_non_officers(client, django_user_model):
    """A member asking for corp assets is still downgraded to their own holdings."""
    client.force_login(_user(django_user_model, 728004))
    resp = client.get(reverse("stockpile:assets"), {"owner": "corp"})
    assert resp.status_code == 200
    assert resp.context["owner"] == "mine"


# --- Object-level authorisation on Tocha's Lab promote ----------------------
# Every other object route in apps/fitting/views.py re-checks fit.can_view()/can_edit()
# after resolving the pk. ``promote`` checked only the actor's ROLE, so an officer could
# POST any pk and have services.promote_to_doctrine copy that fit's hull+modules into the
# corp doctrine library — and flip the owner's row to visibility=DOCTRINE, republishing a
# PRIVATE fit corp-wide. Fit.can_view has no officer branch: PRIVATE (the model default)
# is owner-and-superuser only.
def _private_fit(owner, ship_type_id=587):
    from apps.fitting.models import Fit, FitRevision

    fit = Fit.objects.create(owner=owner, name="secret rifter", ship_type_id=ship_type_id)
    rev = FitRevision.objects.create(fit=fit, revision_number=1, ship_type_id=ship_type_id,
                                     items=[], created_by=owner)
    fit.current_revision = rev
    fit.save(update_fields=["current_revision"])
    return fit


@pytest.mark.django_db
def test_officer_cannot_promote_another_pilots_private_fit(client, django_user_model):
    from apps.doctrines.models import Doctrine
    from apps.fitting.models import Fit, Visibility

    victim = _user(django_user_model, 728010)
    fit = _private_fit(victim)
    assert fit.visibility == Visibility.PRIVATE

    officer = _user(django_user_model, 728011, role=rbac.ROLE_OFFICER)
    doctrine = Doctrine.objects.create(name="Armor HAC")
    client.force_login(officer)
    resp = client.post(reverse("fitting:promote", args=[fit.pk]), {"doctrine": doctrine.pk})

    assert resp.status_code == 404, "an officer must not reach another pilot's PRIVATE fit"
    fit.refresh_from_db()
    assert fit.visibility == Visibility.PRIVATE, "the victim's fit must not be republished"
    assert fit.promoted_doctrine_fit_id is None
    assert doctrine.fits.count() == 0, "no doctrine fit may be created from a private fit"
    # And the fit itself must be untouched.
    assert Fit.objects.get(pk=fit.pk).name == "secret rifter"


@pytest.mark.django_db
def test_officer_cannot_promote_another_pilots_corp_visible_fit(client, django_user_model):
    """Being allowed to READ a fit is not consent to have it permanently republished.

    Promotion rewrites the owner's row (visibility -> DOCTRINE, promoted_doctrine_fit_id)
    and there is no un-promote, so a can_view check is too weak here: it would still let an
    officer take any member's corp-shared fit. The gate is ownership.
    """
    from apps.doctrines.models import Doctrine
    from apps.fitting.models import Visibility

    author = _user(django_user_model, 728012)
    fit = _private_fit(author, ship_type_id=624)
    fit.visibility = Visibility.CORPORATION
    fit.save(update_fields=["visibility"])

    officer = _user(django_user_model, 728013, role=rbac.ROLE_OFFICER)
    doctrine = Doctrine.objects.create(name="Shield Kite")
    client.force_login(officer)
    resp = client.post(reverse("fitting:promote", args=[fit.pk]), {"doctrine": doctrine.pk})

    assert resp.status_code == 404
    fit.refresh_from_db()
    assert fit.visibility == Visibility.CORPORATION
    assert fit.promoted_doctrine_fit_id is None
    assert doctrine.fits.count() == 0


# --- Audit-log date filter must stay index-seekable -------------------------
@pytest.mark.django_db
def test_audit_log_date_filter_is_inclusive_and_uses_no_date_cast(client, django_user_model):
    """Boundary rows on both endpoint days must be included, and no DATE() cast emitted.

    The old ``created_at__date__gte/lte`` pair was inclusive but wrapped the indexed column
    in DATE(), so Postgres could not seek the index. The half-open range must keep exactly
    the same rows.
    """
    import datetime as dt

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.admin_audit.models import AuditLog

    stamps = {
        "before": dt.datetime(2026, 5, 31, 23, 59, tzinfo=dt.UTC),
        "from_edge": dt.datetime(2026, 6, 1, 0, 0, tzinfo=dt.UTC),
        "middle": dt.datetime(2026, 6, 2, 13, 0, tzinfo=dt.UTC),
        "to_edge": dt.datetime(2026, 6, 3, 23, 59, tzinfo=dt.UTC),
        "after": dt.datetime(2026, 6, 4, 0, 0, tzinfo=dt.UTC),
    }
    for label, when in stamps.items():
        row = AuditLog.objects.create(action=f"act-{label}")
        AuditLog.objects.filter(pk=row.pk).update(created_at=when)  # bypass auto_now_add

    client.force_login(_user(django_user_model, 728040, role=rbac.ROLE_DIRECTOR))
    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(reverse("admin_audit:audit"), {"from": "2026-06-01", "to": "2026-06-03"})
    assert resp.status_code == 200

    actions = {row.action for row in resp.context["logs"]}
    assert actions == {"act-from_edge", "act-middle", "act-to_edge"}, actions

    audit_sql = [q["sql"] for q in ctx.captured_queries if "admin_audit_auditlog" in q["sql"]]
    assert audit_sql, "expected the audit query to be captured"
    assert not any("DATE(" in s.upper() for s in audit_sql), (
        "created_at is still wrapped in a DATE() cast, which cannot use its index"
    )


# --- Public campaign permalink must not leak the member-only fleet op -------
@pytest.mark.django_db
def test_public_campaign_permalink_hides_the_linked_operation(client):
    import datetime as dt

    from apps.killboard.models import CombatCampaign
    from apps.operations.models import Operation

    op = Operation.objects.create(name="Op Blackout Strike", type=Operation.Type.PVP)
    camp = CombatCampaign.objects.create(
        name="Winter Push", start_time=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        visibility=CombatCampaign.Visibility.PUBLIC, operation=op,
    )

    resp = client.get(reverse("killboard:campaign_public", args=[camp.slug]))
    assert resp.status_code == 200
    assert resp.context["operation"] is None
    assert b"Op Blackout Strike" not in resp.content


@pytest.mark.django_db
def test_member_campaign_page_still_shows_the_operation(client, django_user_model):
    """The member-facing view keeps the overlay — only the anonymous projection drops it."""
    import datetime as dt

    from apps.killboard.models import CombatCampaign
    from apps.operations.models import Operation

    op = Operation.objects.create(name="Op Blackout Strike", type=Operation.Type.PVP)
    camp = CombatCampaign.objects.create(
        name="Winter Push", start_time=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        visibility=CombatCampaign.Visibility.PUBLIC, operation=op,
    )
    client.force_login(_user(django_user_model, 728030, role=rbac.ROLE_OFFICER))
    resp = client.get(reverse("killboard:campaign_detail", args=[camp.pk]))
    assert resp.status_code == 200
    assert resp.context["operation"] == op


# --- Member-gated CV card must not be stored by a shared cache --------------
@pytest.mark.django_db
def test_member_cv_card_is_not_publicly_cacheable(client, django_user_model):
    from apps.killboard import views as kb_views

    resp = kb_views._png_response(b"\x89PNG", cache_hit=False, public=False)
    assert "public" not in resp["Cache-Control"]
    assert "no-store" in resp["Cache-Control"]
    assert resp["Vary"] == "Cookie"
    # The genuinely public kill card keeps its edge cache.
    pub = kb_views._png_response(b"\x89PNG", cache_hit=False)
    assert pub["Cache-Control"] == "public, max-age=300"


# --- Anonymous API throttle must key on the real client IP ------------------
@pytest.mark.django_db
def test_anon_api_throttle_ignores_spoofed_forwarded_for():
    """A client-supplied X-Forwarded-For must not mint a fresh throttle bucket."""
    from django.test import RequestFactory

    from apps.killboard.api.throttling import KillboardAnonThrottle

    rf = RequestFactory()
    throttle = KillboardAnonThrottle()
    # nginx APPENDS the real peer, so the right-most entry is the only trustworthy one.
    r1 = rf.get("/api/killboard/", HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.9",
                REMOTE_ADDR="10.0.0.1")
    r2 = rf.get("/api/killboard/", HTTP_X_FORWARDED_FOR="9.9.9.9, 203.0.113.9",
                REMOTE_ADDR="10.0.0.1")
    assert throttle.get_ident(r1) == "203.0.113.9"
    assert throttle.get_ident(r1) == throttle.get_ident(r2), (
        "varying the spoofable left-hand XFF entry must not change the throttle bucket"
    )


# --- CSV formula injection: the control is now structural -------------------
def test_safe_csv_writer_neutralises_formula_triggers():
    import io

    from core.exporting import safe_csv_writer

    buf = io.StringIO()
    safe_csv_writer(buf).writerow(["=cmd|'/c calc'!A1", "+1+1", "@SUM(A1)", "plain"])
    line = buf.getvalue()
    for payload in ("'=cmd", "'+1+1", "'@SUM"):
        assert payload in line, f"{payload} not neutralised: {line!r}"
    assert "'plain" not in line, "harmless text must not be mangled"


def test_safe_csv_writer_keeps_numbers_numeric():
    """A number cannot be a formula; quoting -5 would silently turn it into text."""
    import io
    from decimal import Decimal

    from core.exporting import safe_csv_writer

    buf = io.StringIO()
    safe_csv_writer(buf).writerow([-5, Decimal("-12.50"), -3.5, 7])
    assert buf.getvalue().strip() == "-5,-12.50,-3.5,7"


def test_no_export_writes_csv_without_neutralisation():
    """Structural guard: this bug class regressed five times, so pin the invariant.

    Any module that builds a CSV must go through core.exporting — either the
    safe_csv_writer wrapper, or an explicit per-cell csv_safe/_csv_safe call.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in list((root / "apps").rglob("*.py")) + list((root / "core").rglob("*.py")):
        if "/migrations/" in str(path):
            continue
        src = path.read_text()
        if not re.search(r"\bcsv\.writer\(", src):
            continue
        if not re.search(r"csv_safe", src):
            offenders.append(str(path.relative_to(root)))
    assert offenders == [], (
        "these modules build a CSV with a raw csv.writer and no neutralisation — "
        f"use core.exporting.safe_csv_writer: {offenders}"
    )


# --- Killmail detail must not rebuild the whole market price index ----------
# The page used build_price_index(), which loads EVERY MarketPrice row for both profiles
# per call. price_for() resolves identically off a 300s process-local snapshot.
@pytest.mark.django_db
def test_killmail_detail_does_not_scan_the_whole_price_table(client, django_user_model):
    import datetime as dt
    from decimal import Decimal

    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    from apps.killboard.models import Killmail
    from apps.market.models import MarketPrice
    from apps.market.pricing import price_for, reset_price_cache

    for tid in range(9000, 9060):  # a stand-in for the real ~15k-40k row table
        MarketPrice.objects.create(type_id=tid, location=None,
                                   profile=MarketPrice.Profile.JITA_SELL,
                                   sell_min=Decimal("1000"))
    Killmail.objects.create(
        killmail_id=728900, killmail_hash="h728900",
        killmail_time=dt.datetime(2026, 6, 1, 12, tzinfo=dt.UTC),
        solar_system_id=30000142, victim_ship_type_id=9000,
        total_value=Decimal("1000"), involves_home_corp=True,
        home_corp_role=Killmail.HomeRole.VICTIM,
    )

    client.force_login(_user(django_user_model, 728020))
    reset_price_cache()
    price_for(9000)  # warm the shared snapshot, as a live process would be

    with CaptureQueriesContext(connection) as ctx:
        resp = client.get(reverse("killboard:detail", args=[728900]))
    assert resp.status_code == 200
    price_scans = [q["sql"] for q in ctx.captured_queries if "market_marketprice" in q["sql"]]
    assert price_scans == [], (
        f"detail page still queries the price table {len(price_scans)}x; "
        "it must resolve from the cached snapshot"
    )


@pytest.mark.django_db
def test_owner_can_still_promote_their_own_private_fit(client, django_user_model):
    """can_view() admits the owner, so an officer promoting their OWN fit still works."""
    from apps.doctrines.models import Doctrine

    officer = _user(django_user_model, 728014, role=rbac.ROLE_OFFICER)
    fit = _private_fit(officer, ship_type_id=621)
    doctrine = Doctrine.objects.create(name="Solo Roam")
    client.force_login(officer)
    resp = client.post(reverse("fitting:promote", args=[fit.pk]), {"doctrine": doctrine.pk})

    assert resp.status_code == 302
    assert doctrine.fits.count() == 1
