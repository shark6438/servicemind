# Release rollback criteria

Applies to application releases. Owned by the Release Team.

## Automatic rollback
A release rolls back automatically when the error rate exceeds 2 percent for 5 minutes, or
when the p95 latency exceeds twice the pre-release baseline for 5 minutes.

## Manual rollback
The release manager may roll back at any time before the release is declared stable. A
release is declared stable 60 minutes after deployment with no automatic rollback.

## Data migrations
A release carrying a forward-only migration cannot be rolled back; it is rolled forward
with a fix. The rollback plan states which migrations are forward-only before approval.

## Record
Every rollback records the triggering signal, the time, and the version returned to.
