# Combat and Progression

## Table of contents

- [The killboard](#the-killboard)
- [Rankings, stats, roster, and pilot pages](#rankings-stats-roster-and-pilot-pages)
- [Combat ranks and rewards](#combat-ranks-and-rewards)
- [Hall of Fame](#hall-of-fame)
- [Combat Signatures](#combat-signatures)
- [Newbro milestones](#newbro-milestones)

## The killboard

The **killboard** (`/killboard/`) is the corp's zKillboard-style combat record. It's public
— anyone can browse it, filter it, and open an individual killmail (`/killboard/<killmail
id>/`) to see the full breakdown: fit, damage done, final blow, and value. Every kill and
loss involving the corporation flows into it automatically, and it's the backbone that
combat ranks, the Discord kill feed, and battle reports are all built from.

## Rankings, stats, roster, and pilot pages

- **Rankings** (`/killboard/rankings/`) — the leaderboard, public to everyone.
- **Stats** (`/killboard/stats/`) — corp-wide combat statistics; visible to members and,
  where leadership allows, allies.
- **Roster** (`/killboard/roster/`) — a combat-focused view of the membership.
- **Your pilot page** (`/killboard/pilot/<character>/`) — per-pilot analytics: your kills,
  losses, ships flown, and trends over time. Anyone can open any pilot's page; it's the
  same page whether it's you or a corpmate.
- **Compare** (`/killboard/compare/`) — put two pilots side by side.

## Combat ranks and rewards

The corp runs a configurable **combat rank ladder** — a sequence of named ranks you climb
based on threshold combat metrics. Your current rank and progress toward the next one show
on your Command Center dashboard, and ranking up triggers a one-time celebration. Some
corps also run an optional **reward** track tied to ranks; if yours does, rewards are
never automatic — an officer reviews and approves each one before it's marked paid, and
only activity from the point the ladder went live counts (rewards are never backdated over
pilots who were already past a threshold when it was introduced).

## Hall of Fame

The **Hall of Fame** is a monthly corp recognition leaderboard, built from everyone's
[contribution ledger](./tools-and-services.md#contribution-ledger) — overall, and broken
out by category (combat, industry, logistics, and so on). Once a month is over, its board
is frozen: if leadership later retunes how contributions are weighted, past months don't
retroactively shift underneath you.

## Combat Signatures

A **Combat Signature** is a personalised PNG banner built from your own killboard and
profile data — a compact image you paste into a forum signature, a Discord post, or a
website. You build and manage them privately at **Signatures** (`/killboard/signatures/`,
in the Killboard section of the sidebar); the finished image lives at a stable, public URL
that anyone you share it with can display.

Signatures are available only when leadership has switched the feature on, and only to
current home-corp pilots. If you fly linked alts, you manage each pilot's signatures while
that pilot is your active character — you can only edit a signature owned by the pilot you
are currently flying as.

### Building a signature

The builder is a single form with a live preview. You choose:

- **A name** — a private label for your own list (it is not shown on the image).
- **A size preset** — `compact` (468×120) and `standard` (600×150) suit strict forums,
  `wide` (728×120) fits wide web banners, and `card` (600×200) suits Discord and web
  embeds. Leadership may restrict which presets are offered.
- **A layout** — *Identity* (portrait, name/corp header, a stat grid), *Tactical* (rank
  emblem and progress bar with compact stats), or *Minimal* (a single stat strip).
- **A background** — one design from a fixed, curated library of original abstract art.
  There is no image upload; you pick from the enabled designs.
- **Components** — the pieces of data shown, in the order you tick them, up to twelve.
  These include your portrait, pilot name, corporation and alliance (ticker and logo),
  core combat stats (kills, losses, solo kills, final blows, ISK destroyed and lost, ISK
  efficiency, kill/death ratio), your rank title and progress, featured trophies and your
  trophy count, your last and best kill, favourite ship and top ship class, an
  activity-period label, and an optional "stats as of" timestamp.
- **An activity period** — the window the stats summarise: last 7/30/90 days, this month,
  last month, or all time.
- **A language** — the language the image's labels render in. It defaults to your current
  interface language; pilot names and localised labels (including Chinese, Japanese, and
  Korean) are drawn with the correct glyphs.
- **A theme** — Gold, Cyan, or Green accent colours.
- **Featured trophies** — pick from the trophies you have actually earned, up to the limit
  leadership sets.

Each layout and size has room for only so many components. If you select more than will
fit, the builder tells you which ones "won't be shown" — they stay saved, but the smaller
image simply omits them. Use the **preview** to check the result before saving; note that
the preview shows plain monogram placeholders instead of portraits and logos — the real
portrait appears once the first full render completes a few moments after you save.

### Live and snapshot signatures

A new signature is **live**: it refreshes automatically as your stats change, so an
embedded banner stays current on its own. You can convert a live signature to a
**snapshot**, which freezes both its configuration and its statistics at the moment of
conversion — useful for commemorating a specific month or milestone. A snapshot can only be
renamed afterwards, not reconfigured, and the conversion is one-way: to change a snapshot,
create a new signature instead. Snapshots are available only if leadership has enabled them.

### Embedding your signature

Each signature in your list offers four ready-to-paste snippets — copy whichever your
destination understands:

- **Direct URL** — the raw image link, for anywhere that accepts an image address.
- **BBCode** (`[img]…[/img]`) — for phpBB / XenForo-style forums.
- **Markdown** (`![…](…)`) — for Markdown sites and chat that renders it.
- **HTML** (`<img …>`) — for your own web pages.

### What is public, and what is not

Only the data you deliberately add as components is ever shown; nothing else about your
account is exposed. The public URL contains a long, unguessable token, so the image is not
listed or discoverable anywhere — but it is genuinely public: **anyone who has the URL can
view the image**, without logging in. Treat the link as you would any shareable image link.
The image also asks search engines not to index it, so it will not turn up as its own page
in a search.

### Regenerating, rotating, disabling, and deleting

From your signatures list you can:

- **Regenerate** — queue a fresh render immediately rather than waiting for the next
  scheduled refresh. This is rate-limited; if you trigger it too often you will be asked to
  wait a minute.
- **Rotate the URL** — mint a new public link. **The old URL stops working straight away**,
  so anywhere you had embedded the old link will show a broken image until you paste the new
  one. Use it if a link has been shared somewhere you no longer want it live.
- **Disable** — take the image offline while keeping the signature. Its public URL stops
  serving; re-enabling it renders a fresh image (and counts against your active-signature
  limit again).
- **Delete** — remove the signature and its image permanently.

You can hold up to a set number of **active** signatures at once (leadership configures the
limit); disabled signatures do not count toward it.

### If you leave the corporation

By default, leaving the home corporation **freezes** your signatures: the images stay up as
they were, automatic refresh stops, and you can no longer edit them. If you rejoin, they
unfreeze and resume refreshing on their own. Leadership can instead choose a stricter policy
that removes your images when you leave — check with them if you are unsure which applies.

### Troubleshooting

- **A neutral placeholder instead of your banner** — the first render is still queued. It
  is normally replaced within a few minutes; the placeholder is served so a forum never
  caches a broken image in the meantime.
- **An "unavailable" image** — the URL points at a signature that is disabled, was deleted,
  or whose link you rotated away (the old link 404s by design). Copy the current Direct URL
  from your list.
- **A "too many requests" or throttle message** — you (or the page embedding the image)
  asked for it too quickly. Wait a minute and try again.
- **A signature stuck showing "failed"** — a render error kept the last good image in place.
  Editing the signature or pressing **Regenerate** clears the error and retries; if it keeps
  failing, ask leadership, who can see the technical reason in the admin console.

## Newbro milestones

New-player onboarding presents a checklist of milestones — account setup, key skills,
your first doctrine ship, early activity — some of which tick themselves off automatically
as your synced data catches up, and some you mark done yourself. It sits alongside a
searchable glossary of EVE and corp-specific terms, and a "what to do today" surface aimed
squarely at your first few weeks. It's visible to anyone, signed in or not, and is
personalised once you are.

---

Next: see [Fleets and doctrines](./fleets-and-doctrines.md) to check what you can fly and
sign up for an operation.
