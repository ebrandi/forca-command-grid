# Linked Pilots — flying your alts without logging out

Most capsuleers have more than one pilot. Command Grid lets you link them to one account and
switch between them with two clicks, instead of logging out and back in every time.

**Linking pilots does not merge them.** Each pilot keeps their own data, permissions,
corporation membership, ESI authorisation and dashboard. Switching pilots changes the identity
you are currently using in Command Grid — it never pools your pilots together.

## Linking a pilot

1. Open **Pilot → Linked Pilots**, or click your portrait in the sidebar and choose
   **Link Another Pilot**.
2. Press **Link Another Pilot**. You are sent to EVE Online's own login screen.
3. **Choose the pilot you want to link** on CCP's character-selection screen, and authorise.
4. You come back to Linked Pilots, and the new pilot is there.

Every pilot must be authorised individually through EVE SSO. There is no way to add a pilot by
typing their name — and that is the point: authorising a pilot through CCP *is* the proof that
you control them. (CCP does not tell third-party apps which characters share an EVE account, so
nothing can be inferred; each one is a separate, deliberate authorisation.)

If a pilot is already linked to a *different* Command Grid account, the link is refused. Ask an
officer to detach them first.

## Switching pilots

Click your portrait at the bottom of the sidebar (or, on mobile, at the top right) and pick a
pilot. You stay on the same page if that page is available to the pilot you switched to;
otherwise you land on their dashboard and Command Grid tells you why.

The pilot you are flying is shown in the sidebar at all times. It survives page navigation,
refreshes and new tabs.

When you sign in, the pilot you signed in **as** becomes the pilot you are flying.

## What changes when you switch

Everything that is *about you* now describes the pilot you switched to: your skills, your
assets, your readiness, your killboard record, your orders, your dashboard.

**Your permissions change too, and this is deliberate.**

* If you are a Director on your main and you switch to a normal member alt, you lose
  Director-only access until you switch back. Corporation authority belongs to the pilot who
  actually holds the in-game role — not to everyone who happens to share an account with them.
* If you switch to a pilot who is not in the corporation, that pilot sees what any outsider
  sees. Your corp pilots are unaffected, and you can switch straight back.

Your **interface language does not change** when you switch pilots. It belongs to you, not to
any one pilot.

## The Linked Pilots page

**Pilot → Linked Pilots** shows every pilot on your account with:

* their corporation and alliance,
* when you linked them and when you last flew them,
* their **ESI authorisation status**, and any permissions they are missing,
* when their data last synchronised.

From here you can **switch**, **reauthorise**, set your **main pilot**, or **unlink**.

## Reauthorising

EVE authorisations expire, and they stop working if you revoke them at CCP. A pilot in that
state shows an **ESI Authorisation Required** warning, and their data stops refreshing.

You can still switch to a pilot with a broken authorisation — a dead token never locks you out
of your own pilot. To fix it, press **Reauthorise** and complete the EVE login **as that pilot**.

> Command Grid cannot preselect a pilot on CCP's login screen — only you can. If you authorise a
> different pilot than the one you meant to fix, you are told so plainly, and that pilot is
> linked instead.

## Your main pilot

Your **main** is the pilot a fresh session starts on if you have not signed in as someone else.
Set it with **Make Main**. You always have exactly one.

## Unlinking

**Unlink** removes a pilot from your account and revokes their EVE authorisation. You are asked
to confirm, and the pilot is named in the confirmation.

* **Their history is kept.** Killmails, contributions and corporation records stay — they are
  part of the corp's record, not just yours. What is destroyed is the *authorisation*; what is
  severed is the *link*.
* You can link the same pilot again later with a fresh EVE SSO authorisation.
* **You cannot unlink your last pilot.** An EVE pilot is how you sign in, so releasing the only
  one would lock you out of your own account. Link another pilot first — or, if you really want
  to leave, delete your account from the [Privacy page](./account-and-esi.md).
* If you unlink the pilot you are currently flying, Command Grid switches you to another one.
* If you unlink your main, another pilot becomes your main.
