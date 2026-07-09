# Workflows

Recommended routines for running the corporation day-to-day with [FORCA] Command Grid.
These are **recommended** practices, not enforced by the app — adapt them to your corp's
own rhythm and size.

## Table of contents

- [Daily](#daily)
- [Weekly](#weekly)
- [Monthly](#monthly)
- [Best practices](#best-practices)

## Daily

- [ ] Check `/ops/health/` for any integration or sync problem before it compounds.
- [ ] Review the officer deck on the Command Center for anything time-sensitive.
- [ ] Work the recommendations queue (`/recommendations/`) — action or dismiss anything
      that's gone stale.
- [ ] Clear any pending SRP claims (`/srp/queue/`) so pilots aren't left waiting on a
      decision.
- [ ] Glance at open fleet ops (`/operations/`) for under-signed slots that need a nudge.
- [ ] Check the recruitment desk (`/recruitment/`) if it's actively in use.

## Weekly

- [ ] Read the readiness platform's weekly executive report (`/readiness/report/`) and
      act on any high-priority finding.
- [ ] Review Command Intelligence's weekly scheduled report, if enabled, and any open
      Courses of Action.
- [ ] Reconcile the mining ledger and process any pending mining payout split.
- [ ] Review buyback and corp-store fulfilment boards for anything overdue.
- [ ] Check the doctrine coverage dashboard (`/doctrines/coverage/`) against upcoming
      operations, and generate supply tasks for any gap.
- [ ] Spot-check the audit log (`/ops/audit/`) for anything unexpected.

## Monthly

- [ ] Review the Hall of Fame once the month freezes, and confirm contribution weights
      still reflect what the corp values.
- [ ] Review the combat rank ladder and any pending rank-up rewards.
- [ ] Review mentorship pairings for stale or completed programmes, and pay any earned
      rewards.
- [ ] Revisit feature audiences (`/ops/admin/features/`) — confirm they still match who
      you actually want using each service.
- [ ] Review retention policy and, once you trust the member-leave reports it's been
      producing, consider arming it.
- [ ] Review the officer, `recruiter`, and `fc` role assignments for anyone who has moved
      on or changed responsibilities.

## Best practices

- **Prefer audience widening to ad-hoc access.** If allies need a service, register them
  as a partner alliance or friendly corporation and use the `alliance` audience rather than
  making individual exceptions.
- **Let separation of duties do its job.** Don't ask an SRP manager to approve their own
  claim, or a director to self-approve their own Director grant — the app already prevents
  both; treat that as a feature, not friction.
- **Arm reward and payout engines deliberately.** Combat rank rewards, mentorship rewards,
  and the guaranteed buyback queue all ship inert or off; turn them on once you've reviewed
  the budget and rules, not by default.
- **Use "sync now" sparingly.** Corp syncs are cheap no-ops until the matching scope is
  granted, but manual syncs still consume ESI budget — let the scheduled jobs do the
  routine work and reserve manual syncs for troubleshooting.
- **Keep the doctrine library current.** Readiness, the Shipyard, industry demand, and
  supply tasking all read from the same doctrine data — an out-of-date doctrine ripples
  into every one of them.
- **Review the audit log after any access change.** It's the fastest way to confirm a
  grant, revoke, or configuration change actually took effect as intended.

---

For the underlying permission model, see
[Permissions and roles](../permissions-and-roles.md). For every configurable setting, see
[Configuration reference](../configuration-reference.md).
