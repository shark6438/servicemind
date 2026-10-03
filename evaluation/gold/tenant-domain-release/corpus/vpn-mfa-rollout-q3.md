# VPN MFA Phased Rollout (from 2026-06-01)

## Status

Approved and not yet in force. This plan takes effect on 2026-06-01 and describes how
enforcement will be extended. Until then the current enrolments stand.

## Plan

1. From 2026-06-01 every new remote-access account must enrol a second factor before its
   first sign-in.
2. From 2026-07-01 accounts that have not enrolled are moved to a challenged-only posture.
3. From 2026-08-01 the `mfa-exempt` group is removed from the directory entirely.

## Effect on existing procedures

Nothing changes for the device rebind runbook or the token store procedure. This plan adds
enrolment requirements; it does not alter how an existing enrolment is repaired.
