"""The container-image scan's *receiving* end — where a host-side control meets a human.

The scan itself runs on the host and is tested there (``scripts/tests/``). What is tested
here is everything that happens after the pipe, and the reason it needs its own file is
that this side has a threat the dependency audit does not: **the input is untrusted**.
pip-audit is a subprocess we launched; a scan document is bytes another process wrote,
possibly truncated mid-write, possibly from a newer script, possibly self-contradictory
because trivy was killed halfway through. Acceptance:

* a finding opens exactly one director finding, and a genuinely clean scan closes it;
* nothing that failed, was rejected, or only scanned *part* of the deployment can close
  anything — "we could not tell" must never be filed as "all clear";
* a malformed or truncated document is refused safely, and says why;
* a change reaches a human exactly once; a repeat is silent;
* a notification fault cannot lose the scan result;
* an advisory with no upstream fix does not fail the gate — a gate that fails every day
  gets switched off, and then the deployment only *looks* covered;
* a result nobody has refreshed reads as ``unknown``, never as green.

Nothing here needs docker, trivy, or a network: the documents are built by hand, which is
also the point — a hand-built document is exactly what an attacker or a broken producer
would send.
"""
from __future__ import annotations

import io
import json
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from apps.admin_audit import image_scan
from apps.admin_audit import tasks as audit_tasks
from apps.admin_audit.models import AppSetting
from apps.pingboard import config
from apps.pingboard.models import Alert
from apps.recommendations.models import Recommendation

pytestmark = pytest.mark.django_db

_OPEN = [Recommendation.State.NEW, Recommendation.State.ACKNOWLEDGED]


@pytest.fixture(autouse=True)
def _reset_notifications_config():
    """The relay rides the ADM-3 event policy; leave no override behind."""
    config.reset("notifications")
    config.reset("general")
    yield
    config.reset("notifications")
    config.reset("general")


# --- document builders --------------------------------------------------------
def _vuln(vid, *, image="forca:prod", fix=("1.2.3",), severity="HIGH", package="openssl"):
    return {
        "id": vid, "severity": severity, "package": package, "version": "1.0.0",
        "fix_versions": list(fix), "image": image, "services": ["web"],
        "url": f"https://avd.aquasec.com/{vid}",
    }


def _doc(*vulns, complete=True, unscannable=(), status=None, images=None, **over):
    """A schema-1 scan document that is internally consistent by construction.

    Built the way ``scripts/image_scan_report.py`` builds it, so a test that wants an
    *inconsistent* document has to say so explicitly with ``**over`` — which keeps the
    "this document lies" cases visible instead of accidental.
    """
    vulns = list(vulns)
    unscannable = list(unscannable)
    if images is None:
        images = [{
            "image": "forca:prod", "image_id": "sha256:aaa", "services": ["web", "worker"],
            "containers": ["forca-web-1"], "os": "debian 13.5",
            "vuln_count": len(vulns),
            "fixable_count": sum(1 for v in vulns if v["fix_versions"]),
        }]
    if status is None:
        status = "error" if not images else ("vulnerable" if vulns else "ok")
    doc = {
        "schema": 1, "scanner": "trivy", "scanner_version": "0.70.0",
        "source": "running-containers", "trigger": "scheduled", "host": "forca-prod",
        "as_of": timezone.now().isoformat(),
        "severity_floor": "HIGH", "pkg_types": "os",
        "status": status,
        "complete": complete and not unscannable and bool(images),
        "image_count": len(images) + len(unscannable), "scanned_count": len(images),
        "vuln_count": len(vulns),
        "fixable_count": sum(1 for v in vulns if v["fix_versions"]),
        "truncated": False,
        "images": images, "unscannable": unscannable, "vulns": vulns,
    }
    doc.update(over)
    return json.dumps(doc)


_UNSCANNABLE = [{
    "image": "nginx:1.30-alpine", "image_id": "sha256:bbb", "services": ["nginx"],
    "containers": ["forca-nginx-1"],
    "error": "the running image is no longer in the local image store",
}]


def _ingest(document, **kw):
    return audit_tasks.ingest_image_scan(document, **kw)


def _finding():
    return Recommendation.objects.filter(
        subject_type="security", subject_id="image_scan", state__in=_OPEN
    ).first()


def _dependency_finding():
    return Recommendation.objects.filter(
        subject_type="security", subject_id="dependency_audit", state__in=_OPEN
    ).first()


