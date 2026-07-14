"""The Command Center (/dashboard/) — the three-page consolidation.

My Readiness and the Daily Briefing (itself the merge of Your Orders +
Recommended Actions) folded into the always-on /dashboard/. These tests pin the
consolidation's core promises: ONE quest queue fed by both engines with
server-side dedup, each engine's own POST endpoint wired through the shared
row include, per-section feature gating with no dead links, and the old URLs
redirecting with their old off-switch semantics.
"""
from __future__ import annotations

import pytest
from django.core.cache import cache

from apps.command_intel import pilot as ci_pilot
from apps.command_intel.models import PilotDirective
from apps.identity.models import RoleAssignment
from apps.pilots.briefing import partition_briefing, unified_quest_queue
from apps.readiness.models import PilotRecommendation
from apps.readiness.pilot import cache_key as rd_cache_key
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac
from core.features import set_disabled

FAKE_DIGEST = {
    "headline": {"kind": "operation", "text": "Prep for Op TESTFIRE: you're ready for 1/2 doctrines.",
                 "url": "/operations/1/"},
    "items": [
        {"kind": "train", "text": "Train Caldari Battlecruiser V — unlocks 2 doctrine(s).", "url": "/skills/"},
        {"kind": "srp", "text": "1 loss(es) eligible for SRP (~140,000,000 ISK). Submit a claim.", "url": "/srp/"},
        {"kind": "task", "text": "You have 2 open task(s).", "url": "/tasks/"},
    ],
}
RD_PAYLOAD = {"overall": 62, "contributions": {},
              "facets": {"doctrine": 80, "combat": 40, "logistics": None,
                         "strategic": 55, "activity": 70, "contribution": 30}}


def _member(django_user_model, suffix, cid):
    u = django_user_model.objects.create(username=f"eve:cc{suffix}")
    RoleAssignment.objects.create(user=u, role=ensure_role(rbac.ROLE_MEMBER))
    EveCharacter.objects.create(character_id=cid, user=u, name=f"CC{suffix}",
                                is_main=True, is_corp_member=True)
    return u


def _prime(user, cid, *, directive=True, reco_kwargs=None):
    """Warm both engines' caches; optionally persist one item in each.

    Both quest logs are scoped to the PILOT (LP-3), so the rows must name the character they
    were computed for or the dashboard — which reads the ACTIVE pilot's log — will not see them.
    """
    character = EveCharacter.objects.get(character_id=cid)
    d = None
    if directive:
        d = PilotDirective.objects.create(
            user=user, character=character,
            slug="fleet_size.shield-ferox/train", constraint_key="fleet_size.shield-ferox",
            category="skill", title="Train into Shield Ferox", detail="Relieves the corp's shortage.",
            leverage=75, points=12, action_url="/skills/",
        )
    cache.set(ci_pilot.cache_key(cid), {"directives": []})
    cache.set(rd_cache_key(cid, user.pk), RD_PAYLOAD)
    r = None
    if reco_kwargs is not None:
        reco_kwargs.setdefault("character_id", cid)
        r = PilotRecommendation.objects.create(user=user, **reco_kwargs)
    return d, r


# --- unit: composition layer ---------------------------------------------------
def test_partition_briefing_routes_each_kind_to_exactly_one_home():
    signals, advice, claimable = partition_briefing(
        {**FAKE_DIGEST, "items": [*FAKE_DIGEST["items"],
                                  {"kind": "task_open", "text": "3 task(s) open to claim.", "url": "/tasks/"}]}
    )
    assert [it["kind"] for it in signals] == ["operation", "srp", "task"]
    assert [it["kind"] for it in advice] == ["train"]
    assert [it["kind"] for it in claimable] == ["task_open"]


