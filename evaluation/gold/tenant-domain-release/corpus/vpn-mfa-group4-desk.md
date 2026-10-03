# Service Desk VPN MFA Rebind Script

## Restriction

Service Desk internal. Contains the caller-verification script and must not be readable
outside the Service Desk group.

## When to use this

A caller reports that VPN multi-factor authentication fails after changing phones and the
Service Desk is authorised to complete the rebind without escalating to the Identity Team.

## Script

1. Confirm the caller's employee number and one recent ticket reference.
2. Ask the caller to attempt a sign-in and read back the failure message.
3. Where the message is a timed-out push, complete the rebind in the identity console.
4. Where the message is an expired token, do not rebind; transfer to the Identity Team.
5. Record the rebind in the ticket and confirm a successful sign-in before closing.

## Escalation

Identity Team owns token-store faults. Network Team owns gateway reachability.
