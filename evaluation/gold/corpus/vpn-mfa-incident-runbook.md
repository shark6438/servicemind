# VPN MFA Incident Runbook

## Purpose

Triaging remote-access incidents where VPN authentication or multi-factor
enrollment fails. This runbook is the primary reference for identity-provider and
token-store symptoms at the network edge.

## Symptom → cause

| Symptom | Likely cause | Owner |
|---|---|---|
| Token enrollment times out | Identity provider overloaded | Identity Team |
| One-time passcode rejected | Clock skew on the gateway | Network Team |
| Push approval never arrives | MFA application registration stale | Identity Team |

## Triage steps

1. Check identity-provider health and recent authentication changes.
2. Compare gateway clock against NTP; skew above 90 seconds blocks codes.
3. Inspect the MFA enrollment store for suspended token records.
4. If users are enrolled but codes still fail, reissue tokens in batches.
5. Record findings as an internal work note before changing production.

## Ownership

Network Team owns gateway connectivity faults. Identity Team owns token enrollment
faults and reissues. Escalate to the platform duty officer when both teams are needed.
