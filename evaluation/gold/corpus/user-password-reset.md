# User Password Reset and Account Unlock

## Scope

Self-service and assisted password resets for corporate directory accounts, and
unlocking accounts that reached the failed-attempt threshold.

## Process

1. Verify the caller's identity with two independent factors.
2. Run the reset in the identity directory, not in a downstream application.
3. Force a change at next sign-on; do not set a temporary password that lingers.
4. If the account is locked from failed attempts, check for a brute-force pattern
   before unlocking.

## Signals

Repeated resets for the same user within a week indicate a token or sync problem,
not a password problem. Raise a ticket for the identity team when resets cluster.

## Prohibited

Never email a password, never reset on a voice call alone, and never disable the
account lockout counter "temporarily".
