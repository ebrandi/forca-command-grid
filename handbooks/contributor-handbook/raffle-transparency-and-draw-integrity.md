# Raffle transparency and draw integrity

How a raffle ticket comes into existence, acquires a permanent number, enters a frozen
pool, and either wins or doesn't — and how any pilot can check all of that for themselves.

The design goal is narrow and worth stating plainly: **a pilot should be able to answer
"was this fair?" without trusting leadership, and without asking anyone.** Everything below
follows from that.

---

## 1. The ticket lifecycle

```
activity happens                 apps/raffle/sources/*.py
      ↓  iter_events()
eligibility + caps + boosters    apps/raffle/engine.py
      ↓  award or record-as-ineligible
append-only ledger row           RaffleTicketLedgerEntry
      ↓  assign_ticket_numbers()
permanent ticket numbers         apps/raffle/tickets.py
      ↓  contest closes
frozen, hashed pool              RaffleTicketPoolSnapshot   ← fingerprint published
      ↓  prepare_draw()
committed secret seed            RaffleDraw.seed_commitment ← commitment published
      ↓  execute_draw()
winners + rolls + receipt        RaffleDrawResult
      ↓  set_fulfilment()
prize delivered
```

Two invariants run through all of it and must never be softened silently:

* **Only enrolled pilots with a valid ESI token earn tickets or win prizes.** Activity from
  an unenrolled pilot becomes a `RaffleIneligibleActivity` row (analytics + outreach),
  never a drawable ticket.
* **The ledger is append-only.** Corrections are new rows or status changes with a recorded
  reason — never destructive edits.

---

## 2. Ticket identity

### A ticket is not a row

Materialising one row per ticket would turn a single 100-ticket solo kill into 100 inserts
and a busy contest into millions of rows, for no gain — the award event already records
everything true about those tickets. Instead each award owns a contiguous half-open range:

```
tickets(entry) == [entry.ticket_start, entry.ticket_start + entry.amount)
```

`ticket_start` is allocated once by `apps.raffle.tickets.assign_ticket_numbers()` from the
contest's monotonic `next_ticket_number` counter, under a row lock on the contest.

### Why numbers are trustworthy

**Append-only.** Numbers are handed out in ledger-`id` order. `id` is monotonic and
immutable, so a newly-swept award always lands *after* every existing one. A pilot shown
`#412–#511` yesterday owns exactly those tickets tomorrow, no matter what anyone else earns
in between.

> Ordering deliberately uses `id`, **not** `occurred_at`. A retroactive award can be
> back-dated; ordering by activity time would insert it *between* existing tickets and
> renumber everything after it.

**Never reused.** A reversed or disqualified award keeps its numbers; its tickets simply
stop being drawable. The gap is the visible evidence that a correction happened. Reclaiming
the range would silently renumber everything after it — the one thing that would make a
pilot's screenshot a lie.

Because invalidated tickets leave gaps, **ticket-number space is not contiguous**. The draw
therefore runs over a second, contiguous *draw space* built at freeze time, and the snapshot
records the mapping between the two coordinate systems.

### How the ticket count was reached

`engine._final_tickets()` returns the multiplier and the pre-boost base alongside the final
count, and `process_source` records both in the award's `metadata` whenever they matter:

| Key | Written when |
|---|---|
| `booster_multiplier`, `base_tickets` | a booster window was in force |
| `capped`, `cap_scope`, `cap_amount` | a daily/weekly/contest cap trimmed the award |

The pilot's ticket ledger renders these as chips. A kill that paid 200 tickets during a
double-ticket weekend says so; a 100-ticket solo trimmed by a daily cap says that too.
Without this the arithmetic looks arbitrary, which is the same trust problem as an
unexplained winner.

### Where numbers are assigned

| Write path | Call site |
|---|---|
| Automatic sweep | `engine.process_source()` after `bulk_create` |
| Manual grant | `services.grant_manual_tickets()` before returning |
| Freeze | `snapshot.freeze_pool()` (belt and braces — an unnumbered award cannot be pooled) |

Numbering is idempotent: rows that already carry a `ticket_start` are untouched, so any of
these may run any number of times.

---

