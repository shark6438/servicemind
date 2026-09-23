# Network Team runbook: VPN MFA break-glass rebind

## Audience

This runbook is restricted to the Globex Network Team. It documents the break-glass
variant of the standard rebind, which requires a network operations approval that the
service desk cannot grant.

## When this applies

Use this runbook only when the standard rebind cannot be completed because the user's
enrolment channel is itself unavailable -- the identity console is reachable but the
self-service enrolment endpoint is not, and the user cannot complete enrolment on their
own handset.

## Procedure

1. Open a network operations approval request naming the affected account and the
   incident number. Break-glass rebinds are never performed on a verbal request.
2. Obtain the approval from the on-call network operations engineer. Record the approver
   and the approval reference in the ticket.
3. On the VPN concentrator, issue a temporary per-user override that accepts the new
   handset secret for the account.
4. Have the user re-enrol and connect while the override is in place.
5. Remove the override as soon as the connection succeeds. An override left in place is
   an open door, and the ticket is not closed until it is removed.

## Limits

The override is scoped to one account and one session. It must not be widened to a
group, a subnet, or a policy. If the standard rebind can be completed, do that instead:
break-glass exists for the case where the standard path is itself broken, and using it
otherwise spends an approval that the next real outage will need.
