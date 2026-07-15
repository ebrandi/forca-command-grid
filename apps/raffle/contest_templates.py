"""Built-in contest templates — reusable blueprints leaders start a contest from.

A template's ``config`` has three parts: ``contest`` (field defaults),
``sources`` ({source_key: {enabled, mode, config, filters}}) and ``prizes`` (a
list of prize dicts). :func:`apply_template` writes them onto a draft contest;
:func:`seed_builtin_templates` upserts the built-ins (called from the data
migration and re-runnable safely).

i18n — the render-time seam (Seam A)
------------------------------------
The prose inside ``config`` is **seeded into the database**: ``seed_builtin_templates``
persists it on ``RaffleContestTemplate.config``, and :func:`apply_template` then copies
it onto ``RaffleContest.objective`` and ``RafflePrize.name``. Wrapping it in
``gettext_lazy`` would be worse than useless: ``config`` is a **JSONField**, and a lazy
proxy inside it is a hard ``TypeError`` at save/migrate time; on the CharFields it would
silently freeze whatever locale was active at seed time into the row.

So the English stays **plain ``str``** — canonical, JSON-safe, the audit record and the
fallback — and is marked for extraction with ``gettext_noop`` (Django's ``makemessages``
passes ``--keyword=gettext_noop``, so xgettext sees these literals exactly as it sees
``_()``). Translation happens at *render* time, in :func:`objective_for` /
:func:`prize_name_for`, keyed on the stable ``RaffleContestTemplate.key`` (mirrored onto
``RaffleContest.template_key``) — see the ``*_i18n`` properties on the models.

A row is only translated while its stored text is still **byte-identical to the shipped
English**. The moment a leader edits the objective (or a contest was never built from a
built-in template) the stored text is their content and is rendered verbatim, in every
locale. Nothing here ever returns blank.
"""
from __future__ import annotations

from django.utils.translation import gettext
from django.utils.translation import gettext_lazy as _
from django.utils.translation import gettext_noop

# The default prize-ladder rank names, persisted into ``config["prizes"][*]["name"]``
# (and from there onto ``RafflePrize.name``). Plain strings — marked for extraction,
# translated at render time by :func:`prize_name_for`.
RANK_NAMES: list[str] = [
    gettext_noop("1st prize"),
    gettext_noop("2nd prize"),
    gettext_noop("3rd prize"),
    gettext_noop("4th prize"),
    gettext_noop("5th prize"),
]


def _prizes(*values):
    return [
        {"rank": i + 1, "name": RANK_NAMES[i], "prize_type": "isk",
         "estimated_value": v, "description": ""}
        for i, v in enumerate(values)
    ]


