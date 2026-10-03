# Globex Citrix Break-Glass Procedure

## Scope

Globex tenant only, and restricted to the Globex platform group. This document describes
how the Globex estate regains access to its virtual desktop platform when the broker is
unavailable. It is not a template, not a shared standard, and does not apply to any other
tenant.

## Procedure

1. Raise a major incident in the Globex estate before changing broker configuration.
2. Request the break-glass credential from the Globex sealed store; two approvers required.
3. Reconnect the broker through the Globex out-of-band network.
4. Rotate the break-glass credential before returning it to the store.

## Why this is tenant-scoped

The Globex estate is administered separately from every other tenant's. A procedure that
reaches its credential store grants access to Globex systems and nothing else, so it is
neither useful nor safe outside the Globex platform group.