def _relayed_alerts():
    return Alert.objects.filter(source_service="admin_audit")


def _stored():
    return AppSetting.get(image_scan.IMAGE_SCAN_SETTING_KEY) or {}


# --- 1. findings open, a clean scan closes ------------------------------------
def test_vulnerable_document_opens_one_director_finding():
    result = _ingest(_doc(_vuln("CVE-2026-1111")), trigger="scheduled")

    assert result["transition"] == "opened" and result["status"] == "vulnerable"
    rec = _finding()
    assert rec is not None
    assert rec.severity == 80 and rec.required_permission == "director"
    assert "CVE-2026-1111" in rec.message
    # The message has to say what to DO; the fix for an OS package is a base-image bump.
    assert "redeploy" in rec.message and "docker-compose.prod.yml" in rec.message
    assert _stored()["status"] == "vulnerable" and _stored()["trigger"] == "scheduled"


def test_a_rebuild_that_fixes_it_clears_its_own_finding():
    _ingest(_doc(_vuln("CVE-2026-1111")))
    assert _finding() is not None

    # The deploy that ships the new base image re-runs the scan on its way out.
    result = _ingest(_doc(), trigger="deploy")

    assert result["transition"] == "cleared"
    assert _finding() is None, "a rebuilt image must not keep claiming to be vulnerable"
    assert _stored()["status"] == "ok" and _stored()["trigger"] == "deploy"


def test_clean_document_with_no_open_finding_is_a_noop():
    result = _ingest(_doc())
    assert result["transition"] == "clean"
    assert _finding() is None and not _relayed_alerts().exists()


def test_the_same_advisory_in_a_second_image_is_a_real_change():
    """Identity is per (image, advisory): nginx and web carrying the same CVE are two
    pieces of work, and "fixed in one, still in the other" must not read as unchanged."""
    _ingest(_doc(_vuln("CVE-2026-1111", image="forca:prod")))
    result = _ingest(_doc(
        _vuln("CVE-2026-1111", image="forca:prod"),
        _vuln("CVE-2026-1111", image="nginx:1.30-alpine"),
    ))
    assert result["transition"] == "worsened"

    # ...and dropping back to one image is a change, not a no-op.
    result = _ingest(_doc(_vuln("CVE-2026-1111", image="nginx:1.30-alpine")))
    assert result["transition"] == "changed"


def test_the_two_scanners_keep_separate_findings():
    """One subject each. An image finding must never close, or be closed by, the Python
    dependency finding — they are fixed by different people doing different things."""
    from apps.admin_audit import dependency_audit as da

    audit_tasks._sync_recommendation({
        "status": "vulnerable", "vuln_count": 1, "as_of": timezone.now().isoformat(),
        "vulns": [{"name": "pillow", "version": "11.3.0", "id": "CVE-2026-9999",
                   "fix_versions": ["12.3.0"]}],
    })
    assert _dependency_finding() is not None

    _ingest(_doc())  # a clean IMAGE scan

    assert _dependency_finding() is not None, "a clean image scan says nothing about pip"
    assert da.AUDIT_SETTING_KEY != image_scan.IMAGE_SCAN_SETTING_KEY


# --- 2. nothing that failed or fell short can clear a finding -----------------
def test_error_document_never_clears_and_never_relays():
    _ingest(_doc(_vuln("CVE-2026-1111")))
    before = _relayed_alerts().count()

    result = _ingest(_doc(status="error", images=[], unscannable=_UNSCANNABLE))

    assert result["transition"] == "error" and result["relayed"] is False
    assert _finding() is not None, "a failed scan must never be mistaken for a rebuild"
    assert _relayed_alerts().count() == before, "an error is not news"


def test_incomplete_document_is_not_clean():
    """The load-bearing rule. A running container can outlive its own image, and a scan
    that skipped it learned nothing about it — reporting "0 findings" would shrink the
    denominator and call the remainder the whole truth."""
    _ingest(_doc(_vuln("CVE-2026-1111")))

    result = _ingest(_doc(complete=False, unscannable=_UNSCANNABLE))

    assert result["status"] == "error", "a partial scan is unknown, not clean"
    assert result["transition"] == "error"
    assert _finding() is not None, "an unscanned image cannot retire a finding"
    assert "could not be scanned" in result["error"]
    assert "nginx" in result["error"], "say WHICH image we are blind to"