## 3. Freezing the pool

`RaffleTicketPoolSnapshot` is created automatically when a contest transitions
`ACTIVE → CLOSED` (`services.set_status`), and its SHA-256 fingerprint is published.

### Why it exists

The seed commitment proves the *seed* was not swapped. It proves nothing about the *pool*.
Before snapshots, the draw read the live ledger at execution time — potentially days after
the seed was generated and stored — so anyone who could read the seed could compute the
outcome in advance and then change it by disqualifying a single ledger entry: every later
ticket range shifts down, a different pilot sits under the winning offset, and the
commitment still verifies perfectly.

Freezing the pool *before* the seed exists closes that hole. With both halves of the input
fixed and public in advance, the outcome can be checked but not chosen.

### What is in it

`entries` is the canonical pool: one compact row per drawable award, ordered by
`ticket_start`:

```
[ledger_entry_id, user_id, ticket_start, amount, draw_start]
```

`draw_start` is the award's offset in the contiguous draw space. Only awards that are
`APPROVED`, positive, numbered and attached to an account are included — an award with no
account can never win, so including it would overstate the pool.

### The fingerprint

`snapshot.canonical_payload()` produces a deliberately boring, line-oriented serialisation
so that a pilot with a text editor and `sha256sum` can reproduce it:

```
forca-raffle-pool-v1
contest=<id>
slug=<slug>
version=<n>
cutoff=<iso8601>
frozen=<iso8601>
algorithm=<version>
rules=<json, sorted keys, no spaces>
tickets=<total>
entries=<count>
--
<ledger_id>,<user_id>,<ticket_start>,<amount>,<draw_start>
...
```

`content_hash = sha256(payload.encode("utf-8"))`.

`SNAPSHOT_FORMAT` is part of the hashed payload, so an old receipt keeps verifying under
its own format if the serialisation ever changes.

### Reopening a frozen pool

A snapshot is never edited. An exceptional correction creates a **new version** and sets
`superseded_by` on the old one. `services._refreeze_after_correction()` also **invalidates
any committed-but-unexecuted draw**: that seed was generated against the old pool, and
letting it run against the new one would mean the seed was known before the pool was final.

Post-freeze ledger changes are refused by default (`services.LedgerFrozen`) and require an
explicit `allow_frozen=True` from the console's audited correction action.

---

## 4. The draw

### Algorithm

1. `prepare_draw()` — ensure a frozen pool, generate a 32-byte CSPRNG seed
   (`secrets.token_hex(32)`), publish `sha256(seed)` and the pool fingerprint.
2. `execute_draw()` — for each prize, roll the hash chain until a valid ticket is hit:

```python
r_i = int(sha256(f"{effective_seed}:{i}").hexdigest(), 16) % snapshot.total_tickets
```

3. `r_i` is bisected onto the snapshot's `draw_start` values to find the owning award, and
   the exact winning ticket number is:

```python
ticket_no = ticket_start + (r_i - draw_start)
```

`effective_seed` is `seed` alone, or `sha256(f"{seed}|{external_entropy}")` when leadership
folded in a public entropy string (a beacon value, a block hash, a number called on comms).

### Eligibility is a skip, never a repack

Draw-time eligibility is re-checked, because the corp rule is "enrolled and valid **at draw
time** to win". But an ineligible pilot's tickets **keep their positions** and are rolled
past, recorded in `skipped_draws`.

This is the important property: **excluding a pilot after the freeze can only cost that
pilot their win. It cannot move the win to a chosen someone else, because nobody else's
position changes.** The same mechanism handles `one_prize_per_pilot`.

### Idempotency and concurrency

* A cross-worker Redis lock (`raffle:draw:lock:<pk>`) in `services.run_draw()`.
* A `select_for_update` + status compare-and-set in `execute_draw()` — a retried beat or a
  double-click returns the existing row untouched.
* `run_draw()` returns any existing current completed draw rather than drawing again.

### The extreme-skew fallback

If a prize's roll budget (`2 × total + 10000`) is exhausted — only possible with pathological
ticket skew — the draw falls back to a uniform-by-ticket roll among still-drawable awards.
This is recorded in `skipped_draws` and, because it uses a different modulus, is **not**
part of the main chain. `verify_draw()` reports `replay_partial` for such a draw rather than
claiming a mismatch.