@pytest.mark.django_db
def test_unified_queue_merges_both_engines_and_dedupes(django_user_model, sde):
    user = _member(django_user_model, "u", 7301)
    d, _ = _prime(user, 7301, reco_kwargs=dict(
        category="asset", ref_type="type", ref_id="1", title="Fit your Ferox",
        detail="Modules missing.", priority=78, points=6, action_url="/doctrines/"))
    # Duplicates that MUST be suppressed while the CI order exists:
    PilotRecommendation.objects.create(user=user, category="skill", ref_type="doctrine",
                                       ref_id="2", title="Train into Shield Ferox",
                                       priority=100, points=12, action_url="/skills/")
    PilotRecommendation.objects.create(user=user, category="role", ref_type="activity",
                                       ref_id="join_op", title="Fly a fleet this week",
                                       priority=40, points=4, action_url="/operations/")
    PilotRecommendation.objects.create(user=user, category="ship", ref_type="mandatory_ship",
                                       ref_id="3", title="Get your Shield Ferox",
                                       priority=90, points=8, action_url="/store/")

    recos = list(PilotRecommendation.objects.filter(user=user, state="open"))
    queue = unified_quest_queue([d], recos)

    titles = [q["title"] for q in queue]
    assert titles[0] == "Train into Shield Ferox"          # corp order ranks first
    assert queue[0]["engine"] == "ci" and queue[0]["corp_order"]
    assert queue[0]["form_url_name"] == "command_intel:directive_action"
    assert "Fit your Ferox" in titles                      # unique readiness value survives
    assert titles.count("Train into Shield Ferox") == 1    # readiness skill duplicate dropped
    # The 'fly a fleet' filler is dropped only when CI carries its OWN join-op
    # fallback — here CI has a real order, so the readiness filler may stay.
    assert "Fly a fleet this week" in titles
    # A TRAIN order doesn't suppress the buy-hull ask (different actions) —
    # the same-hull dedup applies only between SHIP orders (see the ship test).
    assert "Get your Shield Ferox" in titles
    fit = next(q for q in queue if q["title"] == "Fit your Ferox")
    assert fit["engine"] == "readiness"
    assert fit["form_url_name"] == "readiness:reco_action"


# --- end-to-end: the merged page ------------------------------------------------
@pytest.mark.django_db
def test_dashboard_renders_one_queue_with_both_engines_forms(client, django_user_model, sde, monkeypatch):
    monkeypatch.setattr("apps.pilots.briefing.pilot_briefing", lambda user: FAKE_DIGEST)
    user = _member(django_user_model, "a", 7302)
    d, r = _prime(user, 7302, reco_kwargs=dict(
        category="asset", ref_type="type", ref_id="1", title="Fit your Ferox",
        detail="Modules missing.", priority=78, points=6, action_url="/doctrines/"))
    client.force_login(user)
    html = client.get("/dashboard/").content.decode()

    assert "Command Center" in html
    assert "Train into Shield Ferox" in html               # CI order (Priority One)
    assert "Priority one" in html and "high leverage" in html
    assert "Fit your Ferox" in html                        # readiness quest in the same queue
    assert f"/command/me/directive/{d.pk}/" in html        # CI form action
    assert f"/readiness/me/reco/{r.pk}/" in html           # readiness form action
    assert "Train Caldari Battlecruiser V" not in html     # digest advice suppressed by the queue
    # The pinned next-op row supersedes the digest's op signal (the fleet
    # appears once); with no Operation scheduled it shows the quiet state.
    assert "Prep for Op TESTFIRE" not in html
    assert "No ops scheduled." in html
    assert "eligible for SRP" in html                      # srp signal renders
    assert "You have 2 open task(s)" not in html           # tasks live in My work, not signals
    assert html.count("Why the corp needs this") == 1      # exactly one promoted card


@pytest.mark.django_db
def test_dashboard_readiness_panel_renders_from_warm_cache(client, django_user_model, sde):
    user = _member(django_user_model, "b", 7303)
    _prime(user, 7303, directive=False)
    client.force_login(user)
    html = client.get("/dashboard/").content.decode()
    assert "Pilot stats" in html
    assert "stroke-dasharray" in html                      # the readiness ring
    assert "focus here" in html                            # lowest-facet pointer
    assert "no data" in html                               # null facet teaching state

    set_disabled(["readiness"])
    html = client.get("/dashboard/").content.decode()
    # Assert on the panel's own section id, not the header text — "Pilot stats" also
    # appears as a checkbox label in the "Customize panels" control (PCC-4).
    assert 'id="stats"' not in html
    assert 'href="/readiness/' not in html
    set_disabled([])


