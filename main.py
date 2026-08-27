#!/usr/bin/env -S PYTHONUNBUFFERED=1 uv run --script

# /// script
# dependencies = ["boto3"]
# requires-python = ">=3.14"
# ///

"""Roll many small S3 objects into few large tars, then delete the originals.

The bucket to work on is named by the caller -- argv[1] on the CLI, "bucket" in
the Lambda event -- and everything else is read from
s3://<bucket>/bucket-archive/config.toml, so one deployment can serve several
buckets with different layouts::

    prefix_pattern = ['@', '.*']     # one regex per level, top down
    suffix_pattern = '\\.eml\\.zst$'   # which objects to bundle

Each discovered prefix is archived into the matching path under
"bucket-archive/". An object's metadata sidecar travels with it into the tar,
and the manifest is written both inside the tar and beside it::

    me@example.com/INBOX/2026/08/06/21-14-20.1786068860.uid-123456.eml.zst
    me@example.com/INBOX/2026/08/06/21-14-20.1786068860.uid-123456.eml.zst.json
    bucket-archive/me@example.com/INBOX/archive-000042.tar
    bucket-archive/me@example.com/INBOX/archive-000042.manifest.jsonl.zst

Once a prefix holds min_archive_mib of matching objects, the oldest are streamed
into the next tar and then deleted, so a source prefix only ever holds what has
not been archived yet.

    ./main.py my-mail-archive
"""

import json
import sys

from lib.archiver import Archiver


def lambda_handler(event=None, context=None):
    summary = Archiver(event["bucket"], context).run()
    print(json.dumps(summary, indent=2, default=str))
    return summary


if __name__ == "__main__":
    lambda_handler({"bucket": sys.argv[1]})
    sys.exit(0)
