"""Industrial ERP: claim, deliver→stock+credit, blueprint coverage."""
from __future__ import annotations

import contextlib

import pytest
from django.utils import translation

from apps.doctrines.models import Doctrine, DoctrineCategory, DoctrineFit
from apps.erp import services
from apps.erp.models import Blueprint, BuildJob
from apps.identity.models import RoleAssignment
from apps.pilots.models import ContributionEvent
from apps.sso.services import ensure_role
from apps.stockpile.models import Stockpile, StockpileItem
from core import rbac

RIFTER = 587


def _member(django_user_model, name, *roles):
    user = django_user_model.objects.create(username=name)
    for r in roles:
        RoleAssignment.objects.create(user=user, role=ensure_role(r))
    return user


@pytest.mark.django_db
def test_claim_then_deliver_updates_stock_and_credits(django_user_model, sde):
    builder = _member(django_user_model, "builder", rbac.ROLE_MEMBER)
    job = BuildJob.objects.create(output_type_id=RIFTER, quantity=5)

    assert services.claim(job, builder) is True
    job.refresh_from_db()
    assert job.owner_id == builder.id and job.status == BuildJob.Status.BUILDING
    # Second claimer loses the race.
    assert services.claim(job, _member(django_user_model, "other", rbac.ROLE_MEMBER)) is False

    services.deliver(job, builder)
    job.refresh_from_db()
    assert job.status == BuildJob.Status.DELIVERED
    # Corp stock gained 5 hulls.
    item = StockpileItem.objects.get(stockpile__kind=Stockpile.Kind.CORP, type_id=RIFTER)
    assert item.quantity_current == 5
    # Builder credited once (idempotent).
    assert ContributionEvent.objects.filter(
        user=builder, kind="build", ref_type="build_job", ref_id=str(job.pk)
    ).count() == 1


@pytest.mark.django_db
def test_blueprint_coverage_flags_missing_hull(django_user_model, sde):
    cat = DoctrineCategory.objects.create(key="dps", label="DPS")
    d = Doctrine.objects.create(name="Rifter Fleet", category=cat)
    DoctrineFit.objects.create(doctrine=d, name="Rifter", ship_type_id=RIFTER)

    cov = services.blueprint_coverage()
    assert any(g["type_id"] == RIFTER for g in cov["gaps"])

    Blueprint.objects.create(
        owner_type=Blueprint.Owner.CORPORATION, type_id=999, product_type_id=RIFTER
    )
    cov = services.blueprint_coverage()
    assert any(c["type_id"] == RIFTER for c in cov["covered"])
    assert not any(g["type_id"] == RIFTER for g in cov["gaps"])


@pytest.mark.django_db
def test_create_job_is_officer_only(client, django_user_model, sde):
    client.force_login(_member(django_user_model, "m", rbac.ROLE_MEMBER))
    assert client.post("/erp/jobs/create/", {"output_type_id": RIFTER, "quantity": 2}).status_code == 403
    # /erp/ now consolidates into the Industry Center Job Tracker.
    assert client.get("/erp/").status_code == 302
    client.force_login(_member(django_user_model, "fc", rbac.ROLE_OFFICER))
    assert client.post("/erp/jobs/create/", {"output_type_id": RIFTER, "quantity": 2}).status_code == 302
    assert BuildJob.objects.filter(output_type_id=RIFTER).exists()


@pytest.mark.django_db
def test_job_blocks_when_materials_short_then_unblocks(django_user_model, sde):
    job = BuildJob.objects.create(output_type_id=RIFTER, quantity=1)
    # No corp stock yet: a buildable job whose materials are short is flagged BLOCKED.
    services.recheck_block(job)
    job.refresh_from_db()
    assert job.status == BuildJob.Status.BLOCKED
    assert job.blocked_reason
    # A blocked job cannot be claimed.
    assert services.claim(job, _member(django_user_model, "b1", rbac.ROLE_MEMBER)) is False
    # Stock the full BOM into corp stock: the job returns to QUEUED and is claimable.
    sp = Stockpile.objects.create(name="Corp", kind=Stockpile.Kind.CORP)
    for line in services.job_materials(job)["lines"]:
        StockpileItem.objects.create(
            stockpile=sp, type_id=line["type_id"], quantity_current=line["need"]
        )
    services.recheck_block(job)
    job.refresh_from_db()
    assert job.status == BuildJob.Status.QUEUED
    assert job.blocked_reason == ""
    assert services.claim(job, _member(django_user_model, "b2", rbac.ROLE_MEMBER)) is True


# ===========================================================================
#  Seam B: prose is PERSISTED by one pilot (or a worker), then read back by
#  every OTHER pilot in the language *they* chose.
# ===========================================================================
_MISSING = object()


@contextlib.contextmanager
def _translated_de(**msgstrs):
    """Activate ``de`` with ``msgstrs`` genuinely translated, then restore the catalogue.

    The shipped ``de`` catalogue has no msgstr for these scaffolds *yet*, so a plain
    ``translation.override("de")`` would still hand back the English msgid and the bug would stay
    invisible. Seeding the msgstrs here is exactly what a translator filling in that .po entry
    does — it makes the seam testable now, and pins the invariant so it cannot regress later.
    """
    from django.utils.translation import trans_real

    with translation.override("de"):
        catalog = trans_real.catalog()._catalog
        saved = {k: catalog.get(k, _MISSING) for k in msgstrs}
        for key, value in msgstrs.items():
            catalog[key] = value
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is _MISSING:
                    catalog._catalogs[0].pop(key, None)
                else:
                    catalog[key] = value


