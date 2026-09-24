# Data classification standard (revision 2)

Supersedes revision 1. This is the standard in force.

## Levels
Public, Internal, Confidential, Restricted. A dataset carries exactly one level.

## Handling
Confidential data is encrypted at rest and in transit, and is never copied to a personal
device. Restricted data adds a named owner and an access review every quarter.

## Labelling
Every document carries its level in the header. An unlabelled document is treated as
Confidential by default.

## Sharing
Confidential data is shared only inside the tenant. Restricted data is shared only with
named individuals.
