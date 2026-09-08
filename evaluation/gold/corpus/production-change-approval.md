# Production Change Approval Policy

## Policy

No production change ships without a recorded risk assessment and an approval.
This policy governs configuration changes, releases, and infrastructure moves that
can affect live traffic.

## Change classes

| Class | Risk | Approval | Window |
|---|---|---|---|
| Standard | Low | Pre-approved runbook | Any business hour |
| Normal | Medium | Change advisory board | Daily 09:00–11:00 UTC |
| Emergency | High | Duty director | Anytime, post-hoc review within 24h |

## Requirements

- A change record must state the affected CI, the rollback plan, and the expected
  customer impact.
- Normal changes need at least two independent reviewers from the CAB.
- Emergency changes bypass forward approval but require a follow-up review within
  twenty-four hours; skip the follow-up only with duty-director sign-off.
- The change owner closes the record with the measured outcome and any incidents.

## Non-negotiable

If the change modifies authentication infrastructure, the MFA runbook must be
linked and the token store verified reachable before the window opens.
