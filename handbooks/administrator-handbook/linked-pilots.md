# Linked Pilots — what changes for leadership

Members can link several EVE pilots to one Command Grid account and switch between them without
logging out. This page covers what that changes for you, and how to answer the questions you
will be asked.

## The one thing to understand

**Corporation authority now belongs to the pilot, not to the account.**

Before this feature, if any of a member's pilots was an in-game Director, the whole *account*
became a Command Grid Director — and every other pilot on it wielded Director authority,
including pilots in unrelated corporations. That was a privilege-escalation path, and it is now
closed.

A member's authority is the **lesser** of what you granted them and what the pilot they are
currently flying can substantiate:

| The pilot they are flying | What they can do |
| --- | --- |
| Not in the corporation | Nothing internal. They see what any outsider sees, and get the recruitment surface. |
| In the corporation, not an in-game Director | Everything up to Officer, if you granted it. **No Director access.** |
| In the corporation, and an in-game Director | Everything you granted them. |

So a Director who switches to their mining alt loses Director access until they switch back.
This is deliberate, and it is what the "wrong seat" problem looks like when it is fixed: you
cannot accidentally act with Director authority from a pilot who does not hold the role.

**Officer stays with the person** across all of their corp pilots. Officer is a trust decision
you made about a human, and unlike Director there is no in-game role to check it against.

**Admin and superuser are not affected.** They are platform roles, not corp roles.

## Things you will be asked

**"I've lost my Director access!"**
They have switched to a pilot who is not an in-game Director. Ask them to switch back to their
Director pilot from the selector at the bottom of the sidebar. If they *are* on their Director
pilot and it still says no, their in-game Director role check is stale — see below.

**"Command Grid says I'm not a Director but I am."**
The in-game Director check runs every six hours (`sso.reconcile_director_roles`) and needs the
`esi-characters.read_corporation_roles.v1` scope. If they recently gained the role in-game, or
have not granted that scope, ask them to **Reauthorise** the pilot on **Pilot → Linked Pilots**
and wait for the next reconcile.

**"I can't unlink my pilot."**
You cannot unlink your last pilot — an EVE pilot is how you sign in, so releasing the only one
would lock you out of your own account. They must link another pilot first, or delete their
account from the Privacy page.

**"It says my pilot belongs to another account."**
Exactly what it says: that pilot is already linked to a *different* Command Grid account. Someone
must detach it first (Members console → the account holding it → Detach). Command Grid never
tells a member *whose* account holds a pilot.

Note that they can only ever see this message by completing a full EVE SSO authorisation for that
pilot — which is proof they control it in-game. If two people are both authorising the same pilot,
that is worth a conversation.

## What is audited

Every security-relevant action writes an `AuditLog` row you can read in the console:

| Action | Meaning |
| --- | --- |
| `pilot.linked` | a new pilot was authorised and attached to an account |
| `pilot.link_rejected` | refused — `reason` is `ownership_conflict`, `owner_changed` or `session_changed` |
| `pilot.switched` | active pilot changed (`from` and `to` in the metadata) |
| `pilot.switch_denied` | someone tried to switch to a pilot they do not hold — **worth looking at** |
| `pilot.unlinked` | a link was severed and its tokens destroyed |
| `pilot.main_changed` | the account's main pilot changed |
| `pilot.reauthorised` | an existing pilot's ESI authorisation was renewed |

No tokens, OAuth codes or ESI payloads are ever written to the audit log or the application log.

`pilot.switch_denied` is the one to watch. A member switching between their own pilots never
produces it; it means a request named a pilot the caller does not hold.

## Detaching a member's pilot

Detaching a pilot from the Members console still works exactly as before, and it now also clears
that pilot's in-game Director flag — so a detached pilot cannot carry Director authority into
anyone's session. If the member is currently flying the pilot you detach, their very next request
falls back to another of their pilots automatically.

## Data isolation

Linking pilots does **not** merge them. Each pilot keeps its own skills, assets, wallet,
readiness, killboard record, corporation roles, ESI authorisation, quest log and dashboard. A
member switching pilots changes which pilot they are acting as; it never pools their pilots
together, and no report you run will combine them.
