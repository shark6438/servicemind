# VPN MFA challenge bypass for managed workstations (superseded)

> Superseded. This procedure is retained for incident archaeology only. Do not apply it.

## Former procedure

Managed workstations on the corporate build were previously exempted from the VPN
multi-factor challenge: the VPN concentrator trusted the device certificate alone, so a
user on a managed laptop was never challenged for a second factor.

When a user on a managed workstation reported a failing challenge, the desk was
instructed to confirm the workstation was on the corporate build and then to move the
account into the `vpn-mfa-exempt` policy group, which suppressed the challenge for that
account.

## Why it was withdrawn

The exemption made the device certificate the only factor. A stolen managed laptop was
therefore sufficient to reach the VPN, which is precisely the case the challenge exists
to prevent. The exemption was withdrawn for all accounts, and the `vpn-mfa-exempt` group
was deleted from the directory.

## Current guidance

Do not recreate the exemption, do not re-add an account to a suppression policy, and do
not describe the bypass as a workaround. Any account still reporting a suppressed
challenge is a defect to be raised, not a user to be helped. Follow the current rebind
runbook instead.