def test_incomplete_document_still_raises_the_findings_it_did_see():
    """Incompleteness must not swallow real findings: what we read is still true."""
    result = _ingest(_doc(_vuln("CVE-2026-1111"), complete=False, unscannable=_UNSCANNABLE))

    assert result["status"] == "vulnerable" and result["transition"] == "opened"
    rec = _finding()
    assert "CVE-2026-1111" in rec.message
    assert "NOT SCANNED" in rec.message and "nginx" in rec.message


def test_a_document_claiming_clean_while_listing_findings_is_refused():
    """The dangerous forgery: a document that parses and lies. Trusting its headline
    ``status`` alone would close a live finding."""
    _ingest(_doc(_vuln("CVE-2026-1111")))

    result = _ingest(_doc(_vuln("CVE-2026-2222"), status="ok"))

    assert result["status"] == "error"
    assert _finding() is not None
    assert "clean scan alongside findings" in result["error"]


@pytest.mark.parametrize("over, why", [
    ({"vuln_count": 0, "vulns": [], "fixable_count": 0},
     "status says vulnerable, lists nothing"),
    ({"complete": True, "scanned_count": 5}, "claims completeness it does not have"),
    ({"fixable_count": 99}, "more fixable than found"),
    ({"truncated": True}, "claims a clip that did not happen"),
    ({"scanned_count": 9}, "scanned more than it set out to"),
])
def test_self_contradictory_documents_are_refused(over, why):
    _ingest(_doc(_vuln("CVE-2026-1111")))
    result = _ingest(_doc(_vuln("CVE-2026-2222"), **over))
    assert result["status"] == "error", why
    assert _finding() is not None


# --- 3. malformed input is refused safely ------------------------------------
@pytest.mark.parametrize("payload", [
    "",
    "not json at all",
    '{"schema": 1, "status": "ok", "compl',            # truncated mid-write
    "[]",                                              # valid JSON, wrong shape
    '{"schema": 2, "status": "ok", "complete": true}',  # a newer producer
    '{"schema": 1, "status": "fine", "complete": true}',
    '{"schema": 1, "status": "ok"}',                   # no completeness claim at all
    '{"schema": 1, "status": "ok", "complete": "yes"}',
])
def test_malformed_documents_are_refused_without_clearing_anything(payload):
    _ingest(_doc(_vuln("CVE-2026-1111")))

    result = _ingest(payload)

    assert result["status"] == "error"
    assert result["error"], "a refusal must say why — silence looks like 'never ran'"
    assert _finding() is not None
    # ...and the refusal is recorded, so "delivery is arriving and being rejected" is
    # visible on the ops page rather than indistinguishable from "the scanner never ran".
    assert _stored()["status"] == "error"


def test_an_oversized_document_is_refused_rather_than_read():
    """An unbounded read of a pipe turns a runaway producer into an OOM kill of the web
    container — a worse outage than anything the scan could report."""
    huge = io.StringIO("x" * (image_scan.MAX_DOCUMENT_CHARS + 10))
    text, error = image_scan.read_document(huge)
    assert text == "" and "exceeds" in error


def test_a_future_timestamp_cannot_fake_freshness():
    """``as_of`` is attacker-and-accident controlled input; a stamp in the future would
    make a stale result read as permanently fresh."""
    ahead = (timezone.now() + timedelta(days=30)).isoformat()
    _ingest(_doc(as_of=ahead))
    assert image_scan.scan_freshness()["age_seconds"] >= 0


# --- 4. the relay: once per change, silent otherwise -------------------------
def test_a_change_wakes_the_relay_and_a_repeat_does_not(monkeypatch):
    from apps.admin_audit import health_alert

    calls = []
    monkeypatch.setattr(health_alert, "scan_integration_health",
                        lambda: calls.append(1) or {"status": "alerted"})

    assert _ingest(_doc(_vuln("CVE-2026-1111")))["relayed"] is True    # opened → relay
    assert _ingest(_doc(_vuln("CVE-2026-1111")))["transition"] == "unchanged"
    assert _ingest(_doc(_vuln("CVE-2026-1111")))["relayed"] is False
    assert calls == [1], "a daily 'still 3 CVEs' ping is how a channel gets muted"

    _ingest(_doc())  # cleared → relay
    assert calls == [1, 1]