---

## 5. Verification

`draw.verify_draw()` checks four things against the **persisted** records:

| Key | Meaning |
|---|---|
| `commitment_ok` | The revealed seed hashes to the commitment published first |
| `snapshot_ok` | The frozen pool still serialises to its published fingerprint |
| `values_ok` | Every stored roll is the next link in the hash chain |
| `tickets_ok` | Every stored roll maps onto the ticket the record claims |
| `winners_match` | Replaying the chain reproduces exactly the `RaffleDrawResult` rows on file |

> **The bug this replaced:** the previous implementation built `recomputed` and `recorded`
> *both* from `draw.random_values` and compared them — it checked one array against itself
> and never read the results table at all. Editing a winner row in the database left the page
> reporting "Draw verified ✓". `winners_match` now replays from the seed and compares against
> `draw.results`, so a tampered winner is detected. Pinned by
> `tests/test_raffle_transparency.py::test_verify_draw_detects_a_tampered_winner`.

`snapshot.verify_snapshot()` additionally reports `ledger_matches` — whether the live ledger
still agrees with the frozen pool. A mismatch is *not* necessarily wrongdoing (an audited
post-draw correction moves the ledger on) but it is surfaced, never hidden.

### Doing it by hand

1. Download the receipt: `GET /raffle/<slug>/draw/receipt.json`
2. Rebuild the canonical pool payload from `snapshot.entries`, hash it, compare to
   `snapshot.content_hash`.
3. Confirm `sha256(draw.seed) == draw.seed_commitment`.
4. For each roll, compute `sha256(f"{seed}:{index}")` as an integer mod `total_tickets`.
5. Bisect onto `draw_start` — that award's owner is the winner and
   `ticket_start + (offset - draw_start)` is the winning ticket number.

---

## 6. Draw receipts

`/raffle/<slug>/draw/` renders the pilot-facing receipt;
`/raffle/<slug>/draw/receipt.json` is the machine-readable form
(`format: "forca-raffle-receipt-v1"`).

Contents: contest and rules, draw timestamp, algorithm and code version, seed commitment,
revealed seed, external entropy, snapshot version/fingerprint/cutoff, ticket and pilot
counts, every winner with prize, **winning ticket number**, draw order, status, replacement
linkage and prize delivery status, plus the full roll and skip log and the verification
report.

Never in the receipt: ESI tokens, `internal_notes`, `admin_notes`, fulfilment notes, or any
other leadership-only field.

---

## 6b. Rank prizes — earned, not drawn

A contest can carry a second kind of prize: `RaffleRankPrize`, awarded for finishing at a
given place on a killboard board over the contest window (`top killer`, `top solo`, `most
active`). Pilots watch the standings all contest and know before the draw whether they are
winning one.

### Why a separate relation

`RaffleRankPrize` is deliberately **not** in `contest.prizes`. Fifteen call sites read that
relation — the draw loop, the snapshot rules, the odds calculation, the readiness checks,
the budget guard, the console and the pilot page — and each defaults to "whatever is in the
table". A rank prize in that ladder would be drawn by ticket *and* would burn its winner's
one-prize-per-pilot allowance. Missing a filter there fails **open** and corrupts the draw;
with a separate relation the worst a missed reader does is under-count a budget (and
`services.contest_prize_total` / `monthly_prize_spend` sum both, so even that is covered).

`RaffleRankAward` is likewise a sibling of `RaffleDrawResult`, not a row in it:
`uniq_raffle_result_per_prize` is conditioned on `(draw, prize)` and Postgres treats NULLs
as distinct, so a nullable `draw` would silently void "one live winner per prize"; and
`services.dashboard_summary` dereferences `result.draw.contest` with no draw filter.

### The stacking rule

| | Limit |
|---|---|
| Rank prize + rank prize (different boards) | **stacks** — two achievements, two payouts |
| Rank prize + ticket prize | **stacks** |
| Ticket prize + ticket prize | **one per pilot** (`one_prize_per_pilot`) |

