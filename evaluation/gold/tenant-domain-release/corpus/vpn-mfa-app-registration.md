# VPN MFA Application Registration

## Symptom

Users are prompted for a multi-factor challenge repeatedly even though their registered
token is valid and the handset has not changed. Approvals appear on the handset and are
accepted, and the next sign-in prompts again.

## Cause

The application registration that binds the VPN client to the identity provider has
expired. The provider treats each sign-in as coming from an unknown application, so the
challenge is never recorded as satisfied for that session.

## Remedy

1. Confirm the symptom: repeated prompts with a valid token and an unchanged handset.
2. Re-register the VPN client application against the identity provider.
3. Have the user sign in once and confirm the challenge is not repeated.

## Distinguishing this from a device fault

A device fault produces a challenge that is never delivered, so the user sees a timeout.
This fault produces a challenge that is delivered and accepted, and then asked for again.
Rebinding the device does not fix it.
