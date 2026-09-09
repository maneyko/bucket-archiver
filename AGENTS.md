# Working in this repo

Notes for whoever picks this up next, human or agent. The code is small and
should stay that way; most of what is worth knowing is *why* it is shaped this
way and which sharp edges have already drawn blood.

It was extracted from the `imap-sync` repo, where it lived as `compactor/`.
Nothing in it knows what an email is, and it must stay that way: deciding what
metadata *means* belongs to whichever uploader writes the sidecar. That
separation is the only reason one archiver can serve a mail bucket and a photo
bucket.

## Style

- Python 3.14, run via `uv` script shebangs with inline dependencies. No
  virtualenv, no requirements file. Python 3.14 is required for the stdlib
  `compression.zstd` module.
- Small modules, plain functions and classes, no frameworks.
- Prefer deleting code to adding abstraction.
- Comments explain *why*, never *what*. If a comment restates the line below it,
  delete the comment and fix the name.
- No config option gets added until a second real caller needs it. Constants in
  a toml beat flags, flags beat environment variables, and the archiver takes no
  environment at all — the invocation names the bucket (`argv[1]` on the CLI,
  `{"bucket": ...}` in the Lambda event) and the bucket names everything else.

## Invariants — do not break these

1. **The archiver keeps no checkpoint.** Whatever remains under a source prefix
   is what still needs archiving. Never add a state file "for speed".
2. **Delete only after the tar is committed.** The order is: write tar → write
   manifest → delete sources. A crash anywhere leaves duplicate work, never data
   loss.
3. **The tar is self-sufficient.** Sidecars and the manifest go inside it. This
   is what makes the manifest disposable and regenerable.
4. **One writer at a time.** The archiver assumes nothing else is mutating the
   bucket. EventBridge retries are disabled for this reason, and the function's
   `reserved_concurrent_executions = 1` is what actually enforces it — there is
   no lock. The module's `max_concurrency` default is `-1`, so a consumer that
   leaves it unset gets an archiver that can race itself.
5. **`time_reserve_ms` must exceed the worst single archive.** The loop starts
   an archive whenever more than the reserve remains, so the reserve — not the
   15-minute timeout — is the deadline one archive has to meet. Every limit that
   bounds an archive (`max_archive_objects`, `min_archive_mib`) has to be chosen
   against it, and `max_archive_objects` only converts to seconds through
   `fetch_workers`: the 10,000 default is ~110 s at 16 workers and ~950 s at
   one. Three settings, one constraint; changing any of them alone breaks it.
6. **Peak memory must not depend on the largest object.** Objects above
   `PREFETCH_MIB` are streamed rather than prefetched for this reason. Anything
   that buffers a whole object puts a size ceiling on the bucket.

## Sharp edges, all of which have already caused a bug

**IAM resources are scoped by suffix, not by path.** `*/email/*` broke silently
the moment the layout changed. The Lambda's write scope matches
`bucket-archive/*/archive-*`, which cannot accidentally match a `state.json` or
the config. Failures here are silent: deletes come back in `delete_errors`, not
as an exception.

**`zip -r` appends to an existing archive.** `bin/deploy.sh` removes the zip
first, or you ship files you deleted months ago.

**`aws lambda invoke` retries after 60 seconds.** The CLI's default read timeout
silently fires a *second concurrent invocation*, which then races the first for
the same objects and dies with `NoSuchKey`. Always pass `--cli-read-timeout 0`.

**`min_age_seconds` is a race guard, not a nicety.** An uploader writes the
object and then its sidecar. If the archiver bundles in between, that object
loses its metadata permanently. Keep it at 3600.

**A run's cost is round trips, not bytes.** Two archives off the same bucket:
3,900 objects took 291 s at 250 MiB, 3,713 took 328 s at 102 MiB. Two and a
half times the bytes for the same wall clock, and 250 MiB of tar moves at
0.4 MB/s — nowhere near the link. Anything reasoned about in megabytes will be
wrong by the ratio of mean object sizes, which spans 30x in one mail bucket.

