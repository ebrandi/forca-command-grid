"""Give every existing ticket award its permanent number.

Numbering is a *label over data we already hold* — the award order — not an invention:
ledger ids are monotonic, so replaying them in id order reproduces exactly the sequence in
which those tickets were earned. That makes the backfill honest.

What it deliberately does NOT do is touch any draw or winner. A contest drawn before this
migration was drawn without ticket numbers existing at all, so its winning ticket number
was never recorded and cannot be reconstructed: the old code stored the winner's *first*
ticket rather than the one the hash chain actually rolled, and the rolled offset was
mapped onto per-pilot ranges that were rebuilt at draw time. Inventing a number for those
results would be fabricating evidence. They keep ``winning_ticket_no = NULL`` and the UI
reports them as pre-transparency draws.

Contests touched here are flagged ``ticket_numbers_backfilled`` so leadership can see, per
contest, that the numbers were assigned retroactively rather than at award time.
"""
from __future__ import annotations

from django.db import migrations


def backfill(apps, schema_editor):
    RaffleContest = apps.get_model("raffle", "RaffleContest")
    RaffleTicketLedgerEntry = apps.get_model("raffle", "RaffleTicketLedgerEntry")

    for contest in RaffleContest.objects.all().iterator():
        next_no = 1
        touched = []
        # Only positive awards own tickets; a reversal row is a correction, not a ticket.
        rows = (
            RaffleTicketLedgerEntry.objects
            .filter(contest_id=contest.pk, amount__gt=0, ticket_start__isnull=True)
            .order_by("id").only("id", "amount")
        )
        for row in rows.iterator(chunk_size=1000):
            row.ticket_start = next_no
            next_no += row.amount
            touched.append(row)
            if len(touched) >= 1000:
                RaffleTicketLedgerEntry.objects.bulk_update(touched, ["ticket_start"])
                touched = []
        if touched:
            RaffleTicketLedgerEntry.objects.bulk_update(touched, ["ticket_start"])

        if next_no > 1:
            contest.next_ticket_number = next_no
            contest.ticket_numbers_backfilled = True
            contest.save(update_fields=["next_ticket_number", "ticket_numbers_backfilled"])


def unbackfill(apps, schema_editor):
    """Reverse cleanly so the migration can be rolled back with the release.

    Safe because nothing downstream has been written yet at this point in the upgrade:
    snapshots are created from ticket numbers, and no snapshot can exist until the code
    that creates them is running.
    """
    RaffleContest = apps.get_model("raffle", "RaffleContest")
    RaffleTicketLedgerEntry = apps.get_model("raffle", "RaffleTicketLedgerEntry")

    RaffleTicketLedgerEntry.objects.filter(ticket_start__isnull=False).update(ticket_start=None)
    RaffleContest.objects.filter(ticket_numbers_backfilled=True).update(
        next_ticket_number=1, ticket_numbers_backfilled=False
    )


class Migration(migrations.Migration):

    dependencies = [
        ("raffle", "0008_raffleticketpoolsnapshot_and_more"),
    ]

    operations = [
        migrations.RunPython(backfill, unbackfill),
    ]
