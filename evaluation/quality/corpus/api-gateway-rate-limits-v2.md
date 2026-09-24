# Public API gateway rate limits (revision 2)

Supersedes revision 1. These are the limits in force.

## Limits
A client is limited to 600 requests per minute per API key, and 6000 requests per minute
per tenant across all keys.

## Burst
A burst of up to 1200 requests is absorbed over a 10 second window. A burst never raises
the per-minute ceiling.

## Rejection
A rejected request returns HTTP 429 with a Retry-After header in seconds.

## Increases
An increase needs a written capacity case and is reviewed monthly.
