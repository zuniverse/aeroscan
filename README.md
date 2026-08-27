# Drone Data Ingestion Pipeline

Ingestion API for drone infrastructure inspection: a drone registers a run and its
file manifest, uploads frames straight to S3, confirms them in batches, then declares
the run complete. The API reconciles what was promised against what arrived and exposes
the result to the web app.

Design note: [DESIGN.md](DESIGN.md).

## Running it

```bash
docker compose up -d --build --wait     # API, Postgres, MinIO, bucket creation
docker compose exec api python -m scripts.seed
curl -s localhost:8000/health
```

The seed prints the site ids and API keys it created. Interactive docs on
`localhost:8000/docs`, MinIO console on `localhost:9001` (`minioadmin` / `minioadmin`).

```bash
docker compose exec api pytest -q       # 9 tests, on a dedicated database
```

Migrations run at container start in development. In production they do not; see below.

### A run, end to end

```bash
MK='X-API-Key: dev-key-SB-0001'
JSON='Content-Type: application/json'

RUN=$(curl -s -X POST localhost:8000/v1/runs -H "$MK" -H "$JSON" -d '{
  "drone_run_uid": "demo-001",
  "started_at": "2026-07-25T09:00:00Z",
  "operator": "a.moreau",
  "files": [{"key":"frame_00001.jpg","size_bytes":2200000},
            {"key":"frame_00002.jpg","size_bytes":2200000}]}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['run_id'])")

# Confirm one of the two, then complete: the run comes back INCOMPLETE
# with the missing key named.
curl -s -X POST localhost:8000/v1/runs/$RUN/file-confirmations -H "$MK" -H "$JSON" \
  -d '{"files":[{"key":"frame_00001.jpg"}]}'

curl -s -X POST localhost:8000/v1/runs/$RUN/completion -H "$MK" -H "$JSON" \
  -d '{"defect_detected":true,"defect_count":42,"confidence_score":0.871}'

curl -s 'localhost:8000/v1/runs?defect=true&limit=5' \
  -H 'X-API-Key: dev-backoffice-key'
```

## Review guide

Three files carry the decisions. In order:

1. **`app/routers/runs.py`** - everything a drone calls. Idempotent creation, batched
   confirmations, and the reconciliation in `complete_run`, which is where the two
   competing retry rules meet.
2. **`app/models.py`** - the schema, the two denormalisations, and the partial index the
   list query is built around.
3. **`app/routers/queries.py`** - the list endpoint with keyset pagination, and the
   abandoned-run sweep with its two thresholds.

`tests/test_reconciliation.py` is the shortest way to see what the system guarantees.

## Design decisions and trade-offs

- **Idempotency lives in the database, not in the application.** Run creation is one
  `INSERT ... ON CONFLICT DO NOTHING ... RETURNING`, keyed on a drone-generated
  `drone_run_uid`. Check-then-insert would leave a window where two concurrent retries
  both create a run.
- **Confirmations are an `UPDATE` of pre-existing manifest rows, not an upsert.** One row
  per expected file is written at creation, so completion can name the missing keys
  rather than only count them, and a key the run never declared cannot create a row.
  Retry safety comes from `confirmed_at IS NULL` in the `WHERE`: a replayed batch matches
  nothing and moves no counter.
- **`INCOMPLETE` is transitory, not terminal.** Re-flying an inspection means a pilot
  back on site and a weather window, and the structure may have changed since, so files
  lost to a dropped link are uploaded later and the run promoted to `COMPLETED`.
  Inspection results, on the other hand, are written once by the first completion and
  never overwritten: they feed maintenance decisions on physical assets, and nothing
  could arbitrate between two contradictory readings after the fact.
- **Two sweep thresholds rather than one.** A silent `UPLOADING` run has probably lost its
  drone mid-transfer (hours). An `INCOMPLETE` run already has its results recorded and
  may be waiting on a crew travelling back to the site (days).
- **The site belongs to the mission, not to the aircraft.** A drone inspects a viaduct
  one day and a wind farm the next, so there is no standing drone-to-site link: the site
  is stated on run creation and frozen. That removes a join from the hot list query, and
  it makes `site_id` a point-in-time fact rather than a reference something could rewrite
  later. Inspection results live on `runs` too, since the relation is one-to-one and
  written once.
- **Index and query, measured.** `idx_runs_defective` is partial on
  `defect_detected` and leads with site, then `started_at`, then confidence. On
  500 000 runs the first page takes 2.3 ms. The plan does include a top-N heapsort,
  because a range predicate on `started_at` stops the same index from also delivering
  rows pre-sorted by confidence; sorting the few hundred rows in the window is free. The
  alternative index, leading with confidence to avoid the sort, scanned 5 000 rows to
  discard 4 950 and measured eight times slower. Pagination is keyset: page 100 is 1.4 ms
  against 48 ms with `OFFSET`, and Postgres folds the cursor predicate into the index
  condition, so deeper pages scan less rather than more.
- **A fifth endpoint, `upload-urls`.** Signing all 18 000 URLs at creation would mean
  around 7 MB of JSON against MinIO and closer to 12 MB against S3, most of it expired
  before the drone got there. A single presigned POST policy scoped to the run prefix
  would keep the count at four, at the cost of a `multipart/form-data` client.
