import io
import json
import tarfile
import time
from compression import zstd  # Python 3.14 stdlib

from lib.s3util import MultipartUploadStream


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
            with tarfile.open(fileobj=stream, mode="w|", format=tarfile.PAX_FORMAT) as tar:
                for obj in objects:
                    members.append(self.add_member(tar, obj))
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

    def add_member(self, tar: tarfile.TarFile, obj: dict) -> dict:
        key = obj["Key"]
        mtime = int(obj["LastModified"].timestamp())
        body, size = self.s3.get_stream(key)
        with body:
            name = self.add_stream(tar, key.removeprefix(self.source_prefix), body, size, mtime)

        sidecar_key = key + self.settings.sidecar_suffix
        sidecar = self.s3.get_body_or_none(sidecar_key)
        if sidecar is not None:
            self.add_file(tar, sidecar_key.removeprefix(self.source_prefix), sidecar, mtime)

        return {
            "key": key,
            "name": name,
            "size": size,
            "etag": obj.get("ETag", "").strip('"'),
            "last_modified": obj["LastModified"].isoformat(),
            "sidecar_key": sidecar_key if sidecar is not None else None,
            "metadata": self.parse_sidecar(sidecar_key, sidecar),
        }

    def add_file(self, tar: tarfile.TarFile, name: str, data: bytes, mtime: int) -> str:
        return self.add_stream(tar, name, io.BytesIO(data), len(data), mtime)

    def add_stream(self, tar: tarfile.TarFile, name: str, fileobj, size: int, mtime: int) -> str:
        """Copy ``size`` bytes from ``fileobj`` into the tar in 16 KiB chunks."""
        info = tarfile.TarInfo(name=name)
        info.size = size
        info.mtime = mtime
        info.mode = 0o644
        info.uid = info.gid = 0
        info.uname = info.gname = ""
        tar.addfile(info, fileobj)
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
