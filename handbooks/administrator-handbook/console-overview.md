# Console Overview

## Table of contents

- [The Admin Console hub](#the-admin-console-hub)
- [The audit log](#the-audit-log)
- [The integration-health page](#the-integration-health-page)
- [Officer vs. director gating](#officer-vs-director-gating)
- [Major console sections](#major-console-sections)

## The Admin Console hub

`/ops/admin/` is the entry point to every administrative surface in [FORCA] Command Grid.
It is a native, role-gated console built specifically for this application — there is no
separate database-admin tool to learn, and no stock framework admin exposed by default.
Reaching `/ops/` at all requires at least **officer** rank; most individual sections
additionally require **director**.

## The audit log

`/ops/audit/` is an investigable log of sensitive administrative actions: configuration
changes, manual syncs, and financial views. Every console page that changes something
meaningful writes an entry here — use it to answer "who changed this, and when" without
guesswork.

## The integration-health page

`/ops/health/` reports the live status of the deployment's external integrations — ESI
connectivity, background job liveness, and the health of any armed Pingboard channels or
comms-access platform. Check here first whenever something looks like it stopped syncing.

## Officer vs. director gating

The console follows the same [role tiers](../permissions-and-roles.md#role-tiers) as the
rest of the app:

- **Officer** gets you into the hub and into boards that are operational rather than
  sensitive — recommendations, the readiness dashboard, operations management, kill-feed
  settings, and most day-to-day console pages.
- **Director** is required for anything sensitive or corp-wide: finance, access
  governance, corp-level ESI scope grants, retention policy, and the highest
  classification tiers of Command Intelligence.
- **Admin** (superuser) bypasses all gates and is reserved for break-glass operations —
  it should not be a pilot's day-to-day role.

Every action taken through the console — sensitive or not — is checked against these tiers
server-side, not just hidden in the navigation; visiting a URL directly gets the same
gate as clicking a link to it.

## Major console sections

Derived from the [Feature catalog](../feature-catalog.md)'s per-feature "Configurable"
notes and its "Admin console and audit" entry:

| Section | What it configures |
|---|---|
| **Services & features** (`/ops/admin/features/`) | Turn features on/off; set audiences for doctrines, navigation, raffles, and the member services. See [Features and audiences](./features-and-audiences.md). |
| **Members & roles** (`/ops/admin/members/`) | Officer/recruiter/fc grants, dual-control Director approval, per-member audit. See [Members and roles](./members-and-roles.md). |
| **Access governance** (`/ops/admin/access/`) | Partner-alliance and friendly-corporation records, character recovery/detach. |
| **Doctrines & content** (`/ops/admin/doctrines/`, `/ops/admin/content/`) | The doctrine library, fits, requirements, XML/killmail/saved-fit import, knowledge-base pages. |
| **Retention** (`/ops/admin/retention/settings/`) | Data-retention windows and the member-leave sweep policy. |
| **Per-subsystem settings** | Industry, corporation structure alerts, recommendations relay/tuning, notifications, Pingboard channels/automation/templates, jump-planner defaults, comms-access (Discord role sync), and more — one settings page per subsystem, each covered in [Leadership features](./leadership-features.md). |
| **Maintenance-task launcher** | Runs one-off management operations (syncs, backfills) from the console instead of the command line. |

Every section above is reachable only to the role tier that actually needs it — pointing a
lower-privileged member at a console URL directly returns the same access-denied result
the navigation already implies.
