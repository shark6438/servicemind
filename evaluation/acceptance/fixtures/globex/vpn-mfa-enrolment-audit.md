# VPN MFA enrolment audit checklist

## Scope

This checklist is used when an auditor, rather than a service desk analyst, has to confirm
that a population of VPN enrolments is in a known state. It describes how to sample, what
to record, and how to report. It is a bookkeeping procedure. It does not diagnose any
single user's failure and nothing in it is a remedy for one: it says how to *observe*
enrolments, never why one stopped working.

Two rules follow from that and they govern everything below. First, a count is a statement
about a population and never about an account. Second, an observation that has not been
reproduced is not recorded as an observation. An audit that drifts into either is no longer
producing the thing it was commissioned to produce.

## Sampling

1. Draw the population from the identity console, not from the VPN concentrator. The
   concentrator records sessions, and a session that never completed leaves no trace
   there; the console records enrolments, which exist whether or not anyone connected.
2. Sample thirty accounts per service line, or the whole service line where it holds
   fewer than thirty. Record the sample frame, the date it was drawn, and the console
   export it came from.
3. Where a service line spans more than one region, draw the sample per region. A
   regional cap on enrolment age will not appear in a global sample, because the regions
   with the largest populations decide the average.
4. Re-draw the sample each quarter. Carrying a sample forward across quarters measures
   the enrolments that were in the frame when it was drawn, which is not the question.
5. Where a service line has changed ownership mid-quarter, draw the frame against the
   owner recorded on the first day of the quarter and note the ownership change in the
   report. A frame drawn against a moving roster cannot be reproduced.
6. Where an account appears in two service lines, count it once in each frame and say so.
   De-duplicating across frames silently reduces the population and makes the two service
   lines' numbers add up to less than the whole.
7. Keep the frame for four quarters. A finding whose frame has been discarded cannot be
   re-checked, and a finding that cannot be re-checked is an opinion.

## Fields to record

| Field | Source | Why it is recorded |
|---|---|---|
| Account identifier | Identity console | Joins the sample to the service line roster |
| Enrolment date | Identity console | Ages the enrolment without reference to any incident |
| Registered devices | Identity console | Count per account, no device identifiers retained |
| Last successful challenge | Concentrator log | Date only; a date proves nothing about a mechanism |
| Directory lockout state | Directory | Separates an account-state question from an enrolment question |

Quantities are recorded for the population. The audit does not retain device identifiers,
serial numbers or challenge contents: those belong to the account holder and are not
needed to count enrolments.

## Frequency and ownership

The audit runs quarterly and is owned by the service line, not by the security function.
The security function sets the fields and reads the roll-up; it does not draw the sample.
Where the two disagree about a field's meaning, the field is re-specified before the next
run rather than re-interpreted during the current one.

An owner who cannot draw the sample reports that fact. A missed quarter is recorded as a
missed quarter. Substituting the previous quarter's numbers fills the table and destroys
the series, which is the only thing the table was for.

## Reporting

- Report counts and ages. Do not report inference.
- A field that could not be read is reported as unread, not as zero. An unread enrolment
  and an enrolment of age zero are different findings, and the second one is rare.
- Findings are addressed to the service line owner. The audit does not open tickets, and
  it does not close them.
- Where the sample frame itself is wrong, report the frame and stop. A finding drawn from
  a frame that cannot be reproduced is not a finding an owner can act on.

## Boundaries

An audit is not an incident review, and the two have different evidence rules. The audit
answers "how many enrolments look like this"; an incident review answers "what happened to
this account". Running the checklist against a single failed connection produces a sample
of one, which is not a measurement.

This checklist does not, and must not, recommend changing any account's authentication
configuration. Enrolment counts describe a population; they are not a basis for altering
an individual account's factors, and an audit that ends in a configuration change has
stopped being an audit.

## Appendix A — enrolment age bands

Ages are reported in bands because the underlying dates are recorded to the day and a
day-level average invites arithmetic that the data does not support.

| Band | Age of enrolment | What the band is for |
|---|---|---|
| A | Under 30 days | Recently enrolled; the population most likely to be re-checked soon |
| B | 30 to 90 days | Settled; the band a quarterly comparison usually moves |
| C | 90 to 365 days | Aged; where a device-replacement pattern would first become visible as a count |
| D | Over 365 days | Long-lived; reported separately so the other three bands stay readable |

Bands are assigned from the enrolment date alone. The band a row falls into says nothing
about whether the enrolment works: an enrolment in band A is not "newer and therefore
better" and one in band D is not "older and therefore worse". The bands exist to make a
distribution legible, and a distribution is not a diagnosis.

Where a band's population is smaller than five, report the band as "fewer than five"
rather than the exact count. Small cells identify individuals when combined with a roster,
and the audit does not need that resolution to answer its question.

## Appendix B — retention and access

Enrolment exports are retained for four quarters and are readable by the service line
owner and the security function. They are not attached to tickets, they are not sent by
mail, and they are not placed in shared drives that outlive the quarter. An export that
has been attached to a ticket has left the audit's retention rules and now follows the
ticket's, which are different and longer.

Access is reviewed at the same time the sample is drawn, so that the list of who can read
the export is refreshed as often as the export itself. A reader list that is reviewed once
and then inherited is a reader list that only grows.

## Appendix C — exceptions

An exception is any sampled account whose fields could not be read in full. Exceptions are
recorded in a separate table with the reason, and they are never folded into the counts:
folding an unread enrolment into the "band A" row makes the band a mixture of a
measurement and a gap.

| Exception | Recorded as | Not recorded as |
|---|---|---|
| Console export truncated | Truncated, with the last account read | A shorter population |
| Field absent from the export | Field unread, with the export name | A zero |
| Account missing from the roster | Unmatched, with the account identifier | Excluded |
| Export older than the quarter | Stale, with its date | A current quarter |

An exception rate above one in ten is reported to the service line owner before the
quarter's roll-up is published. At that rate the roll-up describes the export rather than
the population, and publishing it would put a number in front of an owner that the audit
cannot stand behind.

## Appendix D — glossary

- **Enrolment**: the record that an account has registered a factor. It exists
  independently of any connection attempt.
- **Challenge**: the step at which a registered factor is exercised. A challenge that is
  attempted and not completed is a session event, not an enrolment change.
- **Frame**: the population a sample was drawn from, with its date and export name.
- **Band**: an age grouping, assigned from the enrolment date alone.
- **Exception**: a sampled account that could not be read in full.
- **Roll-up**: the quarterly summary sent to service line owners. It contains counts,
  ages, bands and exception rates, and no inference of any kind.