@pytest.mark.django_db
def test_digest_advice_is_the_fallback_when_both_engines_off(client, django_user_model, sde, monkeypatch):
    monkeypatch.setattr("apps.pilots.briefing.pilot_briefing", lambda user: FAKE_DIGEST)
    user = _member(django_user_model, "c", 7304)
    client.force_login(user)
    set_disabled(["command_intel_pilot", "readiness"])
    html = client.get("/dashboard/").content.decode()
    assert "Train Caldari Battlecruiser V" in html
    assert "From today&#x27;s digest." in html or "From today's digest." in html
    set_disabled([])


@pytest.mark.django_db
def test_queue_empty_state_rewards_the_current_pilot(client, django_user_model, sde, monkeypatch):
    monkeypatch.setattr("apps.pilots.briefing.pilot_briefing",
                        lambda user: {"headline": None, "items": []})
    user = _member(django_user_model, "d", 7305)
    _prime(user, 7305, directive=False)  # warm caches, zero quests
    client.force_login(user)
    set_disabled(["readiness"])  # readiness recos off so the queue is truly empty
    html = client.get("/dashboard/").content.decode()
    assert "You&#x27;re current" in html or "You're current" in html
    set_disabled([])


@pytest.mark.django_db
def test_disabled_sibling_features_neither_advertise_nor_dead_link(
        client, django_user_model, sde, monkeypatch):
    from apps.industry.models import IndustryProject

    monkeypatch.setattr("apps.pilots.briefing.pilot_briefing", lambda user: FAKE_DIGEST)
    user = _member(django_user_model, "e", 7306)
    _prime(user, 7306)
    IndustryProject.objects.create(name="Ferox batch 7", status=IndustryProject.Status.ACTIVE)
    client.force_login(user)

    html = client.get("/dashboard/").content.decode()
    assert "Ferox batch 7" in html

    # "planetary" is a separate feature that also lives under the /industry/ URL
    # prefix (/industry/pi/), so disable it too to clear every /industry/ link.
    set_disabled(["industry", "planetary", "tasks"])
    html = client.get("/dashboard/").content.decode()
    assert "Ferox batch 7" not in html
    assert 'href="/industry/' not in html
    assert 'href="/tasks/' not in html
    set_disabled([])


@pytest.mark.django_db
def test_quest_actions_land_back_on_the_dashboard(client, django_user_model, sde):
    user = _member(django_user_model, "f", 7307)
    d, r = _prime(user, 7307, reco_kwargs=dict(
        category="asset", ref_type="type", ref_id="1", title="Fit your Ferox",
        detail="", priority=78, points=6, action_url="/doctrines/"))
    client.force_login(user)

    resp = client.post(f"/command/me/directive/{d.pk}/", {"action": "done"}, follow=True)
    assert resp.redirect_chain[-1][0] == "/dashboard/"
    assert "Order complete" in resp.content.decode()

    resp = client.post(f"/readiness/me/reco/{r.pk}/", {"action": "done"}, follow=True)
    assert resp.redirect_chain[-1][0] == "/dashboard/"
    assert "marked done" in resp.content.decode()
    r.refresh_from_db()
    assert r.state == PilotRecommendation.State.DONE


