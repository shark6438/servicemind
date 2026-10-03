# VPN MFA Enrolment Audit Checklist

## Purpose

Routine checks that confirm the multi-factor enrolment store is in the state the platform
expects. This checklist states what to inspect and what a healthy result looks like. It
does not describe how to remedy any fault it uncovers.

## Checks

1. Every active user account has exactly one enrolment record.
2. No enrolment record references a device that has not signed in for 180 days.
3. No account is a member of a group that exempts it from a challenge.
4. The count of enrolment records matches the count held by the identity provider.
5. Every gateway region reports the same enrolment revision.

## Recording

Record the counts, the inspection time and the operator in the audit log. A check that
found something and a check that found nothing are recorded the same way; the remedy is
raised as a separate ticket.