`award_rank_prizes` never touches the draw's `won_users` set, which is what keeps this true.
Earned prizes stack because they are performance; the drawn one is limited because it is luck.

### Standings

`apps.raffle.rankings` builds the boards over `[contest.start_at, contest.end_at)` using
`leaderboards.Window` directly (`leaderboards()` itself clamps unknown keys to 30d).
Differences from the killboard's own boards, all deliberate:

* **Only contest-eligible accounts are ranked.** A pilot who never enrolled or whose token
  lapsed cannot win, so they are not on the board and the places are computed among the
  pilots actually competing. The raffle's "top killer" can therefore differ from the
  killboard's — correct, because the raffle only knows pilots who connected a token.
* **Rollup is by ACCOUNT, not by `mains_for`.** `core.pilots.mains_for` maps a linked
  character whose account has no `is_main` flagged to *itself*, so rolling up by main leaves
  such an account as two rows — one person holding two places and collecting the same prize
  twice. `_rollup_by_account` keys on `user_id`.
* **Ties break on the lower character id.** `leaderboards._rank` sorts with a stable sort
  over a GROUP BY carrying no ORDER BY, so tied pilots can swap places between runs.
  Harmless on a display board, unacceptable when the place decides a payout.
* **Ranked deeper than displayed.** `RANK_DEPTH = 200` vs `BOARD_LIMIT = 10`, so a pilot in
  14th can still be told they are 14th and what 13th costs.

`AWARDABLE_BOARDS` excludes two of the killboard's eight on purpose: `isk_lost` ("bravest
feeder") would put a bounty on feeding, and `efficiency` has a five-fight minimum, so a
pilot on 5 kills and 0 losses maximises their prize by never undocking again.

**Performance.** A contest board's cost is *independent of window width* — `_kill_rows`
starts from `KillmailParticipant`, whose index carries no time column, so a 7-day window
scans like an all-time one. Every contest is also its own cache key, so the killboard's
warmed windows buy nothing. `tasks.refresh_adoption` warms standings for contests that have
a rank prize; the request path is a cold-start fallback only.

### Gating

Rank awards run inside `run_draw`, past the same minimum-activity gate and inside the same
cross-worker lock. A separate entry point would be a way to pay out on a dead contest
without the safeguard leadership configured. Awarding is idempotent
(`uniq_raffle_rank_award_live` is the backstop), and each award freezes the board that
decided it, because the killboard keeps moving afterwards.

## 6c. Winner count and selective boosting

**Winner count.** There is no `winner_count` field: the ticket prize rows *are* the winner
count (`draw.py` awards one winner per prize row in rank order).
`services.set_ticket_prize_slots(contest, n)` materialises or trims rows to match, capped at
`MAX_TICKET_WINNERS = 10` and refused once accrual has started. A slot that has been won is
never removed — `RaffleDrawResult.prize` cascades, so deleting it would erase a published
winner and their fulfilment history.

**Selective booster.** `RaffleContest.prize_booster_applies_to` is `both` (the pre-existing
behaviour), `ticket`, or `rank`. It is contest-level rather than per-prize because the
booster is already one contest-wide goal at one percentage; a per-prize opt-in would be a
second way to express the same thing and would let a leader build a ladder nobody can
explain ("why was 2nd boosted and 3rd not?"). `boosters.is_boostable(prize, contest, kind=…)`
is the single predicate — **use it, never an inline `prize_type in BOOSTABLE_PRIZE_TYPES`
check**, or the pilot page will promise a boost the draw pays at base value.

## 7. Redraws and exceptional cases

Nothing is ever erased. Two distinct mechanisms:

**Forfeit + replacement** (`services.forfeit_result`) — one winner is no longer eligible,
left the corp, or declined. The result is marked `FORFEITED` with a public reason, and the
replacement **continues the same hash chain over the same frozen pool**, so it verifies
under exactly the same maths. `replaces` / `replaced_by` link the two in both directions.

**Redraw** (`services.redraw`) — the whole draw is discarded. A reason is mandatory. The old
draw gets `superseded_by`, its winners become `REDRAWN` with the reason and authoriser, and
every superseded draw is disclosed publicly on the receipt so a "redraw-until-win" cannot
hide behind the fairness proof of the final one.

