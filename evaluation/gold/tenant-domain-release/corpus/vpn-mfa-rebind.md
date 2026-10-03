# VPN MFA Device Rebind Runbook

## When to use this

A user's multi-factor authentication stopped working after they replaced their handset.
The authenticator application on the old device is still registered, so push approvals are
delivered to a device the user no longer holds and every sign-in attempt times out.

## Procedure

1. Verify the caller's identity through the service desk before changing any enrolment.
2. Open the identity console and locate the user's MFA enrolment record.
3. Remove the enrolment bound to the retired device.
4. Issue a fresh enrolment invitation and have the user complete it on the new handset.
5. Confirm one successful sign-in on the new device before closing the ticket.

## What not to do

Never disable multi-factor authentication to work around a failed enrolment. A disabled
MFA factor is a standing authentication bypass and must be treated as a security incident.

## Ownership

Identity Team owns enrolment records. Network Team owns gateway reachability.
