# Database failover runbook

Applies to the production PostgreSQL estate. Owned by the Database Team.

## Trigger
Failover is triggered by the primary being unreachable for 90 seconds, or by an explicit
decision from the incident commander. Failover is never triggered by a slow query alone.

## Procedure
1. Confirm the standby is streaming and its replay lag is under 30 seconds.
2. Promote the standby and confirm it accepts writes.
3. Repoint the connection pooler and wait for the pool to drain idle connections.
4. Confirm application write success rate has recovered above 99 percent.
5. Rebuild the former primary as a standby before the next maintenance window.

## Reverting
A promoted standby is not demoted. Reverting means a second failover, and it requires a
second incident commander decision.
