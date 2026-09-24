# Service account lifecycle

Applies to non-human accounts used by integrations. Owned by the Platform Team.

## Creation
A service account requires a named human owner, a stated purpose, and an expiry no longer
than 24 months. Accounts without an owner are refused at creation.

## Credential rotation
Credentials rotate every 90 days. Rotation is automated; a manual rotation is recorded
against the owner. The overlap window during which both the old and new credential are
accepted is 24 hours and is never extended.

## Review
Every service account is reviewed each quarter. An account whose owner has left, or whose
purpose is no longer stated, is suspended at the review rather than carried to the next.

## Deletion
Deletion follows suspension by 30 days. Logs for the account are retained for 400 days
after deletion.
