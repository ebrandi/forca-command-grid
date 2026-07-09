# Features and Audiences

## Table of contents

- [Where this is configured](#where-this-is-configured)
- [Plain on/off features](#plain-onoff-features)
- [Audience-controlled features](#audience-controlled-features)
- [The member services](#the-member-services)
- [How the gate is enforced](#how-the-gate-is-enforced)

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