_DE_SHORT = {
    "Short: %(materials)s": "Fehlt: %(materials)s",
    "Short: %(materials)s…": "Fehlt: %(materials)s…",
}


@pytest.mark.django_db
def test_blocked_reason_renders_under_the_readers_locale(sde):
    # WRITE side (the job board / the Plan→Job bridge / a worker): no reader, so no locale.
    job = BuildJob.objects.create(output_type_id=RIFTER, quantity=1)
    services.recheck_block(job)
    job.refresh_from_db()
    assert job.status == BuildJob.Status.BLOCKED
    # English behaviour is byte-for-byte unchanged, and the key + raw params ride alongside.
    assert job.blocked_reason.startswith("Short: ")
    assert job.blocked_reason_key in ("job.blocked_short", "job.blocked_short_truncated")
    materials = job.blocked_reason_params["materials"]
    assert isinstance(materials, str) and materials  # EVE type names: raw, never translated
    assert job.blocked_reason == f"Short: {materials}" or job.blocked_reason == f"Short: {materials}…"

    # A German officer's request writing the row must NOT freeze German into it — that is the
    # whole trap a naive gettext() at the write site would fall into.
    with _translated_de(**_DE_SHORT):
        other = BuildJob.objects.create(output_type_id=RIFTER, quantity=1)
        services.recheck_block(other)
    other.refresh_from_db()
    assert other.blocked_reason == job.blocked_reason  # still English in the database

    # READ side: a German pilot loading the board sees GERMAN, re-rendered from the key, with the
    # EVE material names left raw.
    with _translated_de(**_DE_SHORT):
        fresh = BuildJob.objects.get(pk=job.pk)
        assert fresh.blocked_reason_i18n == job.blocked_reason.replace("Short:", "Fehlt:", 1)
    # ...and English readers still get the stored English.
    assert BuildJob.objects.get(pk=job.pk).blocked_reason_i18n == job.blocked_reason


@pytest.mark.django_db
def test_plan_note_renders_under_the_readers_locale(sde):
    from apps.industry.models import IndustryProject

    corp_plan = IndustryProject.objects.create(
        name="Ferox batch 7", visibility=IndustryProject.Visibility.CORP
    )
    job = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=1, **services.plan_note_fields(corp_plan)
    )
    assert job.note == "From plan: Ferox batch 7"  # English unchanged
    assert job.note_key == "job.from_plan"
    assert job.note_params == {"plan": "Ferox batch 7"}

    secret_plan = IndustryProject.objects.create(
        name="Op Deadfall", visibility=IndustryProject.Visibility.LEADERSHIP
    )
    quiet = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=1, **services.plan_note_fields(secret_plan)
    )
    assert quiet.note == "From a leadership plan"
    assert quiet.note_params == {}  # the plan's name is still not leaked to the board

    with _translated_de(**{
        "From plan: %(plan)s": "Aus Plan: %(plan)s",
        "From a leadership plan": "Aus einem Führungsplan",
    }):
        # The plan name is corp content: interpolated raw, never translated.
        assert BuildJob.objects.get(pk=job.pk).note_i18n == "Aus Plan: Ferox batch 7"
        assert BuildJob.objects.get(pk=quiet.pk).note_i18n == "Aus einem Führungsplan"


@pytest.mark.django_db
def test_legacy_rows_without_a_key_render_their_stored_english_verbatim(sde):
    # A row written before this landed carries no key, and nothing is backfilled. It must degrade
    # to its stored English — never to blank. Same for a pilot's hand-typed note (free text is
    # never translated).
    legacy = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=1,
        status=BuildJob.Status.BLOCKED, blocked_reason="Short: Tritanium", note="need by Friday",
    )
    assert legacy.blocked_reason_key == "" and legacy.note_key == ""
    with _translated_de(**_DE_SHORT):
        row = BuildJob.objects.get(pk=legacy.pk)
        assert row.blocked_reason_i18n == "Short: Tritanium"
        assert row.note_i18n == "need by Friday"


@pytest.mark.django_db
def test_editing_a_plan_jobs_note_drops_the_stale_scaffold_key(sde):
    from apps.industry.models import IndustryProject

    plan = IndustryProject.objects.create(
        name="Ferox batch 7", visibility=IndustryProject.Visibility.CORP
    )
    job = BuildJob.objects.create(
        output_type_id=RIFTER, quantity=1, **services.plan_note_fields(plan)
    )
    # A pilot overwrites the bridge's note with their own words: the key must go with it, or the
    # board would keep re-rendering "From plan: …" over what they actually typed.
    services.update_quantity(job, 3, note="need by Friday")
    job.refresh_from_db()
    assert job.note == "need by Friday"
    assert job.note_key == "" and job.note_params == {}
    with _translated_de(**{"From plan: %(plan)s": "Aus Plan: %(plan)s"}):
        assert BuildJob.objects.get(pk=job.pk).note_i18n == "need by Friday"