@pytest.mark.django_db
def test_officer_command_deck_hidden_from_members_and_follows_briefing_key(
        client, django_user_model, sde, monkeypatch):
    monkeypatch.setattr("apps.pilots.briefing.pilot_briefing", lambda user: FAKE_DIGEST)
    member = _member(django_user_model, "g", 7308)
    _prime(member, 7308)
    client.force_login(member)
    html = client.get("/dashboard/").content.decode()
    assert "Command deck" not in html
    assert "SRP exposure" not in html

    RoleAssignment.objects.create(user=member, role=ensure_role(rbac.ROLE_OFFICER))
    html = client.get("/dashboard/").content.decode()
    assert "Command deck" in html and "SRP exposure" in html

    # The 'briefing' key owns only the corp-metric tiles; the role-gated officer
    # quick-nav keeps its deck when the digest is off.
    set_disabled(["briefing"])
    html = client.get("/dashboard/").content.decode()
    assert "Command deck" in html
    assert "SRP exposure" not in html         # leadership metric strata gone
    assert "Officer actions" in html          # quick-nav survives
    assert "Train into Shield Ferox" in html  # quest log unaffected
    set_disabled([])


@pytest.mark.django_db
def test_ship_only_ci_orders_do_not_silence_readiness_training_quests(django_user_model, sde):
    # Regression: the corp's binding constraints can be hull-stock flavoured, so
    # CI emits ONLY 'Stage a X hull' ship orders. The pilot's readiness training
    # quests must survive — dropping them left zero training advice anywhere.
    user = _member(django_user_model, "s", 7310)
    ship_order = PilotDirective.objects.create(
        user=user, slug="doctrine_stock.ferox/stage-hull", constraint_key="doctrine_stock.ferox",
        category="ship", title="Stage a Ferox hull", detail="", leverage=70, points=8,
        action_url="/store/",
    )
    train = PilotRecommendation.objects.create(
        user=user, category="skill", ref_type="doctrine", ref_id="1",
        title="Train into Retribution", priority=100, points=12, action_url="/skills/")
    fallback = PilotRecommendation.objects.create(
        user=user, category="role", ref_type="activity", ref_id="join_op",
        title="Fly a fleet this week", priority=40, points=4, action_url="/operations/")
    same_hull = PilotRecommendation.objects.create(
        user=user, category="ship", ref_type="mandatory_ship", ref_id="9",
        title="Get your Ferox", priority=90, points=8, action_url="/store/")

    queue = unified_quest_queue([ship_order], [train, fallback, same_hull])
    titles = [q["title"] for q in queue]
    assert "Train into Retribution" in titles   # survives a ship-only CI queue
    assert "Fly a fleet this week" in titles    # CI carries no fallback here either
    assert "Get your Ferox" not in titles       # ship-vs-ship same-hull dedup holds
    assert titles[0] == "Stage a Ferox hull"    # corp order still ranks first


@pytest.mark.django_db
def test_drained_queue_shows_the_earned_empty_state_not_digest_advice(
        client, django_user_model, sde, monkeypatch):
    # Regression: dismissing your last quest must not resurface the same advice
    # as un-dismissable digest rows — the engines are ON, so the digest yields.
    monkeypatch.setattr("apps.pilots.briefing.pilot_briefing", lambda user: FAKE_DIGEST)
    user = _member(django_user_model, "t", 7311)
    d, _ = _prime(user, 7311)
    client.force_login(user)
    client.post(f"/command/me/directive/{d.pk}/", {"action": "dismiss"})
    set_disabled(["readiness"])  # only the CI engine in play, now drained
    html = client.get("/dashboard/").content.decode()
    assert "Train Caldari Battlecruiser V" not in html
    assert "You&#x27;re current" in html or "You're current" in html
    set_disabled([])


@pytest.mark.django_db
def test_show_all_toggle_renders_every_quest_exactly_once(client, django_user_model, sde):
    user = _member(django_user_model, "v", 7312)
    _prime(user, 7312, directive=False)
    character = EveCharacter.objects.get(character_id=7312)
    for i in range(9):
        PilotDirective.objects.create(
            user=user, character=character,  # the quest log belongs to the pilot (LP-3)
            slug=f"fleet_size.d{i}/train", constraint_key=f"fleet_size.d{i}",
            category="skill", title=f"Train into Quest{i}", detail="x",
            leverage=90 - i, points=10, action_url="/skills/",
        )
    client.force_login(user)
    html = client.get("/dashboard/").content.decode()
    assert "Show all 9 quests" in html
    for i in range(9):
        assert html.count(f"Train into Quest{i}") == 1, f"Quest{i} duplicated or dropped"


