# VPN connection fails after an application or policy update

## Scope

Applies when a user reports that the VPN *will not connect* and the failure is not a
challenge failure. The symptom -- "VPN is broken" -- is the same sentence users write for
both faults, so the two must be separated before anything is changed.

## Symptoms

- The VPN client rejects the connection before any authentication prompt appears.
- Earlier in the same session the client reported a policy, profile, or client-version
  mismatch, or the user was asked to update the client and did so.
- The user's handset has not changed.
- Other users on the same network report the same failure, or the user fails from every
  network including a hotspot.

## Why this is not a factor rebind

If the connection is rejected before the challenge is presented, the multi-factor step
was never reached. Rebinding the factor cannot fix a fault that occurs ahead of it, and
performing the rebind anyway is a change made against a fault that was never diagnosed.
The distinguishing observation is *where* the failure happens: before the prompt is a
client or policy fault, at the prompt is a credential fault, after the prompt is a factor
fault.

## Remedy

1. Capture the exact client error text and the client version. The text names the fault
   more precisely than the user's summary does.
2. Compare the client's profile against the current published profile; a stale profile is
   the most common cause of a pre-prompt rejection.
3. If the client is out of support, have the user update it and retry.
4. If the rejection persists with a current client and a current profile, and other users
   on the same network are affected, raise a network incident rather than continuing with
   per-user changes.

## Escalation

A failure that affects every user on a network is an outage, not a ticket. Escalate it
and stop making per-account changes: per-account changes during an outage produce
untraceable state and do not address the shared cause.
