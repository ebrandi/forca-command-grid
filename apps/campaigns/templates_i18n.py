"""Render-time i18n seam for the built-in campaign blueprints — "translate until edited".

The blueprints in :mod:`apps.campaigns.templates_builtin` are shipped English **content**: the seed
migration writes them into ``CampaignTemplate`` rows, and
:func:`apps.campaigns.services.instantiate_template` copies that prose *again* into the
``Campaign`` / ``Workstream`` / ``Objective`` / ``Milestone`` / ``Risk`` rows an officer then edits.

Wrapping the source dicts in ``gettext_lazy`` cannot work: Django coerces a lazy proxy to ``str``
on ``.save()``, so whichever locale happened to be active at seed time would be frozen into the row
forever. So the English stays in the database (it is the fallback *and* the audit record) and the
translations live here, in a code-side catalogue keyed by the stable built-in key:

* :data:`BUILTIN_MSGIDS` — ``{source_key: {field: gettext_lazy msgid}}``. Real literal ``_("…")``
  calls, so ``xgettext`` sees them (a ``_(f"…")`` or ``_(variable)`` would be a silent no-op).
  They are resolved with ``str(...)`` at *render* time, under the request's active locale — never
  at write time.
* :data:`BUILTIN_ENGLISH` — the same table derived from ``templates_builtin`` at import, holding
  the *shipped English* (never a msgid). It is what a stored value is compared against.

:func:`render` is the seam:

    stored text == the shipped English  →  still the built-in's words  →  return the TRANSLATION
    stored text != the shipped English  →  an officer edited it  →  return it VERBATIM, untranslated

That comparison **is** the "has this been edited?" test, which is why there is no ``edited``
boolean column: a flag would have to be maintained by every write path (views, services, admin,
shell, future data migrations) and would silently rot the first time one forgot. The stored text
cannot rot — it either is the shipped English or it is not.

Never translated, because they are identifiers rather than prose: ``key`` / workstream ``key`` /
``category`` / ``metric_source`` / ``direction`` / ``probability`` / ``impact`` — all compared,
looked up or constrained on. EVE game data (hull, system, skill names) stays English by policy; a
sentence that merely *contains* one is still translatable prose.
"""
from __future__ import annotations

from django.core.exceptions import FieldDoesNotExist
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from . import templates_builtin


# --------------------------------------------------------------------------- #
#  Stable keys — the single definition of the ``source_key`` format.
#  ``instantiate_template`` stamps these onto the rows it copies prose into, and the catalogue
#  below is keyed by the same strings. Objectives/milestones/risks are keyed by their *index* in
#  the blueprint list (a blueprint carries no per-item key); an operator who reorders or edits a
#  built-in blueprint therefore shifts the mapping — which is harmless, because the stored-English
#  comparison in :func:`render` then fails and the row renders verbatim. A stale key can never
#  produce a *wrong* translation, only no translation.
# --------------------------------------------------------------------------- #
def template_key(key: str) -> str:
    return key


def workstream_key(key: str, ws_key: str) -> str:
    return f"{key}.ws.{ws_key}"


def objective_key(key: str, index: int) -> str:
    return f"{key}.obj.{index}"


def milestone_key(key: str, index: int) -> str:
    return f"{key}.ms.{index}"


def risk_key(key: str, index: int) -> str:
    return f"{key}.risk.{index}"