@pytest.mark.django_db
def test_readiness_trend_and_week_delta_render_from_the_cached_payload(
        client, django_user_model, sde):
    user = _member(django_user_model, "w", 7313)
    _prime(user, 7313, directive=False)
    cache.set(rd_cache_key(7313, user.pk), {**RD_PAYLOAD, "trend": [50, 52, 55, 58, 60, 61, 62, 64, 66, 68],
                                   "week_delta": 6})
    client.force_login(user)
    html = client.get("/dashboard/").content.decode()
    assert 'title="last 14 days"' in html   # the sparkline wrapper
    assert "+6" in html and "this week" in html


@pytest.mark.django_db
def test_signals_and_quest_actions_respect_their_owning_features(
        client, django_user_model, sde, monkeypatch):
    # The in-view kind/url feature filter: disabled destinations disappear from
    # signals, and a quest row keeps its title but loses its action button.
    monkeypatch.setattr("apps.pilots.briefing.pilot_briefing", lambda user: FAKE_DIGEST)
    user = _member(django_user_model, "x", 7314)
    _prime(user, 7314)  # directive action_url=/skills/
    client.force_login(user)
    set_disabled(["srp", "operations", "skills"])
    html = client.get("/dashboard/").content.decode()
    assert html.count("/dashboard/") > 0  # page renders
    assert 'href="/srp/' not in html
    assert 'href="/operations/' not in html
    assert 'href="/skills/' not in html
    assert "Train into Shield Ferox" in html  # the quest survives, sans button
    set_disabled([])


@pytest.mark.django_db
def test_my_losses_srp_claim_form_renders_for_eligible_losses(
        client, django_user_model, sde, monkeypatch):
    from decimal import Decimal

    from django.utils import timezone

    from apps.killboard.models import Killmail

    monkeypatch.setattr("apps.srp.services.eligibility",
                        lambda *a, **kw: {"eligible": True, "payout": Decimal("38000000")})
    user = _member(django_user_model, "y", 7315)
    _prime(user, 7315, directive=False)
    Killmail.objects.create(
        killmail_id=91, killmail_hash="y", killmail_time=timezone.now(),
        solar_system_id=30000142, victim_ship_type_id=587, involves_home_corp=True,
        home_corp_role=Killmail.HomeRole.VICTIM, victim_character_id=7315,
        total_value=Decimal("38000000"),
    )
    client.force_login(user)
    html = client.get("/dashboard/").content.decode()
    assert "My recent losses" in html
    assert 'name="killmail_id" value="91"' in html
    assert "Claim SRP" in html

    # Killboard off + SRP on: the claim surface stands alone; the combat log and
    # the killmail-detail link degrade. (/killboard/intel/ may legitimately stay
    # in the nav — the watchlists ride the separate 'intel' feature key.)
    set_disabled(["killboard"])
    html = client.get("/dashboard/").content.decode()
    assert "My recent losses" in html and "Claim SRP" in html
    # The header's "· corp · 7d" suffix is unique to the combat-log panel — "Combat log"
    # on its own now also appears as a "Customize panels" checkbox label (PCC-4).
    assert "· corp · 7d" not in html
    assert 'href="/killboard/91/"' not in html
    set_disabled([])


@pytest.mark.django_db
def test_characterless_officer_keeps_the_deck(client, django_user_model, sde):
    officer = django_user_model.objects.create(username="eve:ccdeck")
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_MEMBER))
    RoleAssignment.objects.create(user=officer, role=ensure_role(rbac.ROLE_OFFICER))
    client.force_login(officer)
    html = client.get("/dashboard/").content.decode()
    assert "Link a character" in html      # the pilot body is the CTA
    assert "Command deck" in html          # oversight survives without a character
    assert "Officer actions" in html


