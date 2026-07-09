# Documentation Maintenance

Documentation is part of the product. This page describes how the documentation set is
organised and how to keep it accurate as the application changes.

## Table of contents

- [Structure](#structure)
- [When to update what](#when-to-update-what)
- [Style conventions](#style-conventions)
- [Validating documentation](#validating-documentation)

## Structure

```
README.md                     Project entry point
CONTRIBUTING.md               Contributor entry point
SECURITY.md                   Vulnerability reporting policy
CODE_OF_CONDUCT.md            Community standards
NOTICE.md                     Third-party acknowledgements
handbooks/
  README.md                    Documentation landing page (audience routing)
  product-overview.md         What it is and why
  feature-catalog.md          Every implemented feature (source of truth for features)
  configuration-reference.md  All configuration inputs
  permissions-and-roles.md    Roles, permissions, ESI scopes, audiences
  data-and-privacy.md         Data stored and protected
  third-party-services.md     External integrations
  glossary.md                 Terminology
  end-user-guide/             Pilots and newbros
  administrator-handbook/     Corp leadership
  contributor-handbook/       Developers and reviewers
  operator-handbook/          IT operators
  reference/                  Detailed reference tables (this directory)
```

## When to update what

Update documentation in the **same pull request** as the change that affects it.

| Change | Update |
|---|---|
| New or changed environment variable | [configuration-reference.md](../configuration-reference.md) and [environment-variables.md](./environment-variables.md) |
| New or changed feature | [feature-catalog.md](../feature-catalog.md), plus the relevant end-user/administrator page |
| New or changed role, permission, or ESI scope | [permissions-and-roles.md](../permissions-and-roles.md) |
| New or changed scheduled task | [background-jobs.md](./background-jobs.md) |
| New management command or script | [cli-and-scripts.md](./cli-and-scripts.md) |
| New dependency | [dependency-inventory.md](./dependency-inventory.md), [licence-review.md](./licence-review.md), [`NOTICE.md`](../../NOTICE.md) |
| New model or schema change | [database.md](./database.md) and [contributor-handbook/domain-model.md](../contributor-handbook/domain-model.md) |
| New API/route pattern | [api-endpoints.md](./api-endpoints.md) |
| Deployment or operations change | the relevant [operator-handbook](../operator-handbook/README.md) page |

The **feature catalog is the source of truth** for which features exist; the audience
handbooks describe how to use them and should not introduce features that are not in the
catalog.

## Style conventions

- Document **only implemented behaviour**. Do not describe unreleased or speculative work.
- Use lowercase kebab-case filenames (except conventional files like `README.md`).
- Every page has a title and a table of contents.
- Use tables for settings, permissions, jobs, and dependencies.
- Use Mermaid diagrams where they aid understanding.
- Use safe, dummy example values only — never real secrets, hostnames, IPs, or member data.
- Use "recommended" wording for operational best practices that the software does not
  enforce.
- Prefer relative Markdown links between pages.

## Validating documentation

Before publishing changes:

- Check that all internal links resolve (relative paths, correct filenames).
- Re-read for professional tone and consistency with the feature catalog.
- Confirm no secrets or private operational details were introduced.
- Verify any command examples against the current `Makefile`, `scripts/`, and management
  commands.
