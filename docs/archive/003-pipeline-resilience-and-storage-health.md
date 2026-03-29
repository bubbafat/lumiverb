> **Archived 2026-03-29** — Obsolete: built around removed worker queue/pipeline model, never implemented

# ADR-003: Pipeline Resilience and Storage Health

## Status
Proposed

## Context

During the first remote worker deployment (client on macOS, server on DigitalOcean VPS), we hit two failure modes that would also occur in production:

1. **Race condition**: Downstream workers (vision, embed) were enqueued while proxy uploads were still in progress. They tried to read proxies that didn't exist on the server yet, causing mass failures.

2. **Silent infrastructure failure**: If block storage goes offline or is unmounted, the artifact API returns `artifact_missing` for every read. The pipeline churns through jobs marking them as permanently failed, with no systemic signal to the operator. By the time anyone notices, thousands of jobs need manual re-enqueue.

Both cases share a root cause: the pipeline has no concept of infrastructure health and treats all artifact read failures as job-level problems.

## Problems to Solve

### Storage health awareness
- The server has no health probe for the data directory. If block storage is unavailable, `/health` still returns 200 and the pipeline keeps accepting and failing work.
- There is no admin-visible signal (in status, UI, or alerts) that storage is degraded.

### Job failure classification
- A missing proxy due to a race condition (proxy not uploaded yet) is treated the same as a missing proxy due to a bug (proxy was never generated). The former is retryable; the latter is not.
- `artifact_missing` from a storage outage marks jobs as permanently failed/blocked. Recovery requires manual re-enqueue of potentially thousands of jobs.

### Operator visibility
- `lumiverb status` shows per-stage failure counts but doesn't surface the *reason* for failures. "50 vision jobs failed" doesn't tell you it's all the same `artifact_missing` error.
- No alerting mechanism exists. An admin only discovers problems by manually checking.

## Proposed Solutions (to be refined)

### 1. Storage health check
- Add a storage read/write probe to `/health` (or a new `/health/storage` endpoint).
- If storage is unhealthy, the pipeline lock acquisition should fail or warn.
- The web UI settings page could show system health.

### 2. Pipeline circuit breaker
- If N consecutive jobs fail with the same infrastructure-level error (e.g. `artifact_missing`), pause the pipeline stage rather than continuing to burn through the queue.
- Resume automatically once the health check passes.

### 3. Retryable vs permanent failures
- Distinguish between `blocked` (permanent, never retry — e.g. wrong media type) and `retryable` (transient, try again later — e.g. artifact not yet available).
- Workers should return jobs to pending with a backoff delay for transient errors, not mark them as failed.

### 4. Failure aggregation in status
- `lumiverb status` and the web UI should group failures by error type, not just count them. "47 vision jobs failed: artifact_missing" is actionable; "47 vision jobs failed" is not.

### 5. Admin alerts
- Email or webhook notification when a pipeline stage exceeds a failure threshold.
- Not in v1 scope, but the health check infrastructure makes it straightforward to add later.

## Decision

Deferred — this ADR captures the design space. Implementation priority TBD after the current deployment stabilizes.

## Consequences

Until this is implemented:
- Operators must monitor `lumiverb status` manually.
- Storage outages will cause silent mass job failures requiring manual re-enqueue.
- Race conditions between proxy upload and downstream workers can be avoided by running pipeline stages sequentially (proxy first, then the rest).