def test_relay_can_be_skipped_for_ephemeral_databases():
    result = _ingest(_doc(_vuln("CVE-2026-1111")), relay=False)
    assert result["relayed"] is False and not _relayed_alerts().exists()
    assert _finding() is not None, "the finding is still raised — only the push is skipped"


def test_leadership_can_silence_the_relay_without_disabling_the_scan():
    config.set("notifications", {"events": {"admin_audit.integration_health": {"enabled": False}}})

    result = _ingest(_doc(_vuln("CVE-2026-1111")))

    assert result["relayed"] is False and not _relayed_alerts().exists()
    assert result["status"] == "vulnerable" and _finding() is not None
    assert _stored()["vuln_count"] == 1


def test_a_relay_exception_cannot_lose_the_scan_result(monkeypatch):
    """A notification fault is not allowed to cost us the finding."""
    from apps.admin_audit import health_alert

    def boom():
        raise RuntimeError("pingboard exploded")

    monkeypatch.setattr(health_alert, "scan_integration_health", boom)

    result = _ingest(_doc(_vuln("CVE-2026-1111")))

    assert result["status"] == "vulnerable" and result["relayed"] is False
    assert _finding() is not None
    assert _stored()["vuln_count"] == 1


# --- 5. the management command: the pipe, and the gate -----------------------
def test_the_command_reads_the_document_from_stdin(capsys):
    call_command("ingest_image_scan", "--trigger", "scheduled", "--exit-zero",
                 stdin=io.StringIO(_doc()))
    assert "Finding: clean" in capsys.readouterr().out
    assert _stored()["status"] == "ok"


def test_the_command_accepts_a_dash_for_stdin_explicitly(capsys):
    call_command("ingest_image_scan", "-", "--exit-zero", stdin=io.StringIO(_doc()))
    assert "Image scan clean" in capsys.readouterr().out


def test_fixable_findings_fail_the_gate(capsys):
    with pytest.raises(SystemExit) as exit_info:
        call_command("ingest_image_scan", stdin=io.StringIO(_doc(_vuln("CVE-2026-1111"))))
    assert exit_info.value.code == 1
    assert "CVE-2026-1111" in capsys.readouterr().out
    assert _finding() is not None


def test_only_unfixable_findings_do_not_fail_the_gate(capsys):
    """An advisory upstream has not fixed cannot be actioned by bumping anything. A gate
    that fails every single day gets switched off — and then nothing is covered."""
    call_command("ingest_image_scan", stdin=io.StringIO(_doc(_vuln("CVE-2026-3333", fix=()))))

    out = capsys.readouterr().out
    assert "no fix released yet" in out
    rec = _finding()
    assert rec is not None, "unfixable findings are still recorded and still raised"
    assert rec.severity == 60, "a watch item must not outrank actionable work"


def test_a_rejected_document_fails_the_gate_harder_than_findings():
    """"We could not tell" outranks "we found something": it is the state that otherwise
    gets silently filed as clean."""
    with pytest.raises(SystemExit) as exit_info:
        call_command("ingest_image_scan", stdin=io.StringIO("not json"))
    assert exit_info.value.code == 2


def test_exit_zero_reports_without_failing(capsys):
    call_command("ingest_image_scan", "--exit-zero", "--trigger", "deploy",
                 stdin=io.StringIO(_doc(_vuln("CVE-2026-1111"))))  # no SystemExit
    assert "Finding: opened" in capsys.readouterr().out
    assert _stored()["trigger"] == "deploy"


def test_no_relay_flag_skips_the_push(capsys):
    call_command("ingest_image_scan", "--exit-zero", "--no-relay",
                 stdin=io.StringIO(_doc(_vuln("CVE-2026-1111"))))
    assert "relayed to leadership" not in capsys.readouterr().out
    assert not _relayed_alerts().exists()


def test_an_unreadable_path_is_reported_not_crashed(capsys):
    with pytest.raises(SystemExit) as exit_info:
        call_command("ingest_image_scan", "/nonexistent/scan.json")
    assert exit_info.value.code == 2
    assert "not usable" in capsys.readouterr().out


def test_unscannable_targets_are_named_on_the_console(capsys):
    call_command("ingest_image_scan", "--exit-zero",
                 stdin=io.StringIO(_doc(complete=False, unscannable=_UNSCANNABLE)))
    out = capsys.readouterr().out
    assert "NOT SCANNED" in out and "nginx" in out