# --------------------------------------------------------------------------- #
#  The catalogue: one gettext msgid per translatable built-in field (206 strings).
#  Keep it in lockstep with ``templates_builtin`` — ``tests/test_builtin_template_i18n.py`` fails
#  if any msgid drifts from the shipped English, or if a translatable field has no msgid.
# --------------------------------------------------------------------------- #
BUILTIN_MSGIDS: dict[str, dict[str, str]] = {
    "armour_bs_deployment": {
        "name": _("Establish Armour Battleship Deployment Readiness"),
        "description": _(
            "The reference deployment-readiness campaign: qualify pilots into the armour battleship doctrine, "
            "stage fitted hulls and consumables, secure an SRP reserve, prove the logistics route and finish "
            "with a live readiness exercise. Fill in the doctrine, stockpile, wallet and operation selections "
            "after creating — every id is left blank on purpose."
        ),
        "summary": _("Stand up an armour battleship fleet in the new staging within 30 days."),
        "rationale": _(
            "A credible armour battleship fleet in staging is the deterrent this deployment turns on — pilots "
            "trained, hulls fitted and staged, SRP funded, and the supply route proven before we commit."
        ),
        "desired_outcome": _(
            "An armour battleship fleet that can undock at strength from the new staging, with trained "
            "pilots, staged replacements, a funded SRP reserve and a validated logistics route."
        ),
        "success_criteria": _(
            "35 mainline + 12 logistics pilots qualified; 50 mainline and 15 logistics hulls fitted and "
            "staged; 30 replacement hulls and 5 fleets' consumables stocked; ≥3 FCs across TZs; route "
            "validated; SRP reserve funded; final readiness exercise passed."
        ),
        "failure_criteria": _(
            "Deployment date reached without a fleet that can field mainline + logistics at strength, or with "
            "no funded SRP reserve / no proven supply route."
        ),
    },
    "armour_bs_deployment.ws.doctrine": {
        "name": _("Doctrine & fitting"),
        "description": _("Agree, approve and publish the armour battleship doctrine and fits."),
    },
    "armour_bs_deployment.ws.training": {
        "name": _("Training"),
        "description": _("Qualify pilots into the doctrine and run training fleets."),
    },
    "armour_bs_deployment.ws.manufacturing": {
        "name": _("Manufacturing"),
        "description": _("Build and fit the mainline, logistics and replacement hulls."),
    },
    "armour_bs_deployment.ws.procurement": {
        "name": _("Procurement"),
        "description": _("Source ammunition, consumables and modules for the fleets."),
    },
    "armour_bs_deployment.ws.logistics": {
        "name": _("Logistics"),
        "description": _("Validate the route and move staged assets before the deadline."),
    },
    "armour_bs_deployment.ws.finance": {
        "name": _("Finance & SRP"),
        "description": _("Fund and hold the agreed SRP reserve for the deployment."),
    },
    "armour_bs_deployment.ws.fc_coverage": {
        "name": _("FC coverage"),
        "description": _("Ensure qualified fleet commanders across the covered timezones."),
    },
    "armour_bs_deployment.ws.communications": {
        "name": _("Communications"),
        "description": _("Brief the corp, publish the schedule and keep pilots informed."),
    },
    "armour_bs_deployment.ws.final_validation": {
        "name": _("Final validation"),
        "description": _("Run the final readiness exercise and sign the fleet off."),
    },
    "armour_bs_deployment.obj.0": {
        "title": _("Qualify 35 mainline doctrine pilots"),
        "description": _(
            "Pilots flying the mainline armour battleship fit. Set the doctrine to measure after creating the "
            "campaign."
        ),
        "unit": pgettext_lazy("objective unit", "pilots"),
    },
    "armour_bs_deployment.obj.1": {
        "title": _("Qualify 12 logistics pilots"),
        "description": _(
            "Pilots flying the fleet logistics fit. Set the logistics doctrine to measure after creating the "
            "campaign."
        ),
        "unit": pgettext_lazy("objective unit", "pilots"),
    },
    "armour_bs_deployment.obj.2": {
        "title": _("Stage 50 fitted mainline hulls"),
        "description": _(
            "Fitted mainline hulls on hand in the staging structure. Point this at the staging stockpile and "
            "hull type after creating."
        ),
        "unit": pgettext_lazy("objective unit", "ships"),
    },
    "armour_bs_deployment.obj.3": {
        "title": _("Stage 15 fitted logistics hulls"),
        "description": _(
            "Fitted logistics hulls on hand in staging. Point this at the staging stockpile and logistics "
            "hull type after creating."
        ),
        "unit": pgettext_lazy("objective unit", "ships"),
    },
    "armour_bs_deployment.obj.4": {
        "title": _("Build 30 replacement hulls"),
        "description": _("Spare hulls held against losses. Point this at the replacement stockpile after creating."),
        "unit": pgettext_lazy("objective unit", "hulls"),
    },
    "armour_bs_deployment.obj.5": {
        "title": _("Stock ammunition & consumables for 5 fleets"),
        "description": _(
            "Ammo, charges and consumables sufficient for five fleet outings. Point this at the consumables "
            "stockpile after creating."
        ),
        "unit": pgettext_lazy("objective unit", "sets"),
    },
    "armour_bs_deployment.obj.6": {
        "title": _("Confirm 3 qualified FCs across timezones"),
        "description": _(
            "Fleet commanders able to run the doctrine, spread across the covered timezones. Tracked manually "
            "and verified."
        ),
        "unit": pgettext_lazy("objective unit", "FCs"),
    },
    "armour_bs_deployment.obj.7": {
        "title": _("Validate the logistics route"),
        "description": _(
            "A confirmed, tested haul route into the staging system. Tracked manually (0 = untested, 1 = "
            "validated) and verified."
        ),
        "unit": pgettext_lazy("objective unit", "route"),
    },
    "armour_bs_deployment.obj.8": {
        "title": _("Move staged assets before the deadline"),
        "description": _("Percentage of the staging move completed. Tracked manually."),
        "unit": pgettext_lazy("objective unit", "%"),
    },
    "armour_bs_deployment.obj.9": {
        "title": _("Secure the agreed SRP reserve"),
        "description": _(
            "SRP reserve (allocated − spent − exposure) held for the deployment. Set the reserve target and "
            "period after creating; visible to finance only."
        ),
        "unit": pgettext_lazy("objective unit", "ISK"),
    },
    "armour_bs_deployment.obj.10": {
        "title": _("Run 3 training fleets"),
        "description": _(
            "Completed training operations of the doctrine. Pick the operation type to count after creating."
        ),
        "unit": pgettext_lazy("objective unit", "fleets"),
    },
    "armour_bs_deployment.obj.11": {
        "title": _("Complete 1 final readiness exercise"),
        "description": _("A full dress-rehearsal fleet sign-off. Tracked manually and verified."),
        "unit": pgettext_lazy("objective unit", "exercise"),
    },
    "armour_bs_deployment.ms.0": {
        "title": _("Doctrine approved & published"),
    },
    "armour_bs_deployment.ms.1": {
        "title": _("SRP reserve secured"),
    },
    "armour_bs_deployment.ms.2": {
        "title": _("First training fleet complete"),
    },
    "armour_bs_deployment.ms.3": {
        "title": _("Manufacturing line running"),
    },
    "armour_bs_deployment.ms.4": {
        "title": _("Assets staged in system"),
    },
    "armour_bs_deployment.ms.5": {
        "title": _("Final readiness exercise"),
    },
    "armour_bs_deployment.risk.0": {
        "description": _("Insufficient logistics pilots qualified in time"),
        "mitigation": _("Run dedicated logi training fleets; recruit from allied corps."),
        "contingency": _("Deploy with a reduced logi wing and a fitted-ship reserve."),
        "trigger": _("Fewer than 8 logi pilots two weeks out."),
    },
    "armour_bs_deployment.risk.1": {
        "description": _("Missing mainline hulls at staging"),
        "mitigation": _("Front-load the build queue; buy hulls to cover the shortfall."),
        "contingency": _("Charter freight of pre-built hulls from trade hubs."),
        "trigger": _("Staged hulls below 60% of target at week three."),
    },
    "armour_bs_deployment.risk.2": {
        "description": _("Ammunition / consumable material shortage"),
        "mitigation": _("Place standing buy orders early; diversify suppliers."),
        "contingency": _("Ration consumables to priority fleets."),
        "trigger": _("Any consumable line below one fleet's worth."),
    },
    "armour_bs_deployment.risk.3": {
        "description": _("Inadequate SRP reserve"),
        "mitigation": _("Ring-fence a wallet division; schedule donations drive."),
        "contingency": _("Cap SRP payouts to hull-only until the reserve recovers."),
        "trigger": _("Reserve below the agreed floor at any point."),
    },
    "armour_bs_deployment.risk.4": {
        "description": _("No FC available in a covered timezone"),
        "mitigation": _("Train back-up FCs; publish an FC roster."),
        "contingency": _("Consolidate fleets into covered timezones only."),
        "trigger": _("A timezone with zero qualified FCs one week out."),
    },
    "armour_bs_deployment.risk.5": {
        "description": _("Delayed freight into the staging system"),
        "mitigation": _("Validate the route early; pre-position a hauler."),
        "contingency": _("Fall back to the secondary route or jump freighters."),
        "trigger": _("Any critical shipment stalled beyond 48h."),
    },
    "armour_bs_deployment.risk.6": {
        "description": _("Incomplete doctrine approval"),
        "mitigation": _("Lock the fit sign-off as the first milestone."),
        "contingency": _("Publish a provisional fit and iterate."),
        "trigger": _("Doctrine unapproved past the week-one milestone."),
    },
    "doctrine_rollout": {
        "name": _("Doctrine Rollout"),
        "description": _(
            "Train the corp into a new doctrine: publish the fit, qualify pilots and prove it in training "
            "fleets."
        ),
        "summary": _("Get the corp flying a new doctrine within a month."),
        "desired_outcome": _("A published doctrine with enough qualified, practised pilots to field it."),
        "success_criteria": _("Doctrine approved; target pilots qualified; training fleets run."),
    },
    "doctrine_rollout.ws.doctrine": {
        "name": _("Doctrine & fitting"),
    },
    "doctrine_rollout.ws.training": {
        "name": _("Training"),
    },
    "doctrine_rollout.ws.communications": {
        "name": _("Communications"),
    },
    "doctrine_rollout.obj.0": {
        "title": _("Approve & publish the doctrine"),
        "description": _("Agree the fit and publish it. Tracked manually (0/1) and verified."),
        "unit": pgettext_lazy("objective unit", "doctrine"),
    },
    "doctrine_rollout.obj.1": {
        "title": _("Qualify 20 pilots"),
        "description": _("Pilots flying the doctrine fit. Set the doctrine to measure after creating."),
        "unit": pgettext_lazy("objective unit", "pilots"),
    },
    "doctrine_rollout.obj.2": {
        "title": _("Run 3 training fleets"),
        "description": _("Completed training operations. Pick the operation type after creating."),
        "unit": pgettext_lazy("objective unit", "fleets"),
    },
    "doctrine_rollout.obj.3": {
        "title": _("Publish the training schedule"),
        "description": _("Schedule and briefing published to the corp. Tracked manually (0/1)."),
        "unit": pgettext_lazy("objective unit", "post"),
    },
    "doctrine_rollout.ms.0": {
        "title": _("Doctrine published"),
    },
    "doctrine_rollout.ms.1": {
        "title": _("Halfway on qualifications"),
    },
    "doctrine_rollout.risk.0": {
        "description": _("Low pilot uptake of the doctrine"),
        "mitigation": _("Incentivise with SRP and training fleets."),
        "trigger": _("Under half the target qualified at the halfway milestone."),
    },
    "doctrine_rollout.risk.1": {
        "description": _("Fit approval drags"),
        "mitigation": _("Time-box the sign-off to week one."),
    },
    "deployment_prep": {
        "name": _("Deployment Prep"),
        "description": _(
            "Prepare a timed deployment: stage assets, validate the route and brief the corp before the move."
        ),
        "summary": _("Ready the corp for a deployment in three weeks."),
        "desired_outcome": _("Assets staged, route proven and the corp briefed for the deployment."),
        "success_criteria": _("Route validated; assets staged; briefing published."),
    },
    "deployment_prep.ws.logistics": {
        "name": _("Logistics"),
    },
    "deployment_prep.ws.manufacturing": {
        "name": _("Manufacturing"),
    },
    "deployment_prep.ws.communications": {
        "name": _("Communications"),
    },
    "deployment_prep.obj.0": {
        "title": _("Validate the staging route"),
        "description": _("A tested haul route into staging. Tracked manually (0/1) and verified."),
        "unit": pgettext_lazy("objective unit", "route"),
    },
    "deployment_prep.obj.1": {
        "title": _("Stage 40 fitted hulls"),
        "description": _("Fitted hulls on hand in staging. Point this at the staging stockpile."),
        "unit": pgettext_lazy("objective unit", "ships"),
    },
    "deployment_prep.obj.2": {
        "title": _("Move staged assets"),
        "description": _("Percentage of the move completed. Tracked manually."),
        "unit": pgettext_lazy("objective unit", "%"),
    },
    "deployment_prep.obj.3": {
        "title": _("Publish the deployment briefing"),
        "description": _("Deployment plan and schedule published. Tracked manually (0/1)."),
        "unit": pgettext_lazy("objective unit", "post"),
    },
    "deployment_prep.ms.0": {
        "title": _("Route validated"),
    },
    "deployment_prep.ms.1": {
        "title": _("Assets staged"),
    },
    "deployment_prep.risk.0": {
        "description": _("Delayed freight into staging"),
        "mitigation": _("Pre-position a hauler; validate the route early."),
    },
    "deployment_prep.risk.1": {
        "description": _("Hulls not ready in time"),
        "mitigation": _("Front-load the build queue; buy to cover the gap."),
    },
    "stockpile_drive": {
        "name": _("Stockpile Drive"),
        "description": _(
            "Rebuild replacement stock: manufacture hulls, deliver builds and top up consumables to target "
            "levels."
        ),
        "summary": _("Restore doctrine stock to target levels."),
        "desired_outcome": _("Replacement hulls and consumables back at their target on-hand levels."),
        "success_criteria": _("Hull stock and consumable stock at or above target."),
    },
    "stockpile_drive.ws.manufacturing": {
        "name": _("Manufacturing"),
    },
    "stockpile_drive.ws.procurement": {
        "name": _("Procurement"),
    },
    "stockpile_drive.obj.0": {
        "title": _("Stock 50 replacement hulls"),
        "description": _("Hulls on hand against the replacement target. Point this at the stockpile."),
        "unit": pgettext_lazy("objective unit", "hulls"),
    },
    "stockpile_drive.obj.1": {
        "title": _("Deliver 50 built hulls in the window"),
        "description": _("Hulls delivered from industry in this window. Set the output type ids after creating."),
        "unit": pgettext_lazy("objective unit", "hulls"),
    },
    "stockpile_drive.obj.2": {
        "title": _("Top up consumables for 10 fleets"),
        "description": _("Consumable sets on hand. Point this at the consumables stockpile."),
        "unit": pgettext_lazy("objective unit", "sets"),
    },
    "stockpile_drive.ms.0": {
        "title": _("Build queue running"),
    },
    "stockpile_drive.ms.1": {
        "title": _("Halfway to target"),
    },
    "stockpile_drive.risk.0": {
        "description": _("Build material shortage"),
        "mitigation": _("Secure minerals ahead of the queue."),
    },
    "srp_recovery": {
        "name": _("SRP Reserve Recovery"),
        "description": _(
            "Restore a depleted SRP reserve: fund the reserve, run a donations drive and hold it above the "
            "agreed floor."
        ),
        "summary": _("Rebuild the SRP reserve to its agreed floor."),
        "desired_outcome": _("SRP reserve funded above the agreed floor and holding."),
        "success_criteria": _("Reserve at or above the target for the review period."),
    },
    "srp_recovery.ws.finance": {
        "name": _("Finance & SRP"),
    },
    "srp_recovery.ws.communications": {
        "name": _("Communications"),
    },
    "srp_recovery.obj.0": {
        "title": _("Fund the SRP reserve to target"),
        "description": _(
            "SRP reserve (allocated − spent − exposure). Set the target and period after creating; visible to "
            "finance only."
        ),
        "unit": pgettext_lazy("objective unit", "ISK"),
    },
    "srp_recovery.obj.1": {
        "title": _("Run a donations drive"),
        "description": _("Donations campaign published and running. Tracked manually (0/1)."),
        "unit": pgettext_lazy("objective unit", "drive"),
    },
    "srp_recovery.ms.0": {
        "title": _("Reserve halfway to floor"),
    },
    "srp_recovery.risk.0": {
        "description": _("Continued SRP losses outpace funding"),
        "mitigation": _("Cap payouts to hull-only until the reserve recovers."),
        "trigger": _("Reserve falling for two consecutive weeks."),
    },
    "member_integration": {
        "name": _("New Member Integration"),
        "description": _(
            "Bring a new-member intake up to speed: assign mentors, complete onboarding and get pilots into "
            "their first fleets."
        ),
        "summary": _("Integrate the new-member intake over six weeks."),
        "desired_outcome": _("New members mentored, onboarded and flying in fleets."),
        "success_criteria": _("Mentors assigned; onboarding complete; first fleets flown."),
    },
    "member_integration.ws.recruitment": {
        "name": _("Recruitment & mentorship"),
    },
    "member_integration.ws.training": {
        "name": _("Training"),
    },
    "member_integration.obj.0": {
        "title": _("Assign mentors to every new member"),
        "description": _("New members with an assigned mentor. Tracked manually."),
        "unit": pgettext_lazy("objective unit", "pilots"),
    },
    "member_integration.obj.1": {
        "title": _("Complete onboarding checklist"),
        "description": _("New members through the onboarding checklist. Tracked manually."),
        "unit": pgettext_lazy("objective unit", "pilots"),
    },
    "member_integration.obj.2": {
        "title": _("Fly first fleets"),
        "description": _("New members who have flown at least one fleet. Tracked manually."),
        "unit": pgettext_lazy("objective unit", "pilots"),
    },
    "member_integration.ms.0": {
        "title": _("Mentors assigned"),
    },
    "member_integration.ms.1": {
        "title": _("Onboarding complete"),
    },
    "member_integration.risk.0": {
        "description": _("New members go inactive before integrating"),
        "mitigation": _("Early mentor contact; welcome fleets in the first week."),
        "trigger": _("A new member with no activity for ten days."),
    },
}