BUILTIN: list[dict] = [
    {
        "key": "pvp_activity", "name": _("PVP activity raffle"),
        "description": _("Reward everyone who undocks and gets on kills. Solo 100 · final blow 10 · participation 1."),
        "config": {
            "contest": {"objective": gettext_noop("Get pilots on more kills."),
                        "one_prize_per_pilot": True},
            "sources": {"pvp": {"enabled": True, "mode": "auto"},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("2000000000", "1000000000", "500000000", "250000000", "100000000"),
        },
    },
    {
        "key": "solo_kill", "name": _("Solo kill challenge"),
        "description": _("Heavily reward solo PvP prowess — solo kills are worth 10× a normal kill."),
        "config": {
            "contest": {"objective": gettext_noop("Crown the corp's best solo hunter.")},
            "sources": {"pvp": {"enabled": True, "mode": "auto",
                                 "config": {"per_kill": 1, "final_blow": 5, "solo": 250}},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("1500000000", "750000000", "400000000", "200000000", "100000000"),
        },
    },
    {
        "key": "home_defence", "name": _("Home defence contest"),
        "description": _("Reward kills defending home space. Set the home region/systems in the PVP filters."),
        "config": {
            "contest": {"objective": gettext_noop("Defend home — kills in our space earn tickets.")},
            "sources": {"pvp": {"enabled": True, "mode": "auto",
                                 "filters": {"exclude_blue": True}},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("1000000000", "500000000", "250000000", "150000000", "75000000"),
        },
    },
    {
        "key": "mining_month", "name": _("Mining month"),
        "description": _("Reward ore mined from the corp mining ledger (X tickets per m³)."),
        "config": {
            "contest": {"objective": gettext_noop("Fill the ore hangar — mine to earn tickets.")},
            "sources": {"mining": {"enabled": True, "mode": "auto",
                                    "config": {"basis": "m3", "per_ticket": 50000}},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("800000000", "400000000", "200000000", "100000000", "50000000"),
        },
    },
    {
        "key": "industry_drive", "name": _("Industry production drive"),
        "description": _("Recognise builders. Industry has no reliable per-pilot feed, "
                         "so awards are officer-approved."),
        "config": {
            "contest": {"objective": gettext_noop("Keep the production lines running.")},
            "sources": {"industry": {"enabled": True, "mode": "officer_approved"},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("800000000", "400000000", "200000000", "100000000", "50000000"),
        },
    },
    {
        "key": "logistics_campaign", "name": _("Logistics support campaign"),
        "description": _("Reward haulers for delivered courier contracts (officer-approved)."),
        "config": {
            "contest": {"objective": gettext_noop("Keep the supply lines moving.")},
            "sources": {"logistics": {"enabled": True, "mode": "officer_approved"},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("500000000", "300000000", "150000000", "100000000", "50000000"),
        },
    },
    {
        "key": "newbro_support", "name": _("Newbro support contest"),
        "description": _("Reward mentors and helpers. Mentorship completions + leadership grants."),
        "config": {
            "contest": {"objective": gettext_noop("Help new pilots get flying.")},
            "sources": {"mentorship": {"enabled": True, "mode": "officer_approved"},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("500000000", "300000000", "150000000", "100000000", "50000000"),
        },
    },
    {
        "key": "alliance_deployment", "name": _("Alliance deployment raffle"),
        "description": _("PVP + fleet attendance during a deployment. Admits alliance / friendly pilots."),
        "config": {
            "contest": {"objective": gettext_noop("Show up and fight the deployment."),
                        "include_alliance": True},
            "sources": {"pvp": {"enabled": True, "mode": "auto"},
                        "fleet": {"enabled": True, "mode": "auto", "config": {"per_op": 10}},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("3000000000", "1500000000", "750000000", "400000000", "200000000"),
        },
    },
    {
        "key": "mixed_engagement", "name": _("Mixed activity engagement"),
        "description": _("A bit of everything — PVP, mining, fleet attendance and leadership grants."),
        "config": {
            "contest": {"objective": gettext_noop("Reward any way of contributing to the corp.")},
            "sources": {"pvp": {"enabled": True, "mode": "auto"},
                        "mining": {"enabled": True, "mode": "auto"},
                        "fleet": {"enabled": True, "mode": "auto"},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("2000000000", "1000000000", "500000000", "250000000", "100000000"),
        },
    },
    {
        "key": "esi_adoption", "name": _("ESI adoption campaign"),
        "description": _("Drive app enrolment — everyone who connects ESI and flies earns; big CTA on the dashboard."),
        "config": {
            "contest": {"objective": gettext_noop("Get the whole corp enrolled in FORCA Command Grid."),
                        "show_ineligible_to_pilots": True},
            "sources": {"pvp": {"enabled": True, "mode": "auto"},
                        "manual": {"enabled": True, "mode": "manual"}},
            "prizes": _prizes("1000000000", "500000000", "250000000", "150000000", "100000000"),
        },
    },
]

BUILTIN_BY_KEY = {t["key"]: t for t in BUILTIN}


# --------------------------------------------------------------------------- #
#  Render-time i18n seam (Seam A)
# --------------------------------------------------------------------------- #
def builtin_objective(template_key: str) -> str:
    """The shipped English objective for a built-in template key, or ``""``."""
    tpl = BUILTIN_BY_KEY.get(template_key or "")
    if not tpl:
        return ""
    return tpl.get("config", {}).get("contest", {}).get("objective", "")


def objective_for(template_key: str, stored: str) -> str:
    """The objective to *display* for a row that stores ``stored`` under ``template_key``.

    Translated only while the row still holds the shipped English for a **built-in**
    template. An unknown/blank ``template_key`` (a hand-rolled contest) or an edited
    objective (leader content) is returned verbatim — the same text in every locale.
    Never returns blank: the stored value is always the floor.
    """
    if stored and stored == builtin_objective(template_key):
        return gettext(stored)
    return stored


def prize_name_for(template_key: str, rank: int, stored: str) -> str:
    """The prize name to *display* for rank ``rank`` of a contest built from ``template_key``.

    Only the untouched default ladder ("1st prize" … "5th prize") seeded from a built-in
    template translates. A renamed prize ("Vargur + fit"), or any prize on a contest that
    did not come from a built-in template, is leader content and renders verbatim.
    """
    if not stored or template_key not in BUILTIN_BY_KEY:
        return stored
    if 1 <= (rank or 0) <= len(RANK_NAMES) and stored == RANK_NAMES[rank - 1]:
        return gettext(stored)
    return stored


def apply_template(contest, template_key: str, *, overwrite_prizes: bool = False) -> bool:
    """Write a template's defaults onto a (draft) contest. Returns False if unknown."""
    from .models import RaffleContestTemplate, RafflePrize
    from .services import seed_source_configs

    tpl = RaffleContestTemplate.objects.filter(key=template_key, active=True).first()
    config = tpl.config if tpl else BUILTIN_BY_KEY.get(template_key, {}).get("config")
    if not config:
        return False

    # Contest field defaults (only known, safe fields).
    fields = config.get("contest", {})
    allowed = {
        "objective", "one_prize_per_pilot", "include_alliance",
        "show_ineligible_to_pilots", "retroactive_enabled", "public_rules",
    }
    dirty = []
    for k, v in fields.items():
        if k in allowed:
            setattr(contest, k, v)
            dirty.append(k)
    contest.template_key = template_key
    dirty.append("template_key")
    contest.save(update_fields=list(set(dirty)) + ["updated_at"])

    # Sources.
    seed_source_configs(contest)
    for key, sc in config.get("sources", {}).items():
        cfg = contest.source_configs.filter(source_key=key).first()
        if cfg is None:
            continue
        cfg.enabled = sc.get("enabled", cfg.enabled)
        cfg.mode = sc.get("mode", cfg.mode)
        if "config" in sc:
            cfg.config = {**cfg.config, **sc["config"]}
        if "filters" in sc:
            cfg.filters = {**cfg.filters, **sc["filters"]}
        cfg.save()

    # Prizes.
    if overwrite_prizes or not contest.prizes.exists():
        contest.prizes.all().delete()
        RafflePrize.objects.bulk_create([
            RafflePrize(contest=contest, rank=p["rank"], name=p["name"],
                        prize_type=p.get("prize_type", "isk"),
                        estimated_value=p.get("estimated_value", 0),
                        description=p.get("description", ""))
            for p in config.get("prizes", [])
        ])
    return True


def seed_builtin_templates() -> int:
    """Upsert the built-in templates. Idempotent — safe to re-run."""
    from .models import RaffleContestTemplate

    n = 0
    for t in BUILTIN:
        RaffleContestTemplate.objects.update_or_create(
            key=t["key"],
            defaults={"name": t["name"], "description": t["description"],
                      "config": t["config"], "built_in": True, "active": True},
        )
        n += 1
    return n
