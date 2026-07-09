"""0.13 housekeeping/prune jobs: DoctrineImportBatch TTL + relayed notif/mail retention."""
from __future__ import annotations

import datetime as dt

import pytest
from django.utils import timezone


@pytest.mark.django_db
def test_doctrine_import_batch_prune(django_user_model):
    """Abandoned previews prune after a short TTL; terminal batches after a month;
    active previews and recent history survive."""
    from apps.doctrines.models import DoctrineImportBatch
    from apps.doctrines.tasks import housekeeping

    owner = django_user_model.objects.create(username="dir")
    now = timezone.now()

    def _batch(status, age_days):
        b = DoctrineImportBatch.objects.create(owner=owner, status=status)
        DoctrineImportBatch.objects.filter(pk=b.pk).update(
            created_at=now - dt.timedelta(days=age_days)
        )
        return b

    fresh_preview = _batch(DoctrineImportBatch.Status.PREVIEW, 0)          # keep (active)
    _batch(DoctrineImportBatch.Status.PREVIEW, 5)                          # prune (abandoned)
    recent_committed = _batch(DoctrineImportBatch.Status.COMMITTED, 10)    # keep (recent)
    _batch(DoctrineImportBatch.Status.COMMITTED, 40)                       # prune (old)
    _batch(DoctrineImportBatch.Status.EXPIRED, 40)                         # prune (old)

    result = housekeeping()
    assert result == {"abandoned_previews": 1, "old_terminal": 2}
    surviving = set(DoctrineImportBatch.objects.values_list("pk", flat=True))
    assert surviving == {fresh_preview.pk, recent_committed.pk}


@pytest.mark.django_db
def test_relayed_notification_and_mail_prune():
    """CorpNotification + RelayedMail are pruned past the 90-day retention window."""
    from apps.recommendations.models import CorpNotification, RelayedMail
    from apps.recommendations.tasks import housekeeping

    now = timezone.now()
    CorpNotification.objects.create(notification_id=1, type="StructureUnderAttack",
                                    timestamp=now - dt.timedelta(days=5))     # keep
    CorpNotification.objects.create(notification_id=2, type="WarDeclared",
                                    timestamp=now - dt.timedelta(days=120))   # prune
    RelayedMail.objects.create(mail_id=1, subject="Recent",
                               sent_at=now - dt.timedelta(days=5))            # keep
    RelayedMail.objects.create(mail_id=2, subject="Old",
                               sent_at=now - dt.timedelta(days=200))          # prune

    result = housekeeping()
    assert result == {"notifications": 1, "mail": 1}
    assert set(CorpNotification.objects.values_list("notification_id", flat=True)) == {1}
    assert set(RelayedMail.objects.values_list("mail_id", flat=True)) == {1}
