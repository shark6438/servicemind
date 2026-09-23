# Service Desk runbook: VPN MFA rebind on behalf of a user

## Audience

This runbook is restricted to the Globex Service Desk. It documents the assisted rebind,
in which a desk agent performs the registration steps for a user who cannot do them
unaided.

## When this applies

Use this runbook when the user has already confirmed the three facts of a stale device
secret -- password accepted, challenge failed, handset recently changed -- but cannot
complete self-service enrolment because they are on a phone call with the desk and have
no second screen, or because their handset cannot reach the enrolment endpoint.

## Procedure

1. Read back the three facts with the user and record the user's confirmation in the
   ticket. An assisted rebind is an authentication change and is never done on an
   unverified request.
2. Identify the user out of band using the desk's standard voice verification. Do not
   accept the incident number alone as proof of identity.
3. Remove the stale device registration for the account.
4. Walk the user through re-enrolling the handset, one step at a time, waiting for the
   user to confirm each screen before moving on.
5. Have the user connect to the VPN while still on the call, and confirm the connection
   completes. If it does not, escalate rather than repeating the rebind -- a second
   rebind against a still-failing factor hides the real fault.

## Limits

The desk may not disable multi-factor authentication, and may not move the account to a
policy that skips the challenge, as a way to close a ticket. The desk may not issue a
break-glass override; that requires network operations approval and is documented in the
Network Team runbook.