def _english_catalogue() -> dict[str, dict[str, str]]:
    """The shipped English behind every catalogue entry, walked out of ``templates_builtin``.

    Derived, never hand-written, so it is *exactly* the text the seed wrote into the row and
    ``instantiate_template`` copied onto the campaign — which is what makes the edit test in
    :func:`render` trustworthy.
    """
    out: dict[str, dict[str, str]] = {}

    def put(key: str, **fields: str | None) -> None:
        values = {name: value for name, value in fields.items() if value}
        if values:
            out.setdefault(key, {}).update(values)

    for t in templates_builtin.BUILTIN:
        k = t["key"]
        put(
            template_key(k), name=t.get("name"), description=t.get("description"),
            summary=t.get("summary"), rationale=t.get("rationale"),
            desired_outcome=t.get("desired_outcome"), success_criteria=t.get("success_criteria"),
            failure_criteria=t.get("failure_criteria"),
        )
        for w in t.get("workstreams", []):
            put(workstream_key(k, w["key"]), name=w.get("name"), description=w.get("description"))
        for i, o in enumerate(t.get("objectives", [])):
            put(objective_key(k, i), title=o.get("title"), description=o.get("description"),
                unit=o.get("unit"))
        for i, m in enumerate(t.get("milestones", [])):
            put(milestone_key(k, i), title=m.get("title"), description=m.get("description"))
        for i, r in enumerate(t.get("risks", [])):
            put(risk_key(k, i), description=r.get("description"), mitigation=r.get("mitigation"),
                contingency=r.get("contingency"), trigger=r.get("trigger"))
    return out


