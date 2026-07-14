# Account and ESI

## Table of contents

- [The ESI Scopes page](#the-esi-scopes-page)
- [Granting and revoking optional scopes](#granting-and-revoking-optional-scopes)
- [Reconnecting and reconciling ESI](#reconnecting-and-reconciling-esi)
- [Disconnecting a character](#disconnecting-a-character)
- [Linking Discord, Telegram, or WhatsApp for pings](#linking-discord-telegram-or-whatsapp-for-pings)
- [Setting per-category mute preferences](#setting-per-category-mute-preferences)
- [Your privacy and data-rights page](#your-privacy-and-data-rights-page)

## The ESI Scopes page

The **ESI Scopes page** (`/auth/eve/scopes/`) is your one-stop control panel for what data
you share beyond the baseline login scopes. Every optional feature scope the app knows
about is listed there — what it unlocks, and whether it's currently granted for each of
your linked characters. Some scopes marked "Director" additionally require you to hold a
matching in-game corporation role; those are described from the corp's side in the
[Administrator Handbook](../administrator-handbook/esi-and-data.md).

## Granting and revoking optional scopes

To turn on a feature that needs extra data — say, showing your personal assets, or
tracking your own industry jobs — grant that scope from the ESI Scopes page. You'll be
sent back through EVE SSO to authorise the additional permission, then land back on the
page with it marked active. Revoking a scope is just as direct, and takes effect
immediately: the feature it powered simply stops reading that data for you.

## Reconnecting and reconciling ESI

If a character's token has expired, or a feature suddenly stops updating, use
**Reconcile** (`/auth/eve/scopes/reconcile/`) to refresh your scope state without
re-authorising everything from scratch. If reconcile doesn't clear the problem, sign out
and sign back in with that character to get a fresh token.

## Disconnecting a character

To remove a linked character entirely, use its **disconnect** action from your account
pages. This revokes the app's stored token for that character; it does not affect the
character itself or any of your other linked characters.

## Linking Discord, Telegram, or WhatsApp for pings

If your corp uses Pingboard for alerts and reminders, you can link your own handle on any
channel it has armed:

- Go to your Pingboard **channels** page (`/pingboard/channels/`).
- Follow the link/verify flow for Discord, Telegram, or WhatsApp — each requires a short
  verification step so pings only ever go to an account you actually control.
- Once linked, you'll get direct pings for anything routed to you personally, alongside
  whatever the corp broadcasts to shared channels.

The language a ping arrives in depends on who it was addressed to. Anything sent to you
personally — an in-app notification, an EVE mail, or a direct message to your linked
Telegram or WhatsApp handle — is written in the language you chose. Anything the corp posts
to a shared channel has no single recipient, so it goes out once in the one broadcast
language leadership has set: that covers its Discord channel, and any group or channel it
has configured for Telegram or WhatsApp. Discord is always a shared channel, so even with
your Discord account linked, corp pings reach you there in the broadcast language rather
than yours.

Two kinds of ping are never translated: an officer's free-text alert, and any alert your
corp wrote its own template for. Both are delivered exactly as they were typed. The app
translates its own built-in messages, not corp-authored wording.

## Setting per-category mute preferences

From your channel preferences page (`/pingboard/channels/prefs/`) you can mute entire
categories of pings you don't want to be bothered with personally — while still seeing
them if you check the app. The one exception is **EMERGENCY**-priority alerts, which
cannot be muted, by design.

If your corp also runs Discord role sync, linking your Discord account (from `/comms/`)
keeps your roles there in step with your corp membership automatically — nothing to
re-request when your responsibilities change.

## Your privacy and data-rights page

Every pilot — member or not — has their own data-rights page under `/privacy/`. From
there you can see what the app holds about you and request deletion of your own data. See
[Data and privacy](../data-and-privacy.md) for the full picture of what's collected, how
long it's kept, and what happens to your data if you leave the corporation.

One of the things the app stores against your account is your language choice: when you're
signed in, picking a language saves it to your account, not just to the browser you picked
it in, which is why it follows you from one device or browser to the next, and why it
applies to all of your characters at once. You set it from the language selector in the
sidebar — see [Navigating the app](./navigating-the-app.md#choosing-your-language).

---

If something isn't working the way you expect, check
[Troubleshooting](./troubleshooting.md) next.
