# Administrator Handbook

This handbook is for corporation leadership — CEO, directors, officers, FCs, recruiters,
and mentors — who run the corporation's operations using [FORCA] Command Grid. If you're a
line pilot looking for how to *use* the app rather than administer it, see the
[End-User Guide](../end-user-guide/README.md) instead.

## Administrator responsibilities

Leadership is responsible for:

- Deciding which features are on, and who can see the audience-controlled ones (member
  services, doctrines, navigation, raffles).
- Granting officer rank and the lateral `recruiter`/`fc` capabilities, and approving
  Director grants (dual control).
- Authorising the corp-level ESI scopes that power roster, finance, structures, contacts,
  mining, and industry dashboards.
- Configuring and running the leadership-only subsystems: killboard administration,
  operations, readiness, Command Intelligence, recommendations, Pingboard, SRP, mining
  payouts, industry settings, mentorship, raffles, recruitment, and corp finance.
- Data retention and access governance for the deployment.

None of this requires touching the server or redeploying — day-to-day administration
happens entirely inside the **Admin Console**.

## The /ops/ console, in one line

Everything above is managed at `/ops/` — a native, role-gated console purpose-built for
this app (there is no stock Django admin surface to learn). See
[Console overview](./console-overview.md) for the full tour.

## Table of contents

| Page | What's in it |
|---|---|
| [Console overview](./console-overview.md) | The `/ops/` hub, audit log, integration health, and officer vs. director gating |
| [Members and roles](./members-and-roles.md) | How access works: automatic membership, auto-Director, granting officer/recruiter/fc, dual control |
| [Features and audiences](./features-and-audiences.md) | Turning features on/off and setting audiences at `/ops/admin/features/` |
| [ESI and data](./esi-and-data.md) | Which director scopes power which dashboards, granting them, retention, and troubleshooting empty data |
| [Leadership features](./leadership-features.md) | Running each leadership subsystem: killboard, operations, readiness, Command Intelligence, Pingboard, SRP, mining, industry, mentorship, raffles, recruitment, finance |
| [Workflows](./workflows.md) | Recommended day-to-day, weekly, and monthly checklists |

For the full permission model, see [Permissions and roles](../permissions-and-roles.md).
For every environment variable and database-stored setting, see
[Configuration reference](../configuration-reference.md). For the complete feature list
this handbook is grounded in, see the [Feature catalog](../feature-catalog.md).