# --- the absorbed URLs -----------------------------------------------------------
@pytest.mark.django_db
def test_absorbed_pages_redirect_with_their_old_gating(client, django_user_model, sde):
    user = _member(django_user_model, "h", 7309)
    client.force_login(user)

    for url in ("/pilots/briefing/", "/readiness/me/", "/command/me/", "/recommendations/mine/"):
        resp = client.get(url)
        assert resp.status_code == 302, f"{url} returned {resp.status_code}"
        assert resp["Location"] == "/dashboard/", f"{url} redirected to {resp['Location']}"

    # Old off-switch semantics survive on the stubs.
    set_disabled(["briefing", "command_intel_pilot", "recommendations"])
    assert client.get("/pilots/briefing/").status_code == 404
    set_disabled(["readiness"])
    assert client.get("/readiness/me/").status_code == 404
    set_disabled(["command_intel_pilot"])
    assert client.get("/command/me/").status_code == 404
    set_disabled([])


# --- the pilot-audit round: capacitor bar + the five "missing entirely" surfaces --
@pytest.mark.django_db
def test_doctrine_capacitor_bar_replaces_the_chip_wall(client, django_user_model, sde, monkeypatch):
    fake = [
        {"doctrine_id": i, "doctrine": f"Testdoc {i}", "status": s, "fit": "F", "missing_viable": []}
        for i, s in enumerate(
            ["optimal", "viable", "viable", "not_ready", "not_ready", "unknown"], start=1
        )
    ]
    monkeypatch.setattr("apps.identity.views.readiness_summary_for_character", lambda c: fake)
    user = _member(django_user_model, "cb", 7320)
    _prime(user, 7320, directive=False)
    client.force_login(user)
    html = client.get("/dashboard/").content.decode()

    assert "50% fly-ready" in html                       # 3 of 6, as the hero line
    assert "All doctrines →" in html and 'href="/doctrines/"' in html
    assert "My readiness →" in html
    assert "not ready" in html and "unknown" in html     # legend counts as text
    assert "Testdoc 4" not in html                       # the chip wall is gone


@pytest.mark.django_db
def test_pinned_next_op_row(client, django_user_model, sde, monkeypatch):
    from datetime import timedelta

    from django.utils import timezone

    from apps.operations.models import Operation

    user = _member(django_user_model, "op", 7321)
    _prime(user, 7321, directive=False)
    op = Operation.objects.create(
        name="Thera Crash", target_at=timezone.now() + timedelta(hours=3),
        status=Operation.Status.PLANNED,
    )
    monkeypatch.setattr(
        "apps.operations.services.upcoming_for_pilot",
        lambda c: {"op": op, "rows": [], "ready": 1, "total": 2, "pct": 50},
    )
    client.force_login(user)
    html = client.get("/dashboard/").content.decode()
    assert "Next op: Thera Crash" in html
    assert "fly 1/2" in html                             # can-I-fly-it chip
    assert "RSVP →" in html and f'href="/operations/{op.pk}/"' in html

    # Operations off: the pinned row and its quiet state disappear entirely.
    set_disabled(["operations"])
    html = client.get("/dashboard/").content.decode()
    assert "Next op:" not in html and "No ops scheduled." not in html
    set_disabled([])


@pytest.mark.django_db
def test_my_losses_show_srp_lifecycle_and_payout_signal(client, django_user_model, sde, monkeypatch):
    from decimal import Decimal

    from django.utils import timezone

    from apps.killboard.models import Killmail
    from apps.srp.models import SrpClaim

    monkeypatch.setattr("apps.srp.services.eligibility",
                        lambda *a, **kw: {"eligible": True, "payout": Decimal("38000000")})
    user = _member(django_user_model, "pd", 7322)
    _prime(user, 7322, directive=False)
    km = Killmail.objects.create(
        killmail_id=92, killmail_hash="pd", killmail_time=timezone.now(),
        solar_system_id=30000142, victim_ship_type_id=587, involves_home_corp=True,
        home_corp_role=Killmail.HomeRole.VICTIM, victim_character_id=7322,
        total_value=Decimal("38000000"),
    )
    SrpClaim.objects.create(
        killmail=km, claimant=user, status=SrpClaim.Status.PAID,
        loss_value=Decimal("38000000"), computed_payout=Decimal("38000000"),
        decided_at=timezone.now(),
    )
    client.force_login(user)
    html = client.get("/dashboard/").content.decode()
    assert "SRP paid ·" in html                          # lifecycle chip, not "claimed"
    assert "SRP payout landed" in html                   # good-news signal, links /srp/
    assert "Claim SRP" not in html                       # no second claim offered
    assert "My SRP →" in html                            # header link to srp:mine