The `uniq_raffle_result_per_prize` constraint is conditioned on `status = 'won'`, so a
forfeited result stays in the table beside its replacement while a prize can still only have
one live winner.

---

## 8. Visibility rules

| Data | Anonymous | Corp pilot | Member | Officer | Director |
|---|---|---|---|---|---|
| Contest, prizes, rules, ticket-earning rules | ✅¹ | ✅ | ✅ | ✅ | ✅ |
| Pool totals, fingerprint, seed, commitment | ✅¹ | ✅ | ✅ | ✅ | ✅ |
| Winners + winning ticket numbers | ✅¹ | ✅ | ✅ | ✅ | ✅ |
| **Per-pilot pool roster** (`/pool/`, receipt `entries`) | ❌ | ❌ | ✅ | ✅ | ✅ |
| **Own** ticket numbers + evidence (`/tickets/`) | ❌ | own only | own only | own only | own only |
| Manual-grant reason + granting officer | — | own only | own only | ✅ | ✅ |
| Grant `internal_notes`, `admin_notes`, fulfilment notes | ❌ | ❌ | ❌ | ✅ | ✅ |
| Ledger of all pilots, ineligible report, integrity flags | ❌ | ❌ | ❌ | ✅ | ✅ |
| Execute draw, redraw, forfeit, emergency override | ❌ | ❌ | ❌ | ❌ | ✅ |

¹ Only when leadership sets the `raffle` feature audience to `public`; it defaults to
`corp`, in which case anonymous visitors are redirected to log in by `FeatureGateMiddleware`.

The roster line is the deliberate one: **the proof is public, the roster is corp business.**
Hashes, totals, the seed and the winning ticket numbers are published to whoever can see the
contest — enough to check that the published result matches the recorded draw. The list of
which pilot holds which ticket numbers requires the member role, so the corp roster is never
put on the open internet.

All gates are enforced in the view (`_is_member`, `_is_officer`, `@role_required`), never by
hiding a button.

---

## 9. Manual grants and adjustments

`services.grant_manual_tickets()` requires: officer role, a positive amount, a **mandatory
reason**, an unfrozen ledger, and an eligible pilot. Separation of duties applies — an
officer may not grant to their own account (Director/superuser exempt as break-glass).

Granting to a non-enrolled pilot is refused unless *all* of: `override=True`, the raffle
config's `allow_manual_override` is on, and the actor is a Director. It is then loudly
audited as `override_used`.

There is no ticket-balance editing anywhere. Corrections are `reverse_entry()` (appends a
reversal row) or `set_entry_status()` (excluded/disqualified) — both audited, both refused on
a frozen pool without the explicit correction flag.

The owning pilot sees the reason and the granting officer's **display name** (never the
opaque `eve:<id>` username) on their own ticket ledger. `internal_notes` stay leadership-only.

---

## 10. Pre-draw validation

`apps.raffle.readiness.draw_checklist()` returns checks in three states:

* **critical** — the draw is blocked with no override. Running anyway would produce a result
  that cannot be defended afterwards.
* **warning** — allowed, but the Director must tick an acknowledgement, and the
  acknowledgement is written to the audit log (`raffle.draw.warnings_acknowledged`).
* **ok**.

Critical: no prizes · cutoff not passed · contest not closed · unnumbered awards · no frozen
pool · pool fingerprint mismatch · empty pool.
Warning: non-sequential prize ranks · draw scheduled before cutoff · sources not swept past
the cutoff · awards pending approval · accountless approved awards · open integrity flags ·
ledger drifted from the frozen pool · fewer eligible pilots than prizes.

The checklist is strictly read-only — a validator that quietly repairs what it validates is
how a broken pool gets drawn against and nobody notices.

---

## 11. Historical data

Migration `0009_backfill_ticket_numbers` assigns ticket numbers to every pre-existing award
in ledger-`id` order and flags those contests `ticket_numbers_backfilled`.

This is honest: award order is recorded, so replaying it reproduces the sequence in which
those tickets were earned. The numbers are a label over data we already hold.

