# VPN MFA Token Store Procedure (v2)

## Status

In force from 2026-04-01. Supersedes the v1 procedure, which was written for a
single-region identity store.

## Reissue procedure

1. Suspend the user's token record in the identity store.
2. Confirm the suspension has replicated to every gateway region before reissuing.
3. Reissue the token and hand the new secret to the user over a verified channel.
4. Record the reissue against the ticket; a reissue without a ticket is an audit finding.

## Notes

The identity store is deployed in three regions. A token reissued before replication
completes is accepted in one region and rejected in another, which produces a sign-in loop
that looks like a device fault and is not one.
