# Features and Audiences

## Table of contents

- [Where this is configured](#where-this-is-configured)
- [Plain on/off features](#plain-onoff-features)
- [Audience-controlled features](#audience-controlled-features)
- [The member services](#the-member-services)
- [How the gate is enforced](#how-the-gate-is-enforced)
- [Languages](#languages)

## Where this is configured

Every member-facing feature is managed at **Admin Console → Services & features**
(`/ops/admin/features/`). Every feature is **enabled by default**; leadership decides
what, if anything, to turn off or restrict.

## Plain on/off features

Most features are a simple switch: visible, or hidden. Turning one off removes it from the
navigation and 404s a direct link to it.

## Audience-controlled features

A handful of features additionally support a 4-state **audience**, controlling *who* can
see them rather than just on/off:

| Audience | Who can see it |
|---|---|
| `disabled` | Nobody |
| `corp` | Home-corporation members |
| `alliance` | Members plus registered partner-alliance and friendly-corporation pilots |
| `public` | Everyone, including anonymous visitors |

The audience-controlled features are:

- **Doctrines & Shipyard** — default `corp`.
- **Navigation & maps** — default `public`.
- **Raffle contests** — default `corp`.

Widening one of these to `alliance` only helps allies you've actually registered as
partner alliances or friendly corporations under **Access governance** — it does not open
the feature to every alliance member everywhere.

## The member services

**Freight**, **buyback**, and the **corp store** are external-facing services the
corporation offers to its own members and, optionally, to allies or the public. Each has
its **own** audience setting on its own settings page (not the general features page),
because each has separate business reasons to be opened wider or kept internal — for
example, offering buyback to allies to earn ISK, while keeping the corp store internal to
protect fulfilment capacity.

## How the gate is enforced

The audience gate is enforced in two places, so it can't be bypassed by guessing a URL:

- **Navigation** — a disabled or out-of-audience feature simply doesn't render as a nav
  link.
- **`FeatureGateMiddleware`** — a direct request to a disabled or out-of-audience view
  returns a 404, regardless of the requester's role.

This is the same mechanism documented in
[Permissions and roles: Feature flags and audiences](../permissions-and-roles.md#feature-flags-and-audiences);
this page is the "where do I click" companion to that reference.

## Languages

The interface ships in nine languages: English, Portuguese (Brazil), Spanish, French,
Russian, German, Simplified Chinese, Korean and Japanese. English is the canonical source
language and can never be disabled.

Which of the other eight the app offers is set at **Admin Console → Localisation**
(`/ops/admin/i18n/`), a Director-only page. Out of the box **only English is enabled**, so
nothing changes for members until leadership turns a locale on. The shipped defaults apply
only until the form is saved for the first time; after that, the stored settings are what
count.

**Enabling a locale is a corp-wide flip, not a preview.** Browser detection is on by
default, so the moment you tick a locale, every pilot who has not chosen a language of
their own and whose browser prefers that language will see the interface in it on their
next page load. If you want to look at a locale before committing the corp to it, untick
**Detect language from the browser** at the same time you enable it — the locale is then
reachable only by pilots who deliberately pick it in the language selector.

The other controls on the page:

- **Default language** — the language used when nothing else resolves.
- **Broadcast language** — the single language used for group pings that have no one
  recipient (see [Pingboard](./leadership-features.md#pingboard)).
- **Let signed-out visitors pick a language** — whether the selector is shown to
  logged-out visitors at all.

The panel also shows per-locale translation coverage, counted from the catalogues shipped
with the build. The translations are machine drafts with an LLM native-review pass, not
professional human review, so check a locale's coverage before turning it on for the whole
corp. EVE game-data names — ships, modules, systems — are not localised yet.

Individual pilots can override the corp default from their own account; that is covered in
the end-user guide under
[Navigating the app: Choosing your language](../end-user-guide/navigating-the-app.md#choosing-your-language).
