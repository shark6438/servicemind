# Incident Impact and Urgency Matrix

## Rule

Priority is derived from impact and urgency. Never guess priority from the title of
a ticket; score both dimensions first.

## Matrix

| Urgency \ Impact | High impact | Medium impact | Low impact |
|---|---|---|---|
| High urgency | P1 | P2 | P3 |
| Medium urgency | P2 | P3 | P4 |
| Low urgency | P3 | P4 | P4 |

## Definitions

- **Impact**: how many users or revenue processes the fault touches.
- **Urgency**: how quickly business impact grows if the fault is not resolved.
- **Major incident**: P1 requires broad service impact *and* evidence of a growing
  outage, then human escalation for review.

## Guidance

A high-urgency fault with medium impact normally maps to P2. Escalate a P1 only with
demonstrated broad service impact; escalating without evidence causes noise and
missed review gates.
