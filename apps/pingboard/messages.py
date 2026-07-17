"""Code-defined, gettext-wrapped message scaffolds (design doc 08 §7.1).

This is the **single seam** where a service notification's sentence becomes
translatable. A DB ``AlertTemplate.body`` is corp-generated content (verbatim in every
locale, D14.8); a custom officer alert is human free-text (verbatim, D14.6). Only the
scaffolds here carry a ``gettext`` msgid, so only they localise per recipient.

Contract (enforced by review + the terminology linter, doc 08 §11):

* The msgid text is category-C **chrome** (translatable). Jargon inside it — PvP,
  gatecamp, sov, ADM, moon extraction, killmail, FC, logi, doctrine — stays canonical
  English inside the msgid unless CCP publishes an official localisation (D16).
* Every ``{slot}`` is a **protected interpolation slot**: it must be a name from
  ``rendering.VARIABLE_CATALOGUE`` and it stays RAW — the interpolated EVE/game/user
  value is never translated (D14.7 / doc 08 §11.1). Translators keep the ``{slots}``
  verbatim (placeholder-parity gate).
* Corollary — **never put a ``get_FOO_display()`` label in a context slot.** A ``TextChoices``
  label is a ``gettext_lazy`` proxy: ``str()``-ing it into ``Alert.context`` (a JSONField)
  freezes whatever locale was active AT EMIT TIME — on a request path that is the *acting
  officer's* language, which then reaches every recipient verbatim because slots are never
  re-translated. An enum label is chrome: register **one key per enum value** and let the call
  site select the key from the RAW code (``campaign.health``, ``order.status``,
  ``timer.timer_type``). Context carries codes and raw EVE/user data — never labels.
* NEVER ``str.join``/concatenate the ``gettext_lazy`` proxies below; they are resolved
  with ``str(...)`` inside an active ``translation.override`` at render time
  (``rendering_i18n.render_for``), never before.

Populated incrementally as service call sites migrate from ``body=f"…"`` to a scaffold
key + ``context`` (phased, English-safe — doc 08 §16). Until a site migrates it keeps
``custom_message=True`` and delivers verbatim English, identical to today.
"""
from __future__ import annotations

from dataclasses import dataclass

from django.utils.translation import gettext_lazy as _


@dataclass(frozen=True)
class Scaffold:
    """A translatable (subject, body) pair of gettext msgids with ``{slot}`` placeholders.

    ``subject`` may be empty when the alert has no distinct localised subject (the frozen
    ``Alert.title`` is used as the audit fallback).
    """

    subject: str  # a gettext msgid with {slot} placeholders (may be "")
    body: str     # a gettext msgid with {slot} placeholders