BUILTIN_ENGLISH: dict[str, dict[str, str]] = _english_catalogue()


def msgid(source_key: str, field: str) -> str | None:
    """The ``gettext_lazy`` msgid for a built-in field — ``None`` for corp content/unknown keys."""
    return BUILTIN_MSGIDS.get(source_key or "", {}).get(field)


def english(source_key: str, field: str) -> str:
    """The English this built-in field ships (and was seeded/copied) with, or ``""``."""
    return BUILTIN_ENGLISH.get(source_key or "", {}).get(field, "")


def render(source_key: str, field: str, stored: str, *, max_length: int | None = None) -> str:
    """The seam. Returns the translation while the row still holds the shipped English, and the
    stored text verbatim once an officer has edited it. Never blanks, never raises.

    ``max_length`` is the *stored* column's limit: ``instantiate_template`` truncates as it copies
    (``title[:200]``, ``name[:120]``, ``trigger[:200]``…), so the shipped English has to be
    truncated the same way before the comparison — otherwise a long built-in string would always
    look "edited" and would never translate.
    """
    stored = stored or ""
    proxy = msgid(source_key, field)
    if proxy is None:
        return stored  # corp template or hand-written row — gettext never touches user content
    shipped = english(source_key, field)
    if max_length:
        shipped = shipped[:max_length]
    if stored != shipped:
        return stored  # edited: the officer's words, verbatim, in every locale
    # str() resolves the lazy proxy NOW, under the active locale; a missing msgstr falls back to
    # the English msgid, so this branch can never blank the row.
    return str(proxy) or stored


# The provenance attribute to read, in order: a copied row carries ``source_key``; a
# ``CampaignTemplate`` row *is* the built-in and carries ``key``.
_KEY_ATTRS = ("source_key", "key")


def provenance_key(obj) -> str:
    """The catalogue key for a row. The *first attribute that exists* wins, even when empty —
    a hand-made ``Workstream`` has an empty ``source_key`` and must stop there, never fall through
    to its own lane ``key`` (a lane key is a campaign-local slug, not a built-in key)."""
    for attr in _KEY_ATTRS:
        if hasattr(obj, attr):
            return getattr(obj, attr) or ""
    return ""


def text(obj, field: str, *, source_key: str | None = None, msgid_field: str | None = None) -> str:
    """:func:`render` for a model row — reads the stored value, its provenance key and its
    ``max_length`` off the field itself.

    ``msgid_field`` overrides the catalogue field name when the column is named differently from
    the blueprint field it was copied from.
    """
    if source_key is None:
        source_key = provenance_key(obj)
    try:
        max_length = obj._meta.get_field(field).max_length
    except FieldDoesNotExist:
        max_length = None
    return render(
        source_key, msgid_field or field, getattr(obj, field, "") or "", max_length=max_length
    )