@pytest.mark.django_db
def test_training_row_reads_the_skillqueue_snapshot(client, django_user_model, sde):
    from datetime import timedelta

    from django.utils import timezone

    from apps.characters.models import SkillQueueSnapshot

    user = _member(django_user_model, "tq", 7323)
    _prime(user, 7323, directive=False)
    char = EveCharacter.objects.get(character_id=7323)
    now = timezone.now()
    SkillQueueSnapshot.objects.create(
        character=char, is_latest=True,
        entries=[{
            "skill_id": 3300, "finished_level": 5, "queue_position": 0,
            "start_date": (now - timedelta(hours=1)).isoformat(),
            "finish_date": (now + timedelta(hours=20)).isoformat(),
        }],
    )
    client.force_login(user)
    html = client.get("/dashboard/").content.decode()
    assert "Training Gunnery V" in html
    assert "queue ends" in html                          # <2d left → the warning chip

    # Empty queue → the loud warning the audit asked for.
    SkillQueueSnapshot.objects.filter(character=char).update(is_latest=False)
    SkillQueueSnapshot.objects.create(character=char, is_latest=True, entries=[])
    html = client.get("/dashboard/").content.decode()
    assert "Skill queue empty" in html


@pytest.mark.django_db
def test_personal_combat_week_line(client, django_user_model, sde):
    from decimal import Decimal

    from django.utils import timezone

    from apps.killboard.models import Killmail, KillmailParticipant

    user = _member(django_user_model, "cw", 7324)
    _prime(user, 7324, directive=False)
    km = Killmail.objects.create(
        killmail_id=93, killmail_hash="cw", killmail_time=timezone.now(),
        solar_system_id=30000142, victim_ship_type_id=587, involves_home_corp=True,
        home_corp_role=Killmail.HomeRole.ATTACKER, total_value=Decimal("61000000"),
    )
    KillmailParticipant.objects.create(
        killmail=km, role=KillmailParticipant.Role.ATTACKER, seq=0, character_id=7324,
    )
    client.force_login(user)
    html = client.get("/dashboard/").content.decode()
    assert "You this week:" in html
    assert "61.00M" in html                              # my ISK destroyed
    assert "My stats →" in html


@pytest.mark.django_db
def test_my_services_inflight_rows(client, django_user_model, sde, monkeypatch):
    from apps.buyback.models import BuybackOffer
    from apps.logistics.models import CourierContract
    from apps.store.models import StoreOrder

    # Audience gating is each service's own (not core.features) — force-open
    # all three; the rows themselves are what this test pins.
    monkeypatch.setattr("apps.buyback.services.can_access", lambda u: True)
    monkeypatch.setattr("apps.store.services.can_access", lambda u: True)
    monkeypatch.setattr("apps.logistics.services.can_access", lambda u: True)
    user = _member(django_user_model, "sv", 7325)
    _prime(user, 7325, directive=False)
    BuybackOffer.objects.create(seller=user, status=BuybackOffer.Status.OPEN)
    StoreOrder.objects.create(
        buyer=user, status=StoreOrder.Status.IN_PRODUCTION,
        kind=StoreOrder.Kind.DOCTRINE_FIT, ship_type_id=587,
    )
    CourierContract.objects.create(
        created_by=user, status=CourierContract.Status.OUTSTANDING,
        origin_name="Jita IV-4", dest_name="Staging",
    )
    client.force_login(user)
    html = client.get("/dashboard/").content.decode()
    assert "1 buyback offer awaiting a buyer or payout" in html
    assert "1 corp-store order in progress" in html
    assert "1 freight contract in flight" in html
