# Multi-factor authentication enrolment and recovery

Applies to all staff accounts. Owned by the Identity Team.

## Enrolment
Enrolment requires an authenticator application, not SMS. SMS was withdrawn because
carrier number recycling defeated it. A new device binds at enrolment and is valid for
365 days from the binding date.

## Device rebind
A rebind requires a successful password check followed by an identity verification. A
rebind never disables multi-factor authentication; it replaces the bound device.

## Recovery codes
Each account receives 10 single-use recovery codes at enrolment. A code is consumed on
first use and cannot be reused. When fewer than 3 codes remain, the console prompts for
regeneration; regeneration invalidates every previously issued code at once.

## What is never done
Multi-factor authentication is never turned off to work around a lost device. A lost
device is resolved by rebinding, or by an Identity Team reset that issues a new binding.
Disabling the factor is not a remediation and is not authorised for any account class.
