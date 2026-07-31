"""Static-analysis follow-up: redirect targets and API error bodies stay attacker-proof.

Three code-scanning alerts landed on the killboard's redirect/error seams. Two were false
positives, one was a real (if modest) bug; all three are pinned here so the reasoning survives a
refactor instead of living in a dismissal comment:

* ``console_signatures.signature_admin_action`` bounced back to the search screen with the
  operator's raw search term glued onto the URL (``f"{url}?q={q}"``). The *host* was never
  attacker-controlled — the path comes from ``reverse()`` and the term only ever reaches the
  query string — but an unescaped ``&``/``#``/``=`` injected or truncated query parameters. Now
  ``urlencode``d, and both properties are asserted: one faithful ``q``, and a same-origin target.
* ``views.adversary_watch`` composed a view name from the URL's ``kind`` segment. ``redirect()``
  falls back to treating its argument as a *literal URL* when ``reverse()`` fails and the string
  contains a "/" or ".", so an unvalidated kind would have been a genuine redirect sink. It was
  already dominated by the ``adversary.is_valid_kind`` allowlist; the redirect target is now
  chosen from the literal ``_ADVERSARY_ROUTES`` table as well. Both the allowlist and the
  table's parity with ``adversary.ENTITY_KINDS`` are pinned below.
* ``api.stream`` surfaces ``stream.TopicError`` to the caller. It is the topic parser's own
  ``ValueError`` subclass, always raised with a hand-written sentence — no traceback, no
  exception chain, no internal state — so surfacing it is diagnosability, not disclosure. What
  the wire error must NOT do is reflect unbounded caller input back, which is asserted here.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from apps.identity.models import RoleAssignment
from apps.killboard import adversary, stream, views
from apps.killboard.api import stream as api_stream
from apps.killboard.models import (
    CombatSignature,
    KillboardApiToken,
    SignatureBackground,
    Watchlist,
    WatchlistEntry,
)
from apps.sso.models import EveCharacter
from apps.sso.services import ensure_role
from core import rbac

pytestmark = pytest.mark.django_db

STREAM = "/api/killboard/stream/"
AJSON = {"HTTP_ACCEPT": "application/json"}


def _user(django_user_model, username, cid, role):
    """An enrolled pilot at the given role (the console/adversary tests' shared shape)."""
    user = django_user_model.objects.create(username=username)
    RoleAssignment.objects.create(user=user, role=ensure_role(role))
    EveCharacter.objects.create(character_id=cid, user=user, name=username,
                                is_main=True, is_corp_member=True)
    return user


# --------------------------------------------------------------------------- #
#  Signature moderation: the carried-over search term is encoded, not glued on
# --------------------------------------------------------------------------- #
def _signature(django_user_model) -> CombatSignature:
    owner = django_user_model.objects.create(username="sig-owner")
    char = EveCharacter.objects.create(character_id=9100, user=owner, name="Owner",
                                       is_main=True, is_corp_member=True)
    bg = SignatureBackground.objects.create(key="nebula-ql", name="Nebula", enabled=True)
    return CombatSignature.objects.create(
        character=char, name="Sig", background=bg, layout="identity", size_preset="standard",
        mode=CombatSignature.Mode.LIVE, status=CombatSignature.Status.ACTIVE,
        config={"components": ["pilot_name"], "period": "30d", "featured_trophy_ids": [],
                "show_timestamp": False, "theme": "gold"},
    )


# Search terms an operator can legitimately type that are ALSO query-string metacharacters.
HOSTILE_TERMS = [
    "Foo&next=/evil",                 # would have injected a second parameter
    "Foo#frag",                       # would have truncated the URL at the fragment
    "a=b&c=d",                        # would have injected two more parameters
    "Pilot Name",                     # plain space: must survive as a space, not "+"-mangled
    "Kaçak Pırat",                    # non-ASCII: must round-trip through percent-encoding
]


@pytest.mark.parametrize("term", HOSTILE_TERMS)
def test_moderation_redirect_carries_the_search_term_verbatim(client, django_user_model, term,
                                                              monkeypatch):
    """Whatever the operator searched for comes back as exactly one faithful ``q`` parameter.

    Fails against the old ``f"{url}?q={q}"``: ``Foo&next=/evil`` arrived as two parameters and
    ``Foo#frag`` arrived as a fragment, so ``q`` was no longer the term that was typed.
    """
    monkeypatch.setattr("apps.killboard.tasks.signature_render_task.delay",
                        lambda *a, **k: None)
    sig = _signature(django_user_model)
    client.force_login(_user(django_user_model, "off-q", 9101, rbac.ROLE_OFFICER))

    resp = client.post(
        reverse("admin_audit:signature_admin_action", args=[sig.pk, "regenerate"]), {"q": term}
    )
    assert resp.status_code == 302
    parts = urlsplit(resp.url)
    assert parse_qs(parts.query) == {"q": [term]}          # one parameter, unmangled
    assert parts.fragment == ""                            # nothing truncated into a fragment


def test_moderation_redirect_never_leaves_this_origin(client, django_user_model, monkeypatch):
    """The bounce-back target is always the reversed search path on this origin.

    The search term cannot reach the scheme, the host or the path — the classic
    ``//evil.example`` and ``https://evil.example`` payloads stay inside ``q``.
    """
    monkeypatch.setattr("apps.killboard.tasks.signature_render_task.delay",
                        lambda *a, **k: None)
    sig = _signature(django_user_model)
    client.force_login(_user(django_user_model, "off-o", 9102, rbac.ROLE_OFFICER))
    search = reverse("admin_audit:signature_search")

    for payload in ("//evil.example/x", "https://evil.example/x", "\\\\evil.example"):
        resp = client.post(
            reverse("admin_audit:signature_admin_action", args=[sig.pk, "regenerate"]),
            {"q": payload},
        )
        parts = urlsplit(resp.url)
        assert parts.scheme == "" and parts.netloc == ""   # relative, same-origin
        assert parts.path == search
        assert parse_qs(parts.query) == {"q": [payload]}


def test_moderation_redirect_omits_an_empty_query(client, django_user_model, monkeypatch):
    """An empty search still lands on the bare search URL (no dangling ``?q=``)."""
    monkeypatch.setattr("apps.killboard.tasks.signature_render_task.delay",
                        lambda *a, **k: None)
    sig = _signature(django_user_model)
    client.force_login(_user(django_user_model, "off-e", 9103, rbac.ROLE_OFFICER))
    resp = client.post(
        reverse("admin_audit:signature_admin_action", args=[sig.pk, "regenerate"]), {"q": "  "}
    )
    assert resp.url == reverse("admin_audit:signature_search")


# --------------------------------------------------------------------------- #
#  Adversary watch: the redirect target comes from an allowlist, never the URL
# --------------------------------------------------------------------------- #
def test_adversary_route_table_covers_exactly_the_valid_kinds():
    """``_ADVERSARY_ROUTES`` and ``adversary.ENTITY_KINDS`` are one allowlist, not two.

    Adding a kind to ``ENTITY_KINDS`` without a literal route here must fail the build rather
    than fall through to a composed (and therefore attacker-influenced) redirect target.
    """
    assert set(views._ADVERSARY_ROUTES) == set(adversary.ENTITY_KINDS)
    for kind, route in views._ADVERSARY_ROUTES.items():
        assert route == f"killboard:adversary_{kind}"
        reverse(route, args=[1])       # every entry is a real, reversible view name


# Kinds that ``<str:kind>`` happily matches. Each contains a "." — the character that makes
# Django's ``resolve_url`` give up on ``reverse()`` and treat the string as a literal URL, so
# each of these is exactly what a redirect sink would need to be exploitable.
HOSTILE_KINDS = ["evil.example", "character.evil", "https:evil.example", "..", "chara.cter"]


@pytest.mark.parametrize("kind", HOSTILE_KINDS)
def test_adversary_watch_rejects_a_kind_outside_the_allowlist(client, django_user_model, kind):
    """A kind that is not on the allowlist is a 404 — no redirect, no watchlist row.

    This is the test that fails if the ``adversary.is_valid_kind`` guard is ever removed: the
    request then reaches the redirect (or the route lookup) with attacker-chosen text.
    """
    Watchlist.objects.create(name="Hostiles")
    client.force_login(_user(django_user_model, f"off-{abs(hash(kind))}", 9200, rbac.ROLE_OFFICER))

    resp = client.post(reverse("killboard:adversary_watch", args=[kind, 42]),
                       {"watchlist_id": ""})
    assert resp.status_code == 404
    assert not WatchlistEntry.objects.filter(entity_type=kind).exists()


def test_adversary_watch_still_returns_to_the_entity_page(client, django_user_model):
    """The legitimate flow is unchanged: add the entity, bounce back to its adversary page."""
    wl = Watchlist.objects.create(name="Hostiles")
    client.force_login(_user(django_user_model, "off-ok", 9201, rbac.ROLE_OFFICER))

    for kind in adversary.ENTITY_KINDS:
        resp = client.post(reverse("killboard:adversary_watch", args=[kind, 4242]),
                           {"watchlist_id": wl.id})
        assert resp.status_code == 302
        assert resp.url == reverse(f"killboard:adversary_{kind}", args=[4242])
        assert WatchlistEntry.objects.filter(
            watchlist=wl, entity_type=kind, entity_id=4242).exists()


# --------------------------------------------------------------------------- #
#  Stream API: a topic error is a curated sentence, and it is bounded
# --------------------------------------------------------------------------- #
def _member_headers(django_user_model):
    user = _user(django_user_model, "eve:stream", 9300, rbac.ROLE_MEMBER)
    _tok, raw = KillboardApiToken.issue(user, name="t")
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}", **AJSON}