**What it deliberately does not do is touch any draw or winner.** A contest drawn before
this work was drawn without ticket numbers existing at all, and the old code recorded the
winner's *first* ticket rather than the one the chain rolled. Inventing a number for those
results would be fabricating evidence. They keep `winning_ticket_no = NULL`, `verify_draw()`
returns `{"verifiable": False, "legacy": True}`, and the receipt says so in words rather
than showing a verification that never happened.

The migration is reversible (`unbackfill`), which is safe because no snapshot can exist
until the code that creates them is running.

---

## 12. Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| Ticket range shows `—` | Award not yet numbered | `assign_ticket_numbers(contest)`; check the sweep ran |
| Checklist: "Every ticket has a number" critical | Rows written by a path that skips numbering | Run numbering; find the write path that bypassed it |
| Checklist: pool fingerprint mismatch | Snapshot row edited after freezing | **Do not draw.** Re-freeze with a recorded reason; investigate |
| Pool page: "ledger has changed since frozen" | Audited post-freeze correction | Expected after a correction — confirm it matches an audit entry |
| Receipt: "Partly verified" | Extreme-skew fallback roll was used | Expected; the fallback is recorded in `skipped_draws` |
| Receipt: "Verification failed" on `winners_match` | Results edited outside the app | Investigate immediately — this is the tamper signal |
| Committed draw became `FAILED` | Pool re-frozen after the seed was committed | Commit a fresh seed against the new pool |
| `LedgerFrozen` on a correction | Pool already frozen | Use the console's audited correction (`allow_frozen`) |

Useful queries:

```python
from apps.raffle import snapshot, tickets, readiness
snapshot.verify_snapshot(snapshot.current_snapshot(contest))
tickets.entry_owning_ticket(contest, 412)
readiness.draw_checklist(contest)
```

---

## 13. Security considerations

* **Server-authoritative.** The result is computed and persisted by `execute_draw()` before
  anything is rendered. There is no client-side draw animation and no endpoint that lets a
  browser influence a result. Reloading shows the same result; every viewer sees the same
  result; nothing re-draws on replay.
* **Ordering is the security property.** Pool frozen → fingerprint published → seed
  generated → commitment published → draw. Any reordering reopens the manipulation hole, so
  `_refreeze_after_correction()` invalidates committed seeds.
* **CSPRNG only** (`secrets`), never `random`.
* **Database-enforced invariants**: `uniq_raffle_ticket_event` (idempotent sources),
  `uniq_raffle_ticket_start` (no two awards share a number), `uniq_raffle_result_per_prize`
  conditioned on `won` (one live winner per prize), `uniq_raffle_snapshot_version`.
* **No secrets in the receipt.** The draw seed is a per-draw random value with no other
  purpose — revealing it is the mechanism, not a leak. Application secrets, signing keys and
  ESI tokens never appear.
* **Input clamping.** Ledger filters are clamped to known enums server-side rather than
  echoed — see `security-guidelines.md` on Alpine attribute interpolation.

---

## 14. Test coverage

`tests/test_raffle_transparency.py` (41 tests). The two that this work exists for:

* `test_winning_ticket_is_the_ticket_that_was_actually_drawn` — recomputes the roll
  independently and asserts the recorded ticket is the drawn one.
* `test_verify_draw_detects_a_tampered_winner` — edits a winner row and asserts verification
  fails.

Also covered: numbering (contiguity, append-only under concurrent earning, gaps after
reversal, idempotency, ownership lookup); booster/cap provenance on the award; snapshots
(freeze on close, hash tamper detection, exclusion of unapproved/accountless awards,
supersession); the frozen-ledger guard and audited correction path; draw behaviour (frozen
pool not live ledger, skip-not-repack, idempotency, empty pool, legacy honesty);
forfeit/replacement and redraw; authorisation and the roster boundary; the readiness
checklist and its console enforcement; and scale (200 pilots / 50k+ tickets, plus an
equality assertion that ledger page cost does not grow with history size).

Existing suites — `test_raffle_draw.py`, `test_raffle_lifecycle.py`, `test_raffle_manual.py`,
`test_raffle_admin.py`, `test_raffle_eligibility.py` and the rest — continue to pass
unchanged.