# Keyed by ``template_key`` (namespaced to the notification-event key it serves). Every
# {slot} MUST be a name from rendering.VARIABLE_CATALOGUE; the interpolated value stays raw.
#
# A call site whose English sentence varies with an optional fragment (a comms link, a block
# reason, a system name) registers ONE key per variant rather than pushing an English fragment
# through a slot — a slot value is never translated, so chrome smuggled into it would freeze in
# the emitting worker's locale. Variant keys keep every English word inside a msgid.
SCAFFOLDS: dict[str, Scaffold] = {
    # --- operations ---------------------------------------------------------
    "operations.formup_reminder": Scaffold(
        subject=_("“{operation_name}” form-up"),
        body=_(
            "“{operation_name}” forms up in about {start_time} at {formup_system} · "
            "comms {link}. You signed up — see you there."
        ),
    ),
    "operations.formup_reminder.no_comms": Scaffold(
        subject=_("“{operation_name}” form-up"),
        body=_(
            "“{operation_name}” forms up in about {start_time} at {formup_system}. "
            "You signed up — see you there."
        ),
    ),
    "operations.formup_reminder.no_place": Scaffold(
        subject=_("“{operation_name}” form-up"),
        body=_(
            "“{operation_name}” forms up in about {start_time} · comms {link}. "
            "You signed up — see you there."
        ),
    ),
    "operations.formup_reminder.time_only": Scaffold(
        subject=_("“{operation_name}” form-up"),
        body=_(
            "“{operation_name}” forms up in about {start_time}. "
            "You signed up — see you there."
        ),
    ),
    "operations.formup_reminder.soon": Scaffold(
        subject=_("“{operation_name}” form-up"),
        body=_(
            "“{operation_name}” forms up shortly at {formup_system} · comms {link}. "
            "You signed up — see you there."
        ),
    ),
    "operations.formup_reminder.soon_no_comms": Scaffold(
        subject=_("“{operation_name}” form-up"),
        body=_(
            "“{operation_name}” forms up shortly at {formup_system}. "
            "You signed up — see you there."
        ),
    ),
    "operations.formup_reminder.soon_no_place": Scaffold(
        subject=_("“{operation_name}” form-up"),
        body=_(
            "“{operation_name}” forms up shortly · comms {link}. "
            "You signed up — see you there."
        ),
    ),
    "operations.formup_reminder.soon_only": Scaffold(
        subject=_("“{operation_name}” form-up"),
        body=_("“{operation_name}” forms up shortly. You signed up — see you there."),
    ),
    "operations.auto_cancelled": Scaffold(
        subject=_("Cancelled — {operation_name}"),
        body=_(
            "❌ {operation_name} auto-cancelled — only {count} of {required_count} pilots "
            "confirmed by the sign-up deadline."
        ),
    ),
    # One key per (timer_type, side) pair — a TextChoices label is CHROME and belongs inside
    # the msgid, never in a context slot: a slot is interpolated raw, so a label pushed through
    # one would freeze in the emitting officer's locale and reach every recipient in it. The
    # call site selects the key from the RAW codes (``timer.timer_type`` / ``timer.side``).
    "operations.structure_timer.armor.friendly": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Armor, Friendly (defend))\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.armor.friendly.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Armor, Friendly (defend))\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.armor.hostile": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Armor, Hostile (attack))\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.armor.hostile.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Armor, Hostile (attack))\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.armor.neutral": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Armor, Neutral)\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.armor.neutral.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Armor, Neutral)\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.hull.friendly": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Hull / Final, Friendly (defend))\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.hull.friendly.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Hull / Final, Friendly (defend))\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.hull.hostile": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Hull / Final, Hostile (attack))\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.hull.hostile.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Hull / Final, Hostile (attack))\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.hull.neutral": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Hull / Final, Neutral)\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.hull.neutral.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Hull / Final, Neutral)\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.anchoring.friendly": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Anchoring, Friendly (defend))\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.anchoring.friendly.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Anchoring, Friendly (defend))\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.anchoring.hostile": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Anchoring, Hostile (attack))\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.anchoring.hostile.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Anchoring, Hostile (attack))\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.anchoring.neutral": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Anchoring, Neutral)\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.anchoring.neutral.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Anchoring, Neutral)\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.unanchoring.friendly": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Unanchoring, Friendly (defend))\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.unanchoring.friendly.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Unanchoring, Friendly (defend))\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.unanchoring.hostile": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Unanchoring, Hostile (attack))\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.unanchoring.hostile.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Unanchoring, Hostile (attack))\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.unanchoring.neutral": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Unanchoring, Neutral)\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.unanchoring.neutral.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Unanchoring, Neutral)\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.ihub.friendly": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Sov · IHub, Friendly (defend))\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.ihub.friendly.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Sov · IHub, Friendly (defend))\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.ihub.hostile": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Sov · IHub, Hostile (attack))\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.ihub.hostile.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Sov · IHub, Hostile (attack))\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.ihub.neutral": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Sov · IHub, Neutral)\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.ihub.neutral.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Sov · IHub, Neutral)\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.tcu.friendly": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Sov · TCU, Friendly (defend))\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.tcu.friendly.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Sov · TCU, Friendly (defend))\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.tcu.hostile": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Sov · TCU, Hostile (attack))\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.tcu.hostile.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Sov · TCU, Hostile (attack))\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.tcu.neutral": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Sov · TCU, Neutral)\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.tcu.neutral.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Sov · TCU, Neutral)\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.other.friendly": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Other, Friendly (defend))\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.other.friendly.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Other, Friendly (defend))\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.other.hostile": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Other, Hostile (attack))\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.other.hostile.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Other, Hostile (attack))\n"
            "🕒 {timer_time} EVE"
        ),
    ),
    "operations.structure_timer.other.neutral": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Other, Neutral)\n"
            "🕒 {timer_time} EVE · {system_name}"
        ),
    ),
    "operations.structure_timer.other.neutral.no_system": Scaffold(
        subject=_("Timer — {structure_name}"),
        body=_(
            "⏰ **Timer** — {structure_name} (Other, Neutral)\n"
            "🕒 {timer_time} EVE"
        ),
    ),

    # --- killboard ----------------------------------------------------------
    "killboard.milestone.first_kill": Scaffold(
        subject=_("Milestone unlocked: First kill!"),
        body=_("Congratulations on your first corp killmail! Welcome to the fight."),
    ),
    "killboard.milestone.first_solo": Scaffold(
        subject=_("Milestone unlocked: First solo kill!"),
        body=_("Your first solo kill — you took one down all on your own. Nice work."),
    ),
    "killboard.milestone.first_final_blow": Scaffold(
        subject=_("Milestone unlocked: First final blow!"),
        body=_("You landed your first final blow. The killing shot is yours."),
    ),
    "killboard.rank_up": Scaffold(
        subject=_("Combat rank achieved: {rank_name}"),
        body=_(
            "Congratulations — you've reached the rank of {rank_name} ({kill_count} lifetime "
            "kills recorded). Ranks update from the nightly combat rollup, so this can reflect "
            "kills from the last day or two rather than the exact moment."
        ),
    ),
    "killboard.rank_up.reward": Scaffold(
        subject=_("Combat rank achieved: {rank_name}"),
        body=_(
            "Congratulations — you've reached the rank of {rank_name} ({kill_count} lifetime "
            "kills recorded). A rank reward is pending leadership approval. Ranks update from "
            "the nightly combat rollup, so this can reflect kills from the last day or two "
            "rather than the exact moment."
        ),
    ),
    "killboard.watchlist_tripwire": Scaffold(
        subject=_("Watchlist tripwire: {entity_name}"),
        body=_(
            "⚠ Watched {entity_type} **{entity_name}** ({watchlist_name}) was on a killmail "
            "in {system_name} ~{minutes}m ago. Risk indicator, not a guarantee — verify "
            "before acting."
        ),
    ),

    # --- store --------------------------------------------------------------
    "store.order_status.claimed": Scaffold(
        subject=_("Order update: Claimed"),
        body=_("{ship_name} was claimed — a member is fulfilling it."),
    ),
    "store.order_status.deposit_paid": Scaffold(
        subject=_("Order update: Deposit paid"),
        body=_("Deposit received for {ship_name}."),
    ),
    "store.order_status.in_production": Scaffold(
        subject=_("Order update: In production"),
        body=_("{ship_name} is in production."),
    ),
    "store.order_status.ready": Scaffold(
        subject=_("Order update: Ready"),
        body=_("{ship_name} is ready — coordinate pickup."),
    ),
    "store.order_status.delivered": Scaffold(
        subject=_("Order update: Delivered"),
        body=_("{ship_name} was delivered. Fly safe!"),
    ),
    "store.order_status.cancelled": Scaffold(
        subject=_("Order update: Cancelled"),
        body=_("Your order for {ship_name} was cancelled."),
    ),
    "store.order_eta_changed": Scaffold(
        subject=_("Order update: New delivery estimate"),
        body=_("The estimated delivery for {ship_name} is now {eta_date}."),
    ),
    "store.order_stock_allocated": Scaffold(
        subject=_("Order update: Stock reserved"),
        body=_("{quantity}× {ship_name} from a new delivery is now reserved for your order."),
    ),
    "store.order_reservation_expired": Scaffold(
        subject=_("Order update: Reservation released"),
        body=_(
            "Your reserved {ship_name} was released after waiting unclaimed; "
            "the order is now backordered."
        ),
    ),
    "store.waitlist_available": Scaffold(
        subject=_("Back in stock: {ship_name}"),
        body=_("{ship_name} can be ordered on the Shipyard again."),
    ),
    # --- industry (MRP v1, P3) ------------------------------------------------
    # ``{industry_job_name}`` is an SdeType name — EVE game data, raw by policy.
    "industry.mrp_shortfall": Scaffold(
        subject=_("Material shortfall: {industry_job_name}"),
        body=_(
            "The material plan needs {quantity}× {industry_job_name} by {eta_date}. "
            "Review the Material Plan: {link}"
        ),
    ),
    "store.supply_need.built": Scaffold(
        subject=_("Shipyard restock built"),
        body=_(
            "Production for {fit_name} finished. Assemble and fit the ships, then record "
            "the receipt on the Shipyard inventory console to release waiting backorders."
        ),
    ),

    # --- sso ----------------------------------------------------------------
    "sso.token_death": Scaffold(
        subject=_("Re-authorise {character_name}: corp data ingestion stopped"),
        body=_(
            "The ESI token for {character_name} that authorises corp data ingestion "
            "({scopes}) has stopped working — it was revoked or failed to refresh past the "
            "retry limit. Re-authorise it on the ESI scopes page (/auth/scopes/) to resume "
            "ingestion; corp {scopes} pages will show stale data until then."
        ),
    ),

    # --- skills -------------------------------------------------------------
    "skills.idle_queue": Scaffold(
        subject=_("Your skill queue is empty — {character_name}"),
        body=_(
            "{character_name}'s training queue has run dry, so it's not earning SP. Queue "
            "your next skills in the EVE client (the Skills page has a plan you can paste "
            "in). This is based on the last skill sync, so ignore it if you just queued "
            "something."
        ),
    ),

    # --- capsuleer ----------------------------------------------------------
    "capsuleer.milestone_reached": Scaffold(
        subject=_("Milestone reached: «{milestone_title}»"),
        body=_("Milestone «{milestone_title}» on your goal «{goal_title}» is done. {link}"),
    ),
    "capsuleer.goal_completed": Scaffold(
        subject=_("Goal completed: «{goal_title}»"),
        body=_("You completed your career goal «{goal_title}». {link}"),
    ),
    "capsuleer.review_due": Scaffold(
        subject=_("Review nudge: «{goal_title}»"),
        body=_(
            "It's been a while since you looked at «{goal_title}» ({review_month}). A "
            "two-minute review keeps the plan honest — nothing changes if you don't. {link}"
        ),
    ),
    "capsuleer.suggestion.one": Scaffold(
        subject=_("New Capsuleer Path suggestions"),
        body=_(
            "You have {count} new Capsuleer Path suggestion waiting. Open your path to see "
            "them. {link}"
        ),
    ),
    "capsuleer.suggestion.many": Scaffold(
        subject=_("New Capsuleer Path suggestions"),
        body=_(
            "You have {count} new Capsuleer Path suggestions waiting. Open your path to see "
            "them. {link}"
        ),
    ),

    # --- planetary ----------------------------------------------------------
    "planetary.colony_issue": Scaffold(
        subject=_("PI colony needs attention"),
        body=_(
            "Your PI colony on a {planet_type} in {system_name} needs attention: {details}"
        ),
    ),

    # --- navigation ---------------------------------------------------------
    "navigation.route_watch": Scaffold(
        subject=_("Route watch: {route_name}"),
        body=_(
            "⚠ Route **{route_name}** ({origin_system} → {destination_system}) — "
            "{threat_count} watched system(s) flagged (endpoints & waypoints only):\n"
            "{details}\n"
            "Risk indicator, not a guarantee — this doesn't cover every gate hop. Scout "
            "before you undock."
        ),
    ),

    # --- logistics ----------------------------------------------------------
    "logistics.haul_reminder": Scaffold(
        subject=_("Haul deadline approaching"),
        body=_(
            "Your haul {origin_system} → {destination_system} is due in about {minutes} min. "
            "Deliver it and mark the contract complete to get paid."
        ),
    ),
    "logistics.haul_released_poster": Scaffold(
        subject=_("Haul overdue — released to the pool"),
        body=_(
            "The haul {origin_system} → {destination_system} passed its deadline and was "
            "returned to the pool for another hauler."
        ),
    ),
    "logistics.haul_released_hauler": Scaffold(
        subject=_("Your haul was released"),
        body=_(
            "Your haul {origin_system} → {destination_system} passed its deadline and was "
            "released back to the pool."
        ),
    ),

    # --- corporation / mining ------------------------------------------------
    "corporation.chunk_arrival": Scaffold(
        subject=_("Moon chunk arriving soon"),
        body=_(
            "The moon chunk at {structure_name} is ready to fracture in about {hours}h — "
            "form up to mine it before it decays."
        ),
    ),
    "corporation.infrastructure_alert": Scaffold(
        subject=_("Corp infrastructure alert"),
        body=_(
            "Corp infrastructure needs attention:\n\n{details}\n\nRefuel the low structures "
            "and shore up the soft-ADM systems. This digest fires once per distinct set and "
            "resets when everything is back above threshold."
        ),
    ),

    # --- srp -----------------------------------------------------------------
    "srp.sla_breach": Scaffold(
        subject=_("SRP needs attention"),
        body=_(
            "SRP is breaching its service level:\n\n{details}\n\nReview the SRP queue and "
            "clear/decide the oldest claims. This digest fires once per distinct breach set "
            "and resets when SRP is back within SLA."
        ),
    ),

    # --- admin_audit ---------------------------------------------------------
    "admin_audit.integration_health": Scaffold(
        subject=_("Integration health degraded"),
        body=_(
            "FORCA integration health has degraded:\n\n{details}\n\nOpen the Admin Console → "
            "Health page for the full picture. This alert fires once per distinct problem set "
            "and resets automatically once healthy."
        ),
    ),
    "identity.role_request": Scaffold(
        subject=_("Role grant awaiting approval"),
        body=_(
            "{actor_name} requested a {role_name} grant for {pilot_name}. A different "
            "director must approve it in the Members console."
        ),
    ),

    # --- mentorship ----------------------------------------------------------
    "mentorship.pairing.mentor_requested": Scaffold(
        subject=_("Mentorship pairing"),
        body=_(
            "{mentee_name} has requested you as their mentor. Respond on your mentorship "
            "dashboard."
        ),
    ),
    "mentorship.pairing.mentee_invited": Scaffold(
        subject=_("Mentorship pairing"),
        body=_(
            "{mentor_name} has invited you as their cadet. Respond on your mentorship "
            "dashboard."
        ),
    ),
    "mentorship.pairing.suggested_cadet": Scaffold(
        subject=_("Mentorship pairing"),
        body=_(
            "We suggested {mentee_name} as a cadet you could mentor. Respond on your "
            "mentorship dashboard."
        ),
    ),
    "mentorship.pairing.suggested_mentor": Scaffold(
        subject=_("Mentorship pairing"),
        body=_(
            "We found you a mentor match: {mentor_name}. Respond on your mentorship "
            "dashboard."
        ),
    ),

    # --- recommendations (ESI corp alerts) ------------------------------------
    "recommendations.esi_corp_alert": Scaffold(
        subject="",  # the ESI notification label is the frozen title (an EVE term, D16)
        body=_("🚨 {event_label} — {event_time} EVE"),
    ),

    # --- raffle ---------------------------------------------------------------
    "raffle.started": Scaffold(
        subject=_("🎟️ Raffle open: {contest_name}"),
        body=_("{details}  Connect your ESI token and fly to earn tickets."),
    ),
    "raffle.closed": Scaffold(
        subject=_("Raffle closed: {contest_name}"),
        body=_("Ticket accrual is closed. The draw happens soon — good luck!"),
    ),
    "raffle.grant": Scaffold(
        subject=_("🎟️ +{ticket_count} raffle tickets"),
        body=_("You received {ticket_count} tickets in “{contest_name}”: {reason}"),
    ),
    "raffle.winners": Scaffold(
        subject=_("🏆 Raffle winners: {contest_name}"),
        body=_("Congratulations!\n{details}"),
    ),
    "raffle.win": Scaffold(
        subject=_("🏆 You won {prize_name}!"),
        body=_(
            "You won #{prize_rank} in “{contest_name}”. Leadership will be in touch about "
            "delivery."
        ),
    ),
    "raffle.enrolment_outreach.one": Scaffold(
        subject=_("Enrol to claim your raffle tickets"),
        body=_(
            "You earned {ticket_count} would-be raffle ticket in “{contest_name}” but aren't "
            "enrolled yet — connect your ESI token and enrol to start claiming them: {link}"
            "\n\nPrefer not to be nudged? Opt out here: {opt_out_link}"
        ),
    ),
    "raffle.enrolment_outreach.many": Scaffold(
        subject=_("Enrol to claim your raffle tickets"),
        body=_(
            "You earned {ticket_count} would-be raffle tickets in “{contest_name}” but aren't "
            "enrolled yet — connect your ESI token and enrol to start claiming them: {link}"
            "\n\nPrefer not to be nudged? Opt out here: {opt_out_link}"
        ),
    ),

    # --- campaigns ------------------------------------------------------------
    "campaigns.restricted": Scaffold(
        subject=_("Campaign «{campaign_name}»"),
        body=_(
            "Campaign «{campaign_name}»: you have a campaign notification — open Campaign "
            "Command for details. {link}"
        ),
    ),
    "campaigns.assigned": Scaffold(
        subject=_("Assigned on «{campaign_name}»"),
        body=_(
            "You've been assigned {assignment_label} on campaign «{campaign_name}». Why it "
            "matters: {reason} {link}"
        ),
    ),
    "campaigns.assigned.objective": Scaffold(
        subject=_("Assigned on «{campaign_name}»"),
        body=_(
            "You've been assigned an objective on campaign «{campaign_name}». Why it "
            "matters: {reason} {link}"
        ),
    ),
    "campaigns.assigned.workstream": Scaffold(
        subject=_("Assigned on «{campaign_name}»"),
        body=_(
            "You've been assigned a workstream on campaign «{campaign_name}». Why it "
            "matters: {reason} {link}"
        ),
    ),
    "campaigns.assigned.milestone": Scaffold(
        subject=_("Assigned on «{campaign_name}»"),
        body=_(
            "You've been assigned a milestone on campaign «{campaign_name}». Why it "
            "matters: {reason} {link}"
        ),
    ),
    "campaigns.assigned.campaign": Scaffold(
        subject=_("Assigned on «{campaign_name}»"),
        body=_(
            "You've been assigned command of this campaign on campaign «{campaign_name}». "
            "Why it matters: {reason} {link}"
        ),
    ),
    "campaigns.recognition": Scaffold(
        subject=_("Recognised on «{campaign_name}»"),
        body=_(
            "You were recognised for your contribution to campaign «{campaign_name}». {link}"
        ),
    ),
    "campaigns.recognition.reason": Scaffold(
        subject=_("Recognised on «{campaign_name}»"),
        body=_(
            "You were recognised for your contribution to campaign «{campaign_name}» — "
            "{reason}. {link}"
        ),
    ),
    "campaigns.objective_blocked": Scaffold(
        subject=_("Objective blocked on «{campaign_name}»"),
        body=_(
            "Objective «{objective_title}» on campaign «{campaign_name}» is blocked. {link}"
        ),
    ),
    "campaigns.objective_blocked.reason": Scaffold(
        subject=_("Objective blocked on «{campaign_name}»"),
        body=_(
            "Objective «{objective_title}» on campaign «{campaign_name}» is blocked — "
            "{reason}. {link}"
        ),
    ),
    "campaigns.dependency_completed": Scaffold(
        subject=_("Dependency cleared on «{campaign_name}»"),
        body=_(
            "A dependency cleared on campaign «{campaign_name}» — work that was waiting on "
            "it can proceed. {link}"
        ),
    ),
    "campaigns.deadline_soon": Scaffold(
        subject=_("Deadline on «{campaign_name}»"),
        body=_("«{item_title}» on campaign «{campaign_name}» is due soon. {link}"),
    ),
    "campaigns.deadline_soon.overdue": Scaffold(
        subject=_("Deadline on «{campaign_name}»"),
        body=_("«{item_title}» on campaign «{campaign_name}» is overdue. {link}"),
    ),
    "campaigns.manual_update_needed": Scaffold(
        subject=_("Metric needs an update on «{campaign_name}»"),
        body=_(
            "Objective «{objective_title}» on campaign «{campaign_name}» needs a manual value "
            "update — its last reading has gone stale. {link}"
        ),
    ),
    "campaigns.approval_needed.milestone": Scaffold(
        subject=_("Milestone ready for review on «{campaign_name}»"),
        body=_(
            "Milestone «{milestone_title}» is ready for your review on campaign "
            "«{campaign_name}». {link}"
        ),
    ),
    "campaigns.approval_needed": Scaffold(
        subject=_("Campaign proposed: «{campaign_name}»"),
        body=_(
            "Campaign «{campaign_name}» has been proposed and needs a director's approval. "
            "{link}"
        ),
    ),
    "campaigns.approved": Scaffold(
        subject=_("Campaign approved: «{campaign_name}»"),
        body=_("Your campaign «{campaign_name}» was approved and is ready to start. {link}"),
    ),
    "campaigns.started": Scaffold(
        subject=_("Campaign started: «{campaign_name}»"),
        body=_("Campaign «{campaign_name}» is now active. {details} {link}"),
    ),
    "campaigns.completed": Scaffold(
        subject=_("Campaign completed: «{campaign_name}»"),
        body=_("Campaign «{campaign_name}» completed. {link}"),
    ),
    "campaigns.completed.failed": Scaffold(
        subject=_("Campaign ended (failed): «{campaign_name}»"),
        body=_("Campaign «{campaign_name}» ended (failed). {link}"),
    ),
    "campaigns.completed.cancelled": Scaffold(
        subject=_("Campaign was cancelled: «{campaign_name}»"),
        body=_("Campaign «{campaign_name}» was cancelled. {link}"),
    ),
    "campaigns.issue_escalated": Scaffold(
        subject=_("Issue escalated on «{campaign_name}»"),
        body=_(
            "An issue on campaign «{campaign_name}» has been escalated and needs leadership "
            "attention. {link}"
        ),
    ),
    # One key per Campaign.Health value (``unknown`` never notifies) — the health label is chrome
    # and lives inside the msgid. The call site picks the key from the RAW ``campaign.health`` code
    # and puts NO label in context; a slot is never translated, so a label smuggled through one
    # would freeze in the emitting officer's locale.
    "campaigns.health_changed.healthy": Scaffold(
        subject=_("Campaign health Healthy: «{campaign_name}»"),
        body=_("Campaign «{campaign_name}» is now Healthy. {link}"),
    ),
    "campaigns.health_changed.healthy.reasons": Scaffold(
        subject=_("Campaign health Healthy: «{campaign_name}»"),
        body=_("Campaign «{campaign_name}» is now Healthy — {details}. {link}"),
    ),
    "campaigns.health_changed.watch": Scaffold(
        subject=_("Campaign health Watch: «{campaign_name}»"),
        body=_("Campaign «{campaign_name}» is now Watch. {link}"),
    ),
    "campaigns.health_changed.watch.reasons": Scaffold(
        subject=_("Campaign health Watch: «{campaign_name}»"),
        body=_("Campaign «{campaign_name}» is now Watch — {details}. {link}"),
    ),
    "campaigns.health_changed.at_risk": Scaffold(
        subject=_("Campaign health At Risk: «{campaign_name}»"),
        body=_("Campaign «{campaign_name}» is now At Risk. {link}"),
    ),
    "campaigns.health_changed.at_risk.reasons": Scaffold(
        subject=_("Campaign health At Risk: «{campaign_name}»"),
        body=_("Campaign «{campaign_name}» is now At Risk — {details}. {link}"),
    ),
    "campaigns.health_changed.critical": Scaffold(
        subject=_("Campaign health Critical: «{campaign_name}»"),
        body=_("Campaign «{campaign_name}» is now Critical. {link}"),
    ),
    "campaigns.health_changed.critical.reasons": Scaffold(
        subject=_("Campaign health Critical: «{campaign_name}»"),
        body=_("Campaign «{campaign_name}» is now Critical — {details}. {link}"),
    ),
    "campaigns.health_changed.blocked": Scaffold(
        subject=_("Campaign health Blocked: «{campaign_name}»"),
        body=_("Campaign «{campaign_name}» is now Blocked. {link}"),
    ),
    "campaigns.health_changed.blocked.reasons": Scaffold(
        subject=_("Campaign health Blocked: «{campaign_name}»"),
        body=_("Campaign «{campaign_name}» is now Blocked — {details}. {link}"),
    ),
}


def scaffold(key: str) -> Scaffold | None:
    """The code scaffold registered for ``key``, or ``None``."""
    return SCAFFOLDS.get(key)
