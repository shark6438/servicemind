# Storage capacity thresholds and response

Applies to block and object storage in the production estate. Owned by the Platform Team.

## Thresholds
A warning is raised at 70 percent used, a high-water alert at 80 percent, and a critical
alert at 90 percent. Alerts are evaluated every five minutes.

## Response by level
At warning, the owning team plans expansion within 30 days. At high-water, expansion is
scheduled within 7 days. At critical, expansion is performed within 24 hours and the
service owner is paged.

## Thin provisioning
Volumes are thin provisioned with a 20 percent reserve. The reserve is not usable and does
not appear in the utilisation figure.

## Growth review
Every volume growing more than 10 percent in 30 days is reviewed monthly by its owner.
