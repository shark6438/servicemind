# Order Preservation Fixture

This document is a structural golden fixture, not part of the answerable corpus. It
exists so parser and chunker tests can assert that section and block ordering is
preserved 100% from source to parents and children.

## First section

A single paragraph establishing the opening order marker.

- list item A
- list item B

## Second section

Paragraph two carries the middle marker.

### Nested subsection

Nested heading must remain under Second section, in order.

1. ordered item one
2. ordered item two

| Column one | Column two |
|---|---|
| r1c1 | r1c2 |
| r2c1 | r2c2 |

## Third section

Closing paragraph with the final marker. Order must never be reshuffled by
semantic chunking.