# Anything that would betray an implementation detail rather than describe the caller's mistake.
LEAK_MARKERS = ["Traceback", "File \"", ", line ", "apps/killboard", "apps.killboard",
                "Object at 0x", "SELECT ", "psycopg", "settings."]


@pytest.mark.parametrize("topics,expected_status", [
    ("bogus:1", 400),
    ("not-a-topic", 400),
    ("system:notanint", 400),
    ("iskband:", 400),
])
def test_topic_error_body_is_a_curated_sentence(django_user_model, topics, expected_status):
    """The 400 body explains the bad topic and nothing else — no traceback, no internals."""
    resp = Client().get(f"{STREAM}?mode=poll&topics={topics}", **_member_headers(django_user_model))
    assert resp.status_code == expected_status
    detail = resp.json()["detail"]
    assert detail.endswith(".")                       # a sentence, not a repr
    for marker in LEAK_MARKERS:
        assert marker not in detail


@override_settings(KILLBOARD_API_PUBLIC_READ=True)
def test_gated_topic_is_403_without_leaking_the_gate_internals():
    """A member-only topic 403s for an anonymous public reader with the same curated shape."""
    resp = Client().get(f"{STREAM}?mode=poll&topics=needs-srp", **AJSON)
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail == "Topic 'needs-srp' requires membership."