- **The bucket is the source of truth, not the drone.** Confirmations arrive
  over hours and are only a claim: the API never sees the bytes, so a drone
  can report a file it failed to upload, and a run marked `COMPLETED` on that
  basis would report an asset as inspected when part of it was never seen.
  Completion reconciles
  against one `ListObjectsV2` over the run prefix - around 18 paginated calls,
  seconds against hours of upload, with no queue or worker to operate. That
  leaves confirmations doing what they are good for: a progress signal feeding
  `last_activity_at`. It also separates two faults their count conflates, since
  files never confirmed mean an unfinished upload while files confirmed but
  absent mean a drone whose view of reality is wrong. S3 event notifications
  would do the same continuously and catch a failure mid-run rather than at the
  end, which is worth the infrastructure once early detection is the
  requirement.
- **Synchronous SQLAlchemy.** Every endpoint is database-bound, so async would add
  greenlet plumbing and harder tests without moving the bottleneck.
- **SHA-256 for API keys, not bcrypt.** Slow digests exist to frustrate brute force against
  low-entropy human passwords; a 32-byte random key is not brute-forceable, and a
  per-call salt would make the indexed lookup impossible. Drone keys and the backoffice
  key are separate credentials: a drone writes only its own runs, the web app reads
  every site.

Roughly 750 lines of application code, plus tests and migrations.

## If I had more time

- **Collapse terminal runs.** `run_files` is what breaks first: 200 drones at 5 runs a
  day is around 18 M rows a day. Once a run is reconciled the per-file detail has no
  value, so it should become an aggregate (count plus the missing keys), with monthly
  partitioning underneath.
- **Continuous verification.** S3 event notifications into a queue would flag a
  failed upload mid-run instead of at completion, at the cost of ~18 000 events
  per run.
- **A real sweep schedule.** Today it is an endpoint; it wants a scheduled job with a lock
  so two instances cannot sweep at once.
- **Sweep on progress, not silence.** Comparing `confirmed_file_count` between
  two sweeps would detect absence of progress rather than absence of traffic,
  which is more robust than the idle threshold on very slow links.
- **Observability.** Structured logs with a run id on every line, and metrics on
  confirmation lag and the incomplete rate per drone, which is what would surface a
  failing drone before a human notices.
- **Key rotation and per-drone rate limiting**, neither of which the current single-key
  scheme supports.
- **`requirements-dev.txt`**, so pytest stops shipping in the runtime image.
- **An audit trail of status transitions.** A run currently carries only its
  current status: nothing records when it became `ABANDONED`, or whether a
  sweep or the drone put it there. An append-only `run_status_transitions`
  table written in the same transaction as each change would fix that, and is
  hard to argue against on regulated data. Distinct from full event sourcing,
  which would force the product query onto a materialised projection and move
  idempotency out of the unique constraint.

## Production notes

- **Shape.** ALB into ECS Fargate across two private subnets, RDS Postgres (Multi-AZ) and
  S3 reached through VPC endpoints so drone traffic and database traffic never cross the
  public internet. Drones hit the ALB directly; images go to S3 without touching the API.
- **Storage.** One bucket, versioning on, lifecycle to Infrequent Access at 30 days and
  Glacier at 180. A rule expiring incomplete multipart uploads, which are billed while
  invisible. Presigned PUTs mean the API never proxies 40 GB per run, which is most of the
  cost saving.
- **Secrets.** Secrets Manager, injected as ECS task secrets rather than image layers or
  environment files. Database credentials rotated automatically; drone API keys stored
  as SHA-256 digests in the database, so a database dump does not yield usable keys.
- **Migrations.** A separate one-off ECS task running `alembic upgrade head` before the
  service deploys, never on container start as it is here. Migrations stay
  backward-compatible for one release so old and new tasks can run side by side during a
  rollout.
- **Rollback.** ECS keeps the previous task definition, so application rollback is one
  update and a couple of minutes. Schema rollback is not symmetric: expand-and-contract
  (add nullable, backfill, switch reads, drop later) means a bad deploy can roll back
  without a `downgrade`, which is the only rollback story that survives contact with
  production data.
- **Scaling.** Target-tracking on ALB request count. The API is stateless; the first real
  ceiling is `run_files` write volume, which is why collapsing terminal runs is the first
  item above.

## AI use

I used Claude throughout, mostly as a reviewer and a rubber duck rather than a code
generator. Design decisions were argued out before any code existed: the manifest versus
liveness split, whether `INCOMPLETE` should be terminal, and the trust model around
client-declared confirmations all came from that.

What I reworked or rejected:

- The first design note claimed the index avoided a sort node. It does not, and I only
  found out by running `EXPLAIN ANALYZE` against a 500 000 row table. I kept the index and
  rewrote the justification around measured numbers.
- A size estimate for presigned URL payloads was quoted at 12 MB, then "corrected" to 8 MB
  from a local MinIO measurement. Both were partly wrong: MinIO URLs are shorter than S3
  ones, so the README now gives both figures with the assumption attached.
- A batch confirmation where every row omitted `size_bytes` produced an untyped `NULL`
  column that Postgres typed as `text` and refused to assign to `bigint`. The generated
  tests had never exercised that path because they always sent sizes;
  `test_replayed_confirmations_do_not_double_count` now does.
- Alembic's `env.py` originally set the URL through `config.set_main_option`, which routes
  it through `configparser` and breaks on any password containing `%`. It now builds the
  engine directly.
