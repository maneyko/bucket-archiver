import io
import platform

import boto3


def human_bytes(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(num) < 1024 or unit == "TiB":
            return f"{int(num)} B" if unit == "B" else f"{num:,.1f} {unit}"
        num /= 1024


class S3:
    """Thin wrapper over the bits of the S3 API the archiver needs."""

    def __init__(self, bucket: str):
        self.bucket = bucket
        aws_opts = {}
        if platform.uname().system == "Darwin":
            aws_opts["profile_name"] = "personal"
        self.client = boto3.Session(**aws_opts).client("s3")

    def list_objects(self, prefix: str):
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            yield from page.get("Contents", [])

    def list_common_prefixes(self, prefix: str = ""):
        """Yield the immediate "directories" under ``prefix``."""
        prefixes = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix, Delimiter="/"):
            prefixes.extend([item["Prefix"] for item in page.get("CommonPrefixes", [])])
        return prefixes

    def get_body(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def get_stream(self, key: str):
        """The object's body as a readable stream, plus its length; the caller closes it.

        The length comes from the response rather than a prior listing so it always
        describes the bytes actually being read.
        """
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"], response["ContentLength"]

    def get_body_or_none(self, key: str) -> bytes | None:
        try:
            return self.get_body(key)
        except self.client.exceptions.NoSuchKey:
            return None

    def put(self, key: str, body: bytes, **kwargs):
        return self.client.put_object(Bucket=self.bucket, Key=key, Body=body, **kwargs)

    def delete_keys(self, keys: list[str]) -> list[dict]:
        errors = []
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            response = self.client.delete_objects(
                Bucket=self.bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            errors.extend(response.get("Errors", []))
        return errors


class MultipartUploadStream(io.RawIOBase):
    """Write-only file object that streams into an S3 multipart upload.

    Bytes are buffered until ``part_size`` is reached and then flushed as one
    part, so peak memory stays at roughly one part regardless of object size.
    The upload is created lazily: a stream smaller than one part is written
    with a plain put_object instead.
    """

    def __init__(self, s3: S3, key: str, *, part_size: int, storage_class: str, content_type: str):
        self.client = s3.client
        self.bucket = s3.bucket
        self.key = key
        self.part_size = part_size
        self.put_args = {"StorageClass": storage_class, "ContentType": content_type}

        self.upload_id = None
        self.parts = []
        self.bytes_written = 0
        self._buffer = bytearray()

    def writable(self):
        return True

    def write(self, data: bytes) -> int:
        self._buffer += data
        self.bytes_written += len(data)
        while len(self._buffer) >= self.part_size:
            self._upload_part(bytes(self._buffer[:self.part_size]))
            del self._buffer[:self.part_size]
        return len(data)

    def _upload_part(self, chunk: bytes):
        if self.upload_id is None:
            response = self.client.create_multipart_upload(Bucket=self.bucket, Key=self.key, **self.put_args)
            self.upload_id = response["UploadId"]
        part_number = len(self.parts) + 1
        response = self.client.upload_part(
            Bucket=self.bucket,
            Key=self.key,
            UploadId=self.upload_id,
            PartNumber=part_number,
            Body=chunk,
        )
        self.parts.append({"ETag": response["ETag"], "PartNumber": part_number})

    def complete(self) -> dict:
        """Flush the tail and commit the object."""
        tail = bytes(self._buffer)
        self._buffer.clear()

        if self.upload_id is None:
            return self.client.put_object(Bucket=self.bucket, Key=self.key, Body=tail, **self.put_args)

        if tail:
            self._upload_part(tail)

        return self.client.complete_multipart_upload(
            Bucket=self.bucket,
            Key=self.key,
            UploadId=self.upload_id,
            MultipartUpload={"Parts": self.parts},
        )

    def abort(self):
        """Discard uploaded parts so we are not billed for orphans."""
        self._buffer.clear()
        if self.upload_id:
            self.client.abort_multipart_upload(Bucket=self.bucket, Key=self.key, UploadId=self.upload_id)
