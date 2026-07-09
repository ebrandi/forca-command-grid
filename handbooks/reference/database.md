# Database Reference

[FORCA] Command Grid uses **PostgreSQL 16** as its system of record. The schema is managed
entirely through Django migrations — there is no hand-maintained DDL. This page gives a
high-level map of the schema by domain; the authoritative definition is the set of
`models.py` modules under each app in [`apps/`](../../apps), and the ORM/domain view is in
[contributor-handbook/domain-model.md](../contributor-handbook/domain-model.md).

## Table of contents

- [Conventions](#conventions)
- [Schema by domain](#schema-by-domain)
- [Reference data (SDE)](#reference-data-sde)
- [Migrations](#migrations)
- [Inspecting the database](#inspecting-the-database)

## Conventions

- **Primary keys** default to `BigAutoField`; some tables use natural EVE ids as the primary
  key (e.g. character id, corporation id, killmail id, structure id).
- **`ProvenanceMixin`** is applied to rows sourced from ESI/external data to track where and
  when a row was populated.
- **Encrypted columns:** OAuth access/refresh tokens and integration credentials are stored
  encrypted (Fernet) and exposed only through decrypting accessors.
- **Append-only tables:** audit logs, sync ledgers, and several event/validation trails are
  written but never updated in place.
- **Configuration** is stored partly as dedicated singleton config tables and partly as
  key/value rows in the `AppSetting` store.

## Schema by domain

The schema is organised by the same bounded contexts as the apps. Key tables per domain:

| Domain (app) | Representative tables |
|---|---|
| **Identity & RBAC** (`identity`) | `User`, `Role`, `Permission`, `RoleAssignment`, `RoleChangeRequest` |
| **EVE SSO** (`sso`) | `EveCharacter`, `AuthToken` (encrypted), `EveScopeGrant` |
| **Characters** (`characters`) | `CharacterSkillSnapshot`, `SkillQueueSnapshot`, `CharacterAttributes`, `CharacterFittedShip` |
| **Corporation** (`corporation`) | `EveCorporation`, `EveAlliance`, `CorpMember`, `CorpWalletDivision`, `CorpWalletJournalEntry`, `Contact`, `CorpStructure`, `MoonExtraction`, `PartnerAlliance`, `FriendlyCorporation`, `EveName` |
| **Killboard** (`killboard`) | `Killmail`, `KillmailParticipant`, `KillmailItem`, `FitDeviation`, `BattleReport`, `CombatMetric`, `Watchlist`, `CombatRankTitle`, `MonthlyPilotKillStat` |
| **Doctrines** (`doctrines`) | `Doctrine`, `DoctrineCategory`, `DoctrineFit`, `DoctrineRequirement`, `SkillRequirement`, `DoctrineImportBatch` |
| **Skills** (`skills`) | `SkillPlan`, `SkillPlanStep`, `IdleQueueNudge` |
| **Industry** (`industry`) | `IndustryProject`, `IndustryProjectItem`, `ProductionStep`, `MaterialRequirement`, `ShoppingList`, `IndustryEconomyConfig` |
| **ERP** (`erp`) | `BuildJob`, `Blueprint`, `CorpIndustryJob`, `CharacterIndustryJob`, `Delivery` |
| **Market** (`market`) | `MarketLocation`, `MarketPrice`, `MarketOrderSnapshot`, `MarketHistory`, `MarketWatch` |
| **Stockpile** (`stockpile`) | `Stockpile`, `StockpileItem`, `StockReservation`, `HaulingTask`, `Asset`, `AssetLocation` |
| **Mining** (`mining`) | `MiningObserver`, `MiningLedgerEntry`, `MiningTaxConfig`, `MiningPayout`, `MiningPayoutLine`, `MiningMilestone` |
| **Planetary** (`planetary`) | `PiMaterial`, `PiPlanetType`, `PiSchematic`, `PlanetaryConfig`, `PiPlan`, `PiPlanPlanet`, `PiColony` |
| **Logistics** (`logistics`) | `RateCard`, `CourierContract`, `CorpContract` |
| **Buyback** (`buyback`) | `BuybackConfig`, `BuybackOffer`, `GuaranteedBuybackConfig`, `GuaranteedBuyout` |
| **Store** (`store`) | `StoreConfig`, `StoreOrder` |
| **Navigation** (`navigation`) | `AnsiblexBridge`, `CynoBeacon`, `JumpPlannerConfig`, `SavedJumpRoute` |
| **Operations** (`operations`) | `Operation`, `OperationShipSlot`, `OperationCommitment`, `OperationAttendance`, `StructureTimer`, `SovStructure`, `OperationTemplate` |
| **SRP** (`srp`) | `SrpProgram`, `SrpRule`, `SrpClaim`, `SrpBudget` |
| **Readiness** (`readiness`) | `ReadinessSnapshot`, `ReadinessFinding`, `ReadinessAlert`, `ExecutiveReport`, `PilotRecommendation`, `MandatoryShip`, `StrategicRoleTarget` |
| **Command Intelligence** (`command_intel`) | `IntelligenceSnapshot`, `OperationalConstraint`, `IntelligenceReport`, `CourseOfAction`, `Campaign`, `PilotDirective`, `ConversationTurn`, `BattleAnalysis` |
| **Recommendations** (`recommendations`) | `Recommendation`, `Alert`, `ActionQueueItem`, `CorpNotification`, `RelayedMail`, `RecommendationConfig` |
| **Pingboard** (`pingboard`) | `ChannelProvider`, `AlertTemplate`, `AutomationRule`, `Alert`, `AlertDelivery`, `CalendarEvent`, `PilotContactChannel` |
| **Pilots** (`pilots`) | `PilotPreference`, `ContributionEvent`, `ContributionWeights`, `MonthlyWeightSnapshot` |
| **Onboarding** (`onboarding`) | `OnboardingMilestone`, `OnboardingProgress`, `GlossaryTerm` |
| **Mentorship** (`mentorship`) | `MentorshipProgram`, `MentorshipTrack`, `MentorshipTask`, `MentorProfile`, `MenteeProfile`, `MentorshipPairing`, `MentorshipRewardLedger` |
| **Raffle** (`raffle`) | `RaffleContest`, `RafflePrize`, `RaffleTicketLedgerEntry`, `RaffleDraw`, `RaffleDrawResult`, `RaffleConfig` |
| **Tasks** (`tasks`) | `Task`, `TaskEvent` |
| **Comms access** (`comms_access`) | `CommsAccount`, `PlatformCredential` (encrypted), `EntitlementMapping`, `AccessSyncLedger` |
| **Admin & audit** (`admin_audit`) | `AuditLog` (append-only), `AppSetting`, `DataRetentionPolicy` |

## Reference data (SDE)

The `sde` app holds a relational subset of CCP's Static Data Export — the largest tables by
row count. Key tables: `SdeType`, `SdeGroup`, `SdeCategory`, `SdeSolarSystem`, `SdeRegion`,
`SdeSystemJump`, `SdeStation`, `SdeTypeSkill`, `SdeBlueprintMaterial`, `SdeTypeMaterial`,
`SdeInventionProduct`. Several name columns carry trigram (GIN) indexes to power
autocompletes. This data is loaded by operators via management commands (see
[cli-and-scripts.md](./cli-and-scripts.md)).

## Migrations

- Migrations live in each app's `migrations/` directory and are applied with
  `python manage.py migrate` (wrapped by `make migrate` and run automatically by the deploy
  and update flows).
- Some migrations add composite or expression indexes to keep heavy aggregate pages fast.
- Never edit an applied migration; add a new one. See
  [contributor-handbook/domain-model.md](../contributor-handbook/domain-model.md).

## Inspecting the database

```bash
make dbshell                                   # psql shell in the postgres container
docker compose -f docker-compose.prod.yml exec web python manage.py showmigrations
docker compose -f docker-compose.prod.yml exec web python manage.py dbshell
```

Back up before any manual change — see
[operator-handbook/backup-and-restore.md](../operator-handbook/backup-and-restore.md).
