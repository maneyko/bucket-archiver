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
   bucket. EventBridge retries are disabled for this reason.

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
  without losing anything.
- **Verify by reading back from S3**, not by trusting the return code. Round-trip
  a tar, decompress a body, diff a manifest against the bucket listing.
- **Reconcile counts.** Nearly every real bug showed up as an arithmetic
  mismatch: manifest entries vs distinct objects, bundled objects vs deleted
  ones.

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
lib/tar_builder.py   streams objects + sidecars into a tar, writes the manifest
lib/s3util.py        S3 wrapper and the multipart upload stream
lib/config.py        merges config.toml with the bucket's own
bin/deploy.sh        build, upload, update function code
terraform/           the module consumers use: function, role, schedule
```
