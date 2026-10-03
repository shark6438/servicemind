# User Password Reset and Account Unlock

## When to use this

An account is locked out after repeated sign-on failures, or a user has forgotten their
password and can complete identity verification.

## Procedure

1. Confirm the caller's identity against the service desk record before resetting anything.
2. Unlock the account only after the failed-attempt window has closed.
3. Issue a single-use reset link over a verified channel; do not read a password aloud.
4. Require the user to set a password that the platform does not already hold.
5. Confirm one successful sign-in before closing the ticket.

## What not to do

Never disclose whether a password matches a previous one, and never reset an account on the
strength of a ticket reference alone. Neither is a supported verification.