**Asynchronous invocation retries a failure twice.** Queueing work with
`--invocation-type Event` turned 20 jobs into ~60 executions once the archives
started failing, and it kept going for the better part of an hour. Lowering
`MaximumEventAgeInSeconds` does not retroactively drop what is already queued —
one event still ran after the limit was set to 60 s. Reserved concurrency of 0
is what actually stops it. Drain backlogs synchronously.

**A timeout is not an exception, so `stream.abort()` never runs.** Lambda kills
the process, the `except` never fires, and the multipart upload is left open. An
open upload under `bucket-archive/` is therefore the fingerprint of a timeout:
`aws s3api list-multipart-uploads` is the morning-after check. Nothing is lost
either way, because the tar is only completed before the sources are deleted;
and because an OOM dies inside the GET, before any part is written, it leaves
nothing at all.

**`io.BytesIO(data)` does not copy.** It shares the initial buffer and only
copies on write, so wrapping a body to hand to `tarfile` costs 1x the object,
not 2x. Getting this wrong moves the predicted memory ceiling by a factor of
two — the measured boundary was a 1,356 MiB member archiving fine while an
1,856 MiB one died at 2047 MB of 2048.

**Deep Archive objects cannot be copied or renamed.** `CopyObject` fails with
`InvalidObjectState` until restored (12–48 h). Get the naming right before
writing, because you cannot fix it afterwards. Deleting early still bills the
180-day minimum.

## Testing

There are no unit tests, and adding a framework is not the answer. What has
worked:

- **Create a throwaway bucket** and exercise the real code path against it, then
  delete the bucket. Every change was validated this way, including the edge
  cases (missing sidecar, corrupt sidecar, `state.json` sitting inside a source
  prefix).
- **Set `delete_sources = false`** in the bucket config to rehearse a run
  without losing anything. Narrow `prefix_pattern` to the one prefix under test
  too, or the rehearsal writes a tar for every prefix it can reach.
- **Set `archive_storage_class = "STANDARD"` for the rehearsal as well.** A
  Deep Archive tar cannot be read for 12–48 h, so a rehearsal that writes one
  verifies nothing. Delete the rehearsal tar afterwards — with the sources still
  in place it is a duplicate of what the real run will write. The Lambda's own
  role is denied deletes under `bucket-archive/`; do it with your own
  credentials.
- **Verify by reading back from S3**, not by trusting the return code. Round-trip
  a tar, decompress a body, diff a manifest against the bucket listing. Hash
  both sides in chunks rather than loading them, or verifying a multi-gigabyte
  member needs more memory than writing it did.
- **Reconcile counts.** Nearly every real bug showed up as an arithmetic
  mismatch: manifest entries vs distinct objects, bundled objects vs deleted
  ones.
- **Drive the real classes against an in-memory S3** for anything about
  ordering, memory or concurrency. A fake with `list_objects`, `get_body`,
  `get_stream`, `put` and `delete_keys` is enough to exercise `Archiver` and
  `TarBuilder` unchanged, and instrumenting the fake's `get_body` is the only
  practical way to assert what the prefetch window actually holds. Make the fake
  delegate the way the real class does — a `get_body_or_none` that reads the
  store directly instead of calling `get_body` silently hides every sidecar
  fetch from the instrumentation.

## Deploying

```bash
./bin/deploy.sh     # builds the zip, uploads it, updates the Lambda
```

The Terraform module in `terraform/` pins the package by key (and optionally
version id), so an apply from a consumer repo will roll the function back to
whatever version that repo records. The CLI deploy is the fast path; Terraform
is the record.

Consumers track `main` unless they add `?ref=<tag>`; either way a module change
is only picked up on `terraform init -upgrade`.

Archiving is disabled per bucket by giving it an empty schedule list, which
deletes its EventBridge rules. There is deliberately no disabled-but-present
rule: what exists is what runs.

## Repo map

```
main.py              Lambda handler and CLI, same code path
config.toml          defaults; the bucket's own config overrides them
lib/archiver.py      prefix discovery, selection, the archive loop
lib/tar_builder.py   prefetch window, objects + sidecars, the manifest
lib/s3util.py        S3 wrapper and the multipart upload stream
lib/config.py        merges config.toml with the bucket's own
bin/deploy.sh        build, upload, update function code
terraform/           the module consumers use: function, role, schedule
```
