# Deploy and downtime

Loaded at step 13, when the project declares `deploy` in its manifest.

## Downtime tolerance follows the phase

The phase already decides merge gate, prod deploy and model (DESIGN.md §3.6).
Downtime is the same axis: how much does it cost when the service is gone for a moment.

| Phase | Tolerated downtime | What that means in practice |
|---|---|---|
| `idea` | unlimited | Stop it, change it, start it. Nobody notices. |
| `build` | minutes | `Recreate` is fine. Do not spend effort on rolling updates. |
| `launch` | seconds | Prefer a rolling update. A minute of downtime is a bad first impression, not an outage. |
| `live` | none | Zero-downtime required. A deploy that drops requests is a bug, not a deploy. |

Read the phase, then pick the cheapest strategy that fits. In `build`, a rolling update is
wasted work; in `live`, `Recreate` is a defect.

## Downtime is not the only constraint

Some services cannot run two instances at all, whatever the phase. `board` is one: one SQLite
file, one writer. Two pods means two writers on the same file, and that is data corruption, not
a slower deploy. DESIGN.md §10 says it outright: *do not scale to 2 replicas*.

So the order is:

1. Can this service run two instances at once? No → `Recreate`, and downtime is the price.
2. Yes → let the phase decide whether the rolling update is worth building.

Never trade correctness for uptime. A short gap is visible and recoverable; two writers on one
database is neither.

## After deploying

`board task deploy $T` runs the manifest command and records `task.deployed`. `done` is refused
without it. If the service crash-loops, create a P0 rollback task and take it FIRST — put the
rollback command in the task spec so it is written down before you need it.
