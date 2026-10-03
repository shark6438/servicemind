# Network Team Account Recovery Runbook

## Restriction

Network Team internal. Covers privileged service accounts and must not be readable
outside the team's group.

## When to use this

A privileged account used for gateway or identity administration is locked out and the
network team needs it restored without waiting for the next service desk shift.

## Procedure

1. Raise a major incident if the locked account holds an emergency credential.
2. Two team members must approve the recovery in the incident record.
3. Recover the account through the out-of-band management path only.
4. Rotate every credential the account held before returning it to service.
5. Record the recovery, the two approvals and the rotation in the incident.

## Escalation

Ordinary user accounts are the Service Desk's, not this runbook's.
