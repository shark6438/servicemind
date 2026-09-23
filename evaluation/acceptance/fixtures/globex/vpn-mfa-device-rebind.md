# VPN MFA device rebind after a handset change

## Scope

Applies to any Globex employee who authenticates to the corporate VPN with multi-factor
authentication. It covers the case where the password is accepted and the *second* factor
is rejected, which is the signature of a factor that is bound to a device the user no
longer carries.

## Symptoms

- The VPN client accepts the password and then fails the challenge step.
- The failure repeats on every attempt, from every network, with the same password.
- The user reports replacing, resetting, or factory-wiping a phone in the last 30 days.
- No lockout is recorded against the account; the account itself is healthy.

A password that is accepted proves the credential is intact. It says nothing about the
factor, and it is the factor that is failing.

## Diagnosis

Multi-factor authentication is bound to a registered device secret. The secret lives on
the device, not in the directory, so replacing the phone does not migrate it: the
directory still believes the old handset will answer. Nothing in the account needs to
change, and no directory attribute is wrong.

Confirm the three facts in the ticket before acting: the password step succeeded, the
challenge step failed, and the user's handset changed recently. If the password step is
also failing, this is not the fault described here and the account-recovery procedure
applies instead.

## Remedy

1. Ask the user to have the new handset with them and to be signed in to the corporate
   account on it.
2. Verify the user's identity out of band before touching the factor; a factor rebind is
   an authentication change and is treated as one.
3. In the identity console, remove the stale device registration for the user.
4. Have the user re-enrol the new handset and confirm the challenge succeeds once.
5. Ask the user to connect to the VPN and confirm the connection completes end to end.

The rebind is complete when a full connection succeeds, not when the registration is
recorded. A registration that has been recorded but never exercised has not been shown
to work.

## What not to do

Do not disable multi-factor authentication for the user, not even temporarily, and do not
move them to a policy that skips the challenge. "Turning it off to unblock them" removes
the control rather than repairing it, leaves the account with a single factor, and is not
a remedy for a stale device secret -- the underlying fault is untouched and returns the
moment the control is restored.

Do not reset the password. The password was accepted; resetting it does not address the
factor and destroys a working credential.
