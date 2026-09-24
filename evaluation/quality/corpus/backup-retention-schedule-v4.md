# Backup retention schedule (revision 4)

Supersedes revision 3. This is the schedule in force.

## Retention
Daily backups are kept 35 days. Weekly backups are kept 13 weeks. Monthly backups are
kept 13 months. Yearly backups are kept 7 years.

## Deletion
Deletion is automated and runs nightly. A backup under a legal hold is skipped by the
deletion job and is never deleted by an operator acting manually.

## Offsite
A copy is written offsite within 4 hours of the backup completing.

## Restore requests
A restore request is answered within 4 hours for a full restore and 1 hour for a table.
