import re
import time

from lib.config import ARCHIVE_ROOT, load_settings
from lib.s3util import S3, human_bytes
from lib.tar_builder import TarBuilder


class Archiver:
    """Bundles every source prefix that has enough pending data into tars.

    Source prefixes are discovered by walking the bucket one level per entry in
    ``prefix_pattern``; whatever the last level matches holds the objects to
    bundle. Each bundle is written to the same path under "bucket-archive/", so
    "me@example.com/INBOX/email/" archives into
    "bucket-archive/me@example.com/INBOX/email/archive-000001.tar".

    There is no checkpoint file: sources are deleted once they are inside a tar,
    so whatever is left under a source prefix is exactly what still needs archiving.
    """

    def __init__(self, bucket: str, context=None):
        self.s3 = S3(bucket)
        self.settings = load_settings(self.s3)
        self.context = context

    def run(self) -> dict:
        time_started = time.monotonic()
        archives = []
        for source_prefix in self.discover_prefixes():
            while self.time_left_ms() > self.settings.time_reserve_ms:
                result = self.archive_once(source_prefix)
                if result is None:
                    break
                archives.append(result)
                # Nothing was deleted, so the same objects would be bundled again.
                if not self.settings.delete_sources:
                    break

        return {
            "bucket": self.s3.bucket,
            "archives": archives,
            "objects": sum(archive["objects"] for archive in archives),
            "source_bytes": sum(archive["source_bytes"] for archive in archives),
            "tar_bytes": sum(archive["tar_bytes"] for archive in archives),
            "elapsed_s": round(time.monotonic() - time_started, 1),
        }

    def archive_once(self, source_prefix: str) -> dict | None:
        """Build one tar for this prefix, or return None if there is not enough data."""
        objects = self.select_objects(source_prefix)
        pending_bytes = sum(obj["Size"] for obj in objects)

        if pending_bytes < self.settings.min_archive_mib*1024**2:
            print(
                f"{source_prefix}: {len(objects):,} objects / {human_bytes(pending_bytes)} pending "
                f"(< {self.settings.min_archive_mib} MiB), waiting"
            )
            return None

        tar_key = f"{self.archive_prefix(source_prefix)}archive-{self.next_archive_number(source_prefix):06d}.tar"
        print(f"{source_prefix}: bundling {len(objects):,} objects / {human_bytes(pending_bytes)} -> {tar_key}")

        result = TarBuilder(self.s3, source_prefix, self.settings).build(objects, tar_key)

        errors = []
        if self.settings.delete_sources:
            errors = self.s3.delete_keys(result["keys"])
            for error in errors[:10]:
                print(f"ERROR: failed to delete {error.get('Key')}: {error.get('Code')} {error.get('Message')}")
            deleted = f"deleted {len(result['keys']) - len(errors):,} objects and sidecars"
        else:
            deleted = f"kept {len(result['keys']):,} objects and sidecars (delete_sources = false)"

        print(
            f"{source_prefix}: wrote {tar_key} "
            f"({human_bytes(result['tar_bytes'])}, {result['parts']} parts, {len(result['members']):,} members), "
            f"{deleted}"
        )

        return {
            "prefix": source_prefix,
            "tar_key": tar_key,
            "manifest_key": result["manifest_key"],
            "objects": len(result["members"]),
            "source_bytes": result["source_bytes"],
            "tar_bytes": result["tar_bytes"],
            "delete_errors": errors,
        }

    def select_objects(self, source_prefix: str) -> list[dict]:
        """The oldest objects worth up to one archive, skipping any still being written."""
        cutoff = time.time() - self.settings.min_age_seconds
        objects = []
        pending_bytes = 0

        for obj in self.s3.list_objects(source_prefix):
            if not re.search(self.settings.suffix_pattern, obj["Key"]):
                continue
            if obj["LastModified"].timestamp() > cutoff:
                break
            objects.append(obj)
            pending_bytes += obj["Size"]
            if pending_bytes >= self.settings.min_archive_mib*1024**2:
                break

        return objects

    def next_archive_number(self, source_prefix: str) -> int:
        numbers = (
            int(match.group(1))
            for obj in self.s3.list_objects(self.archive_prefix(source_prefix))
            if (match := re.search(r"archive-(\d+)\.tar$", obj["Key"]))
        )
        return max(numbers, default=0) + 1

    def archive_prefix(self, source_prefix: str) -> str:
        return ARCHIVE_ROOT + source_prefix

    def discover_prefixes(self) -> list[str]:
        """Descend one level per prefix_pattern entry, matching each name along the way."""
        prefixes = [""]
        for pattern in self.settings.prefix_pattern:
            prefixes = [
                child
                for parent in prefixes
                for child in self.s3.list_common_prefixes(parent)
                if child != ARCHIVE_ROOT and re.search(pattern, child.removeprefix(parent).rstrip("/"))
            ]
        return prefixes

    def time_left_ms(self) -> float:
        if self.context is None:
            return float("inf")
        return self.context.get_remaining_time_in_millis()