def test_topic_parser_itself_never_embeds_internal_state():
    """Pin the raise sites: every ``TopicError`` message names only the caller's own topic.

    Asserted at the source so the API-layer claim above cannot be quietly invalidated by a new
    raise site that interpolates an exception, a queryset or a settings value.
    """
    for bad in ("bogus:1", "not-a-topic", "system:x", "pilot:", "iskband:nope"):
        with pytest.raises(stream.TopicError) as exc:
            stream.build_matcher(bad, member=True)
        message = str(exc.value)
        assert bad.split(":")[0] in message
        for marker in LEAK_MARKERS:
            assert marker not in message


def test_error_detail_does_not_reflect_unbounded_caller_input(django_user_model):
    """A caller cannot have kilobytes of its own input echoed back in the error body.

    The parser quotes the offending topic verbatim, so an 8 KB topic token would otherwise come
    straight back on the wire. Fails if ``api_stream._bounded`` is dropped from the handler.
    """
    huge = "x" * 8000
    resp = Client().get(f"{STREAM}?mode=poll&topics={huge}", **_member_headers(django_user_model))
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert len(detail) <= api_stream._MAX_DETAIL_CHARS
    assert detail.endswith("…")                  # truncation is visible, not silent
    assert huge not in detail


def test_bounding_leaves_every_real_message_intact():
    """The cap is a transport ceiling, not validation: real topic errors pass through whole."""
    for bad in ("bogus:1", "shipclass", "system:notanint", "needs-srp"):
        try:
            stream.build_matcher(bad, member=False)
        except stream.TopicError as exc:
            assert api_stream._bounded(str(exc)) == str(exc)
        else:  # pragma: no cover — "needs-srp" is member-gated, the rest are malformed
            pytest.fail(f"expected TopicError for {bad!r}")
