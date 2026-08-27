# bucket-archiver

Rolls many small S3 objects into few large tarballs in Glacier Deep Archive,
then deletes the originals.

```bash
./main.py my-bucket     # same code path the Lambda runs
```

Nothing in here knows what the data is. It finds prefixes with a list of
regexes, bundles objects whose key matches a suffix, carries along a metadata
sidecar it never interprets, and writes an index. The same deployment serves a
mail archive and a photo archive; only their `bucket-archive/config.toml`
differ.

## Why

Deep Archive charges ~$1/TB/month, but adds 40 KB of billable metadata per
object and a request per restore. 400,000 individual objects would drown in
overhead. Packed into ~250 MiB tars, the overhead disappears.

## How it works

1. **Discover.** Walk the bucket one level per entry in `prefix_pattern`,
   matching each path segment. `['@', '.*']` finds `me@example.com/INBOX/`.
   `bucket-archive/` is always skipped, so it can never archive its own output.
2. **Select.** List a source prefix in key order, take objects matching
   `suffix_pattern` until `min_archive_mib` is reached, skipping anything newer
   than `min_age_seconds`.
3. **Bundle.** Stream each object and its `<key><sidecar_suffix>` sidecar into a
   tar via multipart upload, so memory stays at roughly one part regardless of
   size. The manifest goes in as the final member.
4. **Index.** Write the same manifest beside the tar in STANDARD.
5. **Delete.** Remove the sources and their sidecars.

That order matters: a crash before step 5 costs duplicate work, never data.

## Configuration

The invocation names the bucket — `argv[1]` on the CLI, `{"bucket": "..."}` in
the Lambda event — and the bucket names everything else. There is no
environment to set.

Defaults live in [`config.toml`](config.toml); each bucket overrides them at
`s3://<bucket>/bucket-archive/config.toml`:

```toml
prefix_pattern = ['@', '.*']      # one regex per level, top down
suffix_pattern = '\.eml\.zst$'    # which objects to bundle

sidecar_suffix = ".json"          # metadata for "<key>" lives at "<key>.json"

archive_storage_class  = "DEEP_ARCHIVE"
manifest_storage_class = "STANDARD"

min_archive_mib = 250
min_age_seconds = 3600            # never race the process still writing
part_size_mib   = 16
time_reserve_ms = 300_000         # stop starting archives near the Lambda timeout
```

A photo bucket needs no code change, only its own config:

```toml
prefix_pattern = ['^\d{4}$', '^\d{2}$']
suffix_pattern = '\.(jpe?g|png|mp4|mov)$'
sidecar_suffix = '.json'
```

Objects that do not match `suffix_pattern` are ignored entirely — which is how a
`state.json` can sit inside a source prefix and never be bundled or deleted.

## What it produces

```
bucket-archive/me@example.com/INBOX/archive-000001.tar                 DEEP_ARCHIVE
bucket-archive/me@example.com/INBOX/archive-000001.manifest.jsonl.zst  STANDARD
```

Archive numbers come from listing the archive prefix — `max(N) + 1` — so there
is no counter to keep in sync. Inside the tar, members are named relative to the
source prefix, each followed by its sidecar, with the manifest last:

```
2026/08/06/21-14-20.1786068860.uid-123456.eml.zst
2026/08/06/21-14-20.1786068860.uid-123456.eml.zst.json
...
archive-000001.manifest.jsonl.zst
```

The manifest is JSONL: a header line, then one line per object with its key,
size, etag and the full parsed sidecar.

```json
{"key":"…uid-123456.eml.zst","name":"2026/08/06/…","size":28019,
 "etag":"…","sidecar_key":"…eml.zst.json",
 "metadata":{"internaldate":"…","subject":"…","from":[…]}}
```

`sidecar_key: null` means no sidecar existed; a `sidecar_key` with
`metadata: null` means one existed but would not parse — the raw bytes are still
in the tar either way.

Searching costs one GET per archive and no Glacier restore:

```bash
aws s3 cp s3://BUCKET/bucket-archive/…/archive-000001.manifest.jsonl.zst - \
  | zstd -dc | jq -c 'select(.metadata.subject | test("invoice"; "i"))'
```

## Running it

The Lambda is scheduled nightly, one EventBridge rule per bucket. To run it by
hand:

```bash
aws lambda invoke --function-name imap-sync-s3-archiver \
  --cli-read-timeout 0 --payload '{"bucket":"my-bucket"}' /tmp/out.json
```

`--cli-read-timeout 0` is not optional. The CLI's 60-second default silently
retries, producing a second concurrent invocation that races the first.

Each run archives what it can in the time available and stops with
`time_reserve_ms` to spare; it is resumable by design, so a large backlog just
takes several invocations. Loop until it reports `"archives": []`.

```bash
./bin/deploy.sh          # build, upload to S3, update the function
./bin/deploy.sh --build  # build only
```

## Restoring

```bash
aws s3api restore-object --bucket BUCKET --key bucket-archive/…/archive-000001.tar \
  --restore-request Days=7,GlacierJobParameters={Tier=Bulk}   # 12-48h
aws s3 cp s3://BUCKET/bucket-archive/…/archive-000001.tar - | tar -x
```

The tar carries everything needed to interpret itself: every object, every
sidecar, and the manifest. No config, no database, no external index.

## Terraform module

[`terraform/`](terraform/) is a module: the function, its role and policy, its
log group, and one EventBridge rule per bucket. It references a package this
repo's `bin/deploy.sh` has already uploaded, so an apply does not need the
source checked out.

```hcl
module "bucket_archiver" {
  source = "git::ssh://git@github.com/maneyko/bucket-archiver.git//terraform?ref=v1.0.0"

  function_name       = "imap-sync-s3-archiver"
  archive_bucket_arns = [for bucket in aws_s3_bucket.archive : bucket.arn]

  artifact_bucket = aws_s3_bucket.lambda_artifacts.id
  artifact_key    = "imap-sync-s3-archiver/function.zip"

  schedules = {
    my-mail-archive  = "cron(0 8 * * ? *)"
    my-photo-archive = "cron(0 9 * * ? *)"
  }
}
```

`function_name` must match `lambda_name` in `bin/deploy.sh`; nothing enforces
that, and a mismatch means the deploy updates a function Terraform does not
manage. The runtime and handler are properties of the code, so the module pins
them rather than exposing them.

Bump the `ref` when the module changes. A consumer pinned to a tag will not pick
up `main`.
