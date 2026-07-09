# Getting Started

## Table of contents

- [Signing in with EVE SSO](#signing-in-with-eve-sso)
- [What ESI authorisation means](#what-esi-authorisation-means)
- [What data you share, and why](#what-data-you-share-and-why)
- [Login scopes vs. optional feature scopes](#login-scopes-vs-optional-feature-scopes)
- [What a prospective recruit can see](#what-a-prospective-recruit-can-see)
- [Next steps](#next-steps)

## Signing in with EVE SSO

[FORCA] Command Grid uses **EVE Online's own Single Sign-On (SSO)** — there is no separate
password to create. To sign in:

1. Click **Log in with EVE Online**.
2. You're redirected to CCP's own login page. Sign in with your normal EVE Online account
   credentials there (not on this site).
3. CCP shows you exactly which permissions ("scopes") the application is requesting, and
   asks you to authorise them.
4. Once you accept, you're sent back to [FORCA] Command Grid, signed in as that character.

You can link more than one character to the same account by repeating this process from
your account pages. If you hold the **Director** role on your corporation in-game, you are
automatically recognised as a Director in the app the first time you sign in (and kept in
sync afterward) — nothing to request.

## What ESI authorisation means

ESI (the EVE Swagger Interface) is CCP's official API. When you authorise the app, you're
not handing over your account password — you're granting a scoped, revocable token that
lets the app read *specific* pieces of your character's data on your behalf (for example,
"read my skills" or "read my own killmails"). You can see exactly what was requested, and
you can revoke access at any time from your EVE Online account settings on CCP's own site,
or disconnect the character from within the app.

## What data you share, and why

Signing in shares a baseline of your character data — skills, skill queue, implants, your
own killmails, your corporation membership, and your in-game corporation roles — so the
app can show you readiness, doctrines, and rank without any extra setup. Nothing beyond
what you authorise is ever read, and refresh tokens are encrypted at rest.

For the full picture of what's collected, why, and how it's protected, see
[Data and privacy](../data-and-privacy.md).

## Login scopes vs. optional feature scopes

There are two tiers of ESI access:

- **Baseline login scopes** are requested automatically the first time you sign in. These
  cover the app's core value — your skills, killmails, and corp membership — with nothing
  extra for you to configure.
- **Optional feature scopes** unlock individual features and are granted separately, one
  at a time, whenever you choose to, from the **ESI Scopes page**
  (`/auth/eve/scopes/`). Examples include sharing your personal assets, your industry
  jobs and blueprints, your character contracts (for verifying freight deliveries), your
  saved fittings, or your Planetary Industry colonies.

Nothing extra is ever requested silently — every additional scope is something you
explicitly opt into on the ESI Scopes page, and you can revoke it there just as easily.
Some optional scopes (marked "Director" in the catalogue) additionally require you to hold
a specific in-game corporation role — those exist to power corp-wide dashboards, not your
personal data, and are covered in the
[Administrator Handbook](../administrator-handbook/esi-and-data.md).

See [Account and ESI](./account-and-esi.md) for the day-to-day mechanics of managing your
scopes.

## What a prospective recruit can see

You do **not** need to be a corp member to look around. If you sign in with a character
that is *not* in the home corporation — or if you don't sign in at all — you can still see:

- The **public killboard**, rankings, and killmail detail.
- **Public** knowledge-base pages (leadership may publish recruiting-facing guides here).
- Any feature leadership has explicitly set to a **public** audience (for example,
  navigation tools are public by default, and doctrines browsing is often visible to
  allies).
- The **onboarding / new-player surface**, aimed at getting you oriented before you join.

If you sign in and your character isn't in the home corporation, the app automatically
keeps you on this recruiting-facing surface — internal pages (the dashboard, killboard
analytics, doctrines detail, industry, readiness, and so on) simply aren't reachable until
your character is confirmed as a corp member. This is not a bug or a permissions error —
it's how the app tells prospective recruits and members apart. Once you join the corp
in-game and your membership syncs, member pages open up automatically; no request needed.

## Next steps

Once you're a recognised member, head to [Navigating the app](./navigating-the-app.md) to
get oriented, or jump straight to
[Combat and progression](./combat-and-progression.md) to see your killboard rank.
