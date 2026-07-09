"""REC-2 (roadmap 2.13) — recommendation engine tuning console.

Acceptance: directors can enable/disable evaluators and tune the combat-loss
window/threshold + severity floor without a deploy; the engine honours the config.
"""
from __future__ import annotations

import pytest
from django.urls import reverse

from apps.recommendations.engine import EVALUATOR_REGISTRY, run_all
from apps.recommendations.models import Recommendation, RecommendationConfig
from core import rbac
from tests._raffle_utils import enrol_pilot

pytestmark = pytest.mark.django_db


def _url():
    return reverse("admin_audit:recommendations_tuning")


def _director(client, dum, cid):
    user, _ = enrol_pilot(dum, cid, roles=(rbac.ROLE_DIRECTOR,))
    client.force_login(user)


def test_disabling_all_evaluators_produces_no_drafts():
    cfg = RecommendationConfig.active()
    cfg.disabled_evaluators = [k for k, _label, _f in EVALUATOR_REGISTRY]
    cfg.save()
    assert run_all() == 0
    assert Recommendation.objects.count() == 0


def test_console_saves_evaluator_toggles_and_thresholds(client, django_user_model):
    _director(client, django_user_model, 6000)
    assert client.get(_url()).status_code == 200
    # only stock_shortage stays enabled; the rest are muted
    client.post(_url(), {
        "evaluator": ["stock_shortage"],
        "combat_loss_window_days": "14", "combat_loss_threshold": "5", "min_severity": "20",
    })
    cfg = RecommendationConfig.active()
    assert "stock_shortage" not in cfg.disabled_evaluators
    assert "combat_loss" in cfg.disabled_evaluators
    assert (cfg.combat_loss_window_days, cfg.combat_loss_threshold, cfg.min_severity) == (14, 5, 20)


def test_console_clamps_out_of_range_values(client, django_user_model):
    _director(client, django_user_model, 6001)
    client.post(_url(), {
        "evaluator": ["stock_shortage"],
        "combat_loss_window_days": "0", "combat_loss_threshold": "3", "min_severity": "500",
    })
    cfg = RecommendationConfig.active()
    assert cfg.combat_loss_window_days == 1  # clamped up from 0
    assert cfg.min_severity == 100          # clamped down from 500


def test_forbidden_below_director(client, django_user_model):
    user, _ = enrol_pilot(django_user_model, 6002, roles=(rbac.ROLE_OFFICER,))
    client.force_login(user)
    assert client.get(_url()).status_code in (302, 403, 404)
