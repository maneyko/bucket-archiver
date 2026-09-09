import io
import json
import tarfile
import time
from collections import deque
from compression import zstd  # Python 3.14 stdlib
from concurrent.futures import ThreadPoolExecutor

from lib.s3util import MultipartUploadStream

# How much fetched-but-not-yet-written object data may sit in memory. Object
# sizes in one bucket span four orders of magnitude -- the sixteen largest in
# the mail archive total 967 MiB -- so a lookahead bounded only by object count
# would not fit in the Lambda.
PREFETCH_MIB = 64


class TarBuilder:
    """Streams the objects under one source prefix, and their metadata sidecars, into tar bundles.

    Each object is stored next to its "<key><sidecar_suffix>" sidecar, and the
    manifest is written as the final member, so a restored tar carries everything
    needed to interpret it without consulting any config. The identical manifest
    is also stored beside the tar as an index: lose it and it can be recovered.
    """

    def __init__(self, s3, source_prefix: str, settings):
        self.s3 = s3
        self.source_prefix = source_prefix
        self.settings = settings

    def build(self, objects: list[dict], tar_key: str) -> dict:
        """Stream ``objects`` into ``tar_key`` and write a manifest inside it and beside it."""
        members = []
        manifest_key = tar_key.removesuffix(".tar") + ".manifest.jsonl.zst"
        stream = MultipartUploadStream(
            self.s3, tar_key,
            part_size=self.settings.part_size_mib*1024**2,
            storage_class=self.settings.archive_storage_class,
            content_type="application/x-tar",
        )
        try:
            # mode="w|" is the streaming (non-seekable) tar writer.
            with (
                ThreadPoolExecutor(max_workers=self.settings.fetch_workers) as pool,
                tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as tar,
            ):
                for obj, body, sidecar in self.fetched(objects, pool):
                    members.append(self.add_member(tar, obj, body, sidecar))
                manifest = self.manifest_body(tar_key, members)
                self.add_file(tar, manifest_key.rsplit("/", 1)[-1], manifest, int(time.time()))
            stream.complete()
        except Exception:
            stream.abort()
            raise

        # The manifest stays in STANDARD so "which bundle holds this object?" can
        # be answered without a Glacier restore.
        self.s3.put(
            manifest_key,
            manifest,
            ContentType="application/jsonl+zstd",
            StorageClass=self.settings.manifest_storage_class,
        )

        return {
            "tar_key": tar_key,
            "manifest_key": manifest_key,
            "tar_bytes": stream.bytes_written,
            "parts": len(stream.parts) or 1,
            "members": members,
            "source_bytes": sum(member["size"] for member in members),
            "keys": [member["key"] for member in members]
                  + [member["sidecar_key"] for member in members if member["sidecar_key"]],
        }

    def fetched(self, objects: list[dict], pool: ThreadPoolExecutor):
        """Yield (obj, body, sidecar) in ``objects`` order, fetching ahead of the writer.

        The tar writer is sequential and the members must keep the order the
        manifest records, but the two GETs each member costs are independent, and
        a run's time is almost entirely those round trips.
        """
        budget = PREFETCH_MIB*1024**2
        queue = deque()
        in_flight = 0
        index = 0

        while index < len(objects) or queue:
            while index < len(objects) and len(queue) < self.settings.fetch_workers:
                size = objects[index]["Size"]
                # Always keep one in flight, or an object over the budget stalls.
                if queue and in_flight + size > budget:
                    break
                queue.append((size, pool.submit(self.fetch, objects[index])))
                in_flight += size
                index += 1
            size, future = queue.popleft()
            in_flight -= size
            yield future.result()

    def fetch(self, obj: dict) -> tuple[dict, bytes, bytes | None]:
        key = obj["Key"]
        return obj, self.s3.get_body(key), self.s3.get_body_or_none(key + self.settings.sidecar_suffix)

    def add_member(self, tar: tarfile.TarFile, obj: dict, body: bytes, sidecar: bytes | None) -> dict:
        key = obj["Key"]
        mtime = int(obj["LastModified"].timestamp())
        name = self.add_file(tar, key.removeprefix(self.source_prefix), body, mtime)

        sidecar_key = key + self.settings.sidecar_suffix
        if sidecar is not None:
            self.add_file(tar, sidecar_key.removeprefix(self.source_prefix), sidecar, mtime)

        return {
            "key": key,
            "name": name,
            "size": len(body),
            "etag": obj.get("ETag", "").strip('"'),
            "last_modified": obj["LastModified"].isoformat(),
            "sidecar_key": sidecar_key if sidecar is not None else None,
            "metadata": self.parse_sidecar(sidecar_key, sidecar),
        }

    def add_file(self, tar: tarfile.TarFile, name: str, data: bytes, mtime: int) -> str:
        info = tarfile.TarInfo(name=name)
        info.size = len(data)
        info.mtime = mtime
        info.mode = 0o644
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        tar.addfile(info, io.BytesIO(data))
        return info.name

    def parse_sidecar(self, key: str, sidecar: bytes | None):
        """The sidecar is already safe inside the tar, so bad JSON only costs us the index entry."""
        if sidecar is None:
            return None
        try:
            return json.loads(sidecar)
        except json.JSONDecodeError as err:
            print(f"WARNING: unparsable sidecar {key}: {err}")
            return None

    def manifest_body(self, tar_key: str, members: list[dict]) -> bytes:
        """A JSONL listing of the bundle contents, one line per object plus a header.

        The tar's own size is deliberately absent: this goes inside the tar, so it
        cannot describe its own length. Ask S3, or stat the file.
        """
        lines = [json.dumps({
            "type": "header",
            "tar_key": tar_key,
            "source_prefix": self.source_prefix,
            "storage_class": self.settings.archive_storage_class,
            "object_count": len(members),
            "source_bytes": sum(member["size"] for member in members),
        })]
        lines += [json.dumps(member) for member in members]
        return zstd.compress("\n".join(lines).encode() + b"\n", level=9)