# --- 6. the stored result is self-describing about staleness -----------------
def test_a_fresh_clean_scan_reads_as_clean():
    _ingest(_doc())
    fresh = image_scan.scan_freshness()
    assert fresh["status"] == "ok" and fresh["effective_status"] == "ok"
    assert fresh["stale"] is False and fresh["age_seconds"] < 60
    assert fresh["complete"] is True and fresh["scanned_count"] == 1


def test_an_eight_day_old_clean_scan_reads_as_unknown():
    """The bug this exists to prevent: rendering a week-old row as if it were news."""
    _ingest(_doc())
    row = AppSetting.objects.get(key=image_scan.IMAGE_SCAN_SETTING_KEY)
    row.value = {**row.value, "as_of": (timezone.now() - timedelta(days=8)).isoformat()}
    row.save(update_fields=["value"])

    fresh = image_scan.scan_freshness()
    assert fresh["status"] == "ok", "the scan's own verdict is preserved"
    assert fresh["stale"] is True and fresh["effective_status"] == "unknown"


def test_a_single_missed_run_is_not_immediately_stale():
    """One skipped run (host reboot, trivy DB fetch failure) must not flip the surface to
    unknown — otherwise the staleness signal itself becomes noise."""
    _ingest(_doc())
    row = AppSetting.objects.get(key=image_scan.IMAGE_SCAN_SETTING_KEY)
    row.value = {**row.value, "as_of": (timezone.now() - timedelta(hours=30)).isoformat()}
    row.save(update_fields=["value"])

    assert image_scan.scan_freshness()["stale"] is False


def test_never_scanned_is_unknown_not_healthy():
    fresh = image_scan.scan_freshness()
    assert fresh["status"] == "never" and fresh["effective_status"] == "unknown"
    assert fresh["stale"] is True and fresh["as_of"] is None


def test_an_incomplete_run_reads_as_unknown_even_though_it_found_nothing():
    _ingest(_doc(complete=False, unscannable=_UNSCANNABLE))
    fresh = image_scan.scan_freshness()
    assert fresh["effective_status"] == "unknown" and fresh["complete"] is False
    assert fresh["unscannable"], "the surface must be able to name what it could not read"


def test_the_row_carries_its_own_staleness_contract():
    """A consumer reading an old row judges it against the cadence that wrote it."""
    stored = _ingest(_doc())
    assert stored["max_age_hours"] == image_scan.FRESHNESS_MAX_AGE_HOURS
    assert stored["stale_after"] > stored["as_of"]


# --- 7. the ops page shows both scanners -------------------------------------
def test_the_health_page_surfaces_both_scanners():
    from apps.admin_audit.health import integration_health

    payload = integration_health(use_cache=False)
    rows = payload["vulnerability_by_key"]
    assert set(rows) == {"dependency_audit", "image_scan"}
    # Nothing has run: both must read as unknown, never as healthy-by-default.
    assert rows["image_scan"]["effective_status"] == "unknown"
    assert rows["dependency_audit"]["effective_status"] == "unknown"

    _ingest(_doc())
    rows = integration_health(use_cache=False)["vulnerability_by_key"]
    assert rows["image_scan"]["effective_status"] == "ok"


def test_a_finding_does_not_flip_the_integrations_chip():
    """``ok`` means "is data flowing". A CVE is a queue of work, not a broken sync, and
    conflating them makes the dashboard chip permanently amber for a reason the page it
    links to calls healthy."""
    from apps.admin_audit.health import integration_health

    before = integration_health(use_cache=False)["ok"]
    _ingest(_doc(_vuln("CVE-2026-1111")))
    assert integration_health(use_cache=False)["ok"] == before


# --- 8. storage caps ----------------------------------------------------------
def test_a_pathological_document_cannot_bloat_the_stored_row():
    """The counts stay exact; only the itemised list is clipped."""
    many = [_vuln(f"CVE-2026-{i:05d}", package="p" * 500) for i in range(500)]
    result = _ingest(_doc(*many))

    assert result["vuln_count"] == 500, "the count is the truth and is not clipped"
    assert len(result["vulns"]) == 100
    assert result["truncated"] is True
    assert all(len(v["package"]) <= 128 for v in result["vulns"])
    assert len(json.dumps(_stored())) < 200_000
