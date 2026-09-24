# Database backup verification

Applies to the production PostgreSQL estate. Owned by the Database Team.

## Schedule
A full backup runs at 01:00 local time daily. A restore test runs weekly on Sunday and
restores the most recent full backup into an isolated instance.

## Retention
Daily backups are retained for 35 days. Monthly backups are retained for 13 months. A
backup is not considered retained until its checksum has been recorded.

## Verification
A backup is verified when the restore test completes and a row count on three nominated
tables matches the source within 0.1 percent. A backup that fails verification is
reported and the next daily run is marked degraded.

## Recovery objectives
The recovery point objective is 15 minutes. The recovery time objective is 4 hours.
