# Network Team VPN MFA Break-Glass Runbook

## Restriction

Network Team internal. This runbook describes emergency credentials and must not be
readable outside the team's group.

## When to use this

The identity provider is unreachable and the network team needs to regain administrative
access to gateway equipment to restore remote connectivity for the whole tenant.

## Procedure

1. Raise a major incident before touching gateway configuration.
2. Request the break-glass credential from the sealed store; two team members must approve.
3. Authenticate to the gateway from the out-of-band management network only.
4. Restore the identity provider route, then sign in again over the normal path.
5. Return the break-glass credential to the sealed store and rotate it immediately.

## Ownership

Network Team duty officer owns the decision to invoke break-glass.
