# Database Backup and Recovery Standard

## Scope

Operational standard for transactional databases that hold customer and
configuration data. Covers retention, restore drill cadence, and disaster-recovery
recovery-point objectives.

## Schedule

| Tier | Full backup | Log shipping | Retention |
|---|---|---|---|
| Core | Daily 22:00 UTC | Every 15 minutes | 35 days |
| Standard | Daily 22:00 UTC | Hourly | 14 days |

## Recovery procedure

1. Open a restore request in the change system with the source backup date.
2. Restore to a staging instance first and verify row counts and checksums.
3. Point the dependent service at the restored copy during the approved window.
4. Update the runbook with measured recovery time; alert if the target is exceeded.

## Targets

Recovery point objective is 15 minutes for core databases. Recovery time objective
for the primary customer database is four hours.

## Ownership

Database engineering owns backup jobs and restore drills. Service owners approve
restore requests because data recovery can contradict application-level caches.
