# Globex VPN MFA Rebind Guide

## Scope

Globex tenant only. Acme's estate runs a different identity provider, so this guide does
not apply there and must not be served to an Acme caller.

## Procedure

1. Confirm the caller through the Globex service desk.
2. In the Globex identity portal, remove the enrolment bound to the retired handset.
3. Issue a fresh enrolment invitation against the Globex tenant realm.
4. Confirm one successful sign-in on the new handset.

## Note

Globex enrolments are held per tenant realm. A rebind performed in the shared console does
not reach the Globex realm and will appear to succeed while changing nothing.
