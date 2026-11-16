"""S3-compatible application object storage: Cloudflare R2 in production, moto locally.

boto3 is synchronous. Every call runs on a small thread pool of its own, so uploads, listings,
deletes and URL signing never block the event loop, and a slow store cannot take over the default
executor that other blocking work shares.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import boto3
from botocore.config import Config

# Concurrent calls per process; the client keeps as many pooled connections.
CONCURRENCY = 4


class ObjectStorage:
    def __init__(
        self,
        endpoint_url,
        bucket,
        region,
        access_key_id,
        secret_access_key,
        *,
        timeout=3.0,
    ):
        self._bucket = bucket
        self._executor = ThreadPoolExecutor(CONCURRENCY, thread_name_prefix="object-storage")
        # A session of its own: boto3's default session is shared module state.
        self._client = boto3.session.Session().client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=Config(
                signature_version="s3v4",
                # R2, MinIO and moto all accept path-style URLs; virtual hosts need DNS per bucket.
                s3={"addressing_style": "path"},
                connect_timeout=timeout,
                # Per socket read, not per request: a 16 MiB upload may take longer as a whole.
                read_timeout=30,
                retries={"mode": "standard", "max_attempts": 3},
                max_pool_connections=CONCURRENCY,
            ),
        )

    @classmethod
    def from_settings(cls, settings):
        return cls(
            settings.object_storage_endpoint_url,
            settings.object_storage_bucket,
            settings.object_storage_region,
            settings.object_storage_access_key_id.get_secret_value(),
            settings.object_storage_secret_access_key.get_secret_value(),
            timeout=settings.dependency_timeout,
        )

    async def _run(self, function, /, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, partial(function, *args, **kwargs))

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """Write one object; writing the same key again replaces it."""
        await self._run(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every object under ``prefix`` and return how many there were.

        Deleting a missing object succeeds, so a repeated or concurrent call is harmless.
        """

        def delete():
            count = 0
            pages = self._client.get_paginator("list_objects_v2")
            for page in pages.paginate(Bucket=self._bucket, Prefix=prefix):
                for item in page.get("Contents", ()):
                    self._client.delete_object(Bucket=self._bucket, Key=item["Key"])
                    count += 1
            return count

        return await self._run(delete)

    async def download_url(self, key: str, *, filename: str, seconds: int) -> str:
        """A presigned GET URL that serves the object as an attachment named ``filename``.

        Signing is local: nothing is sent to the store, and the URL does not prove the object
        exists. ``filename`` must be plain ASCII without quotes.
        """
        return await self._run(
            self._client.generate_presigned_url,
            ClientMethod="get_object",
            Params={
                "Bucket": self._bucket,
                "Key": key,
                "ResponseContentDisposition": f'attachment; filename="{filename}"',
            },
            ExpiresIn=seconds,
        )

    async def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._client.close()
