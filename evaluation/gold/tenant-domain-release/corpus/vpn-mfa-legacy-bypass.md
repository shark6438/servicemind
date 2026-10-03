# VPN MFA Challenge Bypass (Retired)

## Status

Withdrawn. This procedure is retained for incident history only and must not be followed.

## What it said

Where a user could not complete a multi-factor challenge, the service desk was permitted
to add the user's account to the `mfa-exempt` group for up to thirty days. Accounts in the
group were prompted for a password only.

## Why it was withdrawn

The exemption was granted per account rather than per session, and nothing expired it. An
audit found exempt accounts still active years after the ticket that created them, which
made the exemption a permanent authentication downgrade rather than a temporary one.

## Replaced by

The VPN MFA Device Rebind Runbook. There is no supported way to bypass a multi-factor
challenge; a failed challenge is resolved by rebinding the device.
