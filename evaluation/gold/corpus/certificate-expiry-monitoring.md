# TLS Certificate Expiry Monitoring

## Scope

Automatic monitoring of TLS certificates for public endpoints, internal services,
and the VPN gateway. The goal is to renew certificates before expiry alerts reach
users.

## Monitoring rules

| Certificate | Alert at | Renew by |
|---|---|---|
| Public edge | 30 days | 14 days before expiry |
| Internal service | 21 days | 7 days before expiry |
| VPN gateway | 45 days | 21 days before expiry |

## Renewal

- Renew through the certificate automation; a manual renewal is a normal change.
- After renewal, verify the new leaf on the live endpoint and in the trust chain.
- Link the renewal to the owning service record so expiry alerts stop cleanly.

## Escalation

If an expiry alert is older than five days, escalate to the platform duty officer.
A certificate that expires is a P1: clients fail closed and the affected service
declares an outage.
