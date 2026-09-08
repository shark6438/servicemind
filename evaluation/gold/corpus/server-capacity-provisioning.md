# Server Capacity and Provisioning Standard

## Overview

Defines how capacity is requested, sized, and provisioned for virtual servers, and
how utilization is monitored so teams scale before an outage rather than after.

## Request intake

| Field | Requirement |
|---|---|
| Workload profile | Peak CPU, memory, storage IOPS |
| Growth horizon | 12-month utilisation forecast |
| Owner | Named service owner with budget code |

## Sizing rules

- Provision for the 95th-percentile load, not the average.
- Keep headroom below 70% sustained CPU for interactive workloads.
- Storage tier must match the backup class in the database standard when the server
  hosts a database.

## Monitoring

The capacity dashboard alerts at 75% memory and 80% disk. The response to a capacity
alert is a change request, never an ad-hoc on-host resize outside the approval
policy.

## Closure

After provisioning, record the measured baseline in the server record and schedule a
quarterly review against the 12-month forecast.
