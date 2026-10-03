# VPN MFA Token Store Procedure (v1)

## Status

Superseded on 2026-04-01. Retained because tickets raised before that date refer to it.

## Reissue procedure

1. Suspend the user's token record in the identity store.
2. Wait for the suspension to replicate to every gateway.
3. Reissue the token and hand the new secret to the user over a verified channel.
4. Reissue interval: a token is reissued at most once every 24 hours.

## Notes

The 24-hour interval was chosen when the identity store was single-region. It is no longer
accurate for the current deployment.
