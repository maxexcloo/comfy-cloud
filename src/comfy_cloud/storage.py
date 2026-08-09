from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class ObjectStorage:
    """S3-compatible object storage for gateway outputs.

    Falls back to proxying ComfyUI's /view when the client cannot be built
    (missing boto3) or uploads fail, so storage is strictly additive.
    """

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        region: str,
        prefix: str = "",
        public_base_url: str | None = None,
        expires: int = 3600,
    ) -> None:
        import boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self.expires = expires
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name=region,
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> ObjectStorage | None:
        import os

        env = os.environ if env is None else env
        bucket = env.get("S3_BUCKET")
        if not bucket:
            return None
        try:
            return cls(
                endpoint_url=env.get("S3_ENDPOINT_URL", "https://s3.amazonaws.com"),
                bucket=bucket,
                access_key_id=env.get("S3_ACCESS_KEY_ID", ""),
                secret_access_key=env.get("S3_SECRET_ACCESS_KEY", ""),
                region=env.get("S3_REGION", "us-east-1"),
                prefix=env.get("S3_PREFIX", "outputs"),
                public_base_url=env.get("S3_PUBLIC_BASE_URL"),
                expires=int(env.get("S3_URL_EXPIRES", "3600")),
            )
        except ImportError:
            return None

    def key(self, filename: str, subfolder: str = "") -> str:
        parts = [part for part in (self.prefix, subfolder, filename) if part]
        return "/".join(parts)

    def url(self, key: str) -> str:
        if self.public_base_url:
            return f"{self.public_base_url}/{key}"
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=self.expires,
        )

    def _upload(self, key: str, content: bytes, content_type: str) -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType=content_type or "application/octet-stream",
        )

    def _upload_path(self, key: str, path: Path, content_type: str) -> None:
        self._client.upload_file(
            str(path),
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type or "application/octet-stream"},
        )

    async def upload(
        self, filename: str, content: bytes, content_type: str, subfolder: str = ""
    ) -> str | None:
        try:
            key = self.key(filename, subfolder)
            await asyncio.to_thread(self._upload, key, content, content_type)
            return self.url(key)
        except Exception as exc:  # noqa: BLE001 - storage must never break generation
            log.warning("object storage upload failed: %s", exc)
            return None

    async def upload_path(
        self,
        filename: str,
        path: Path,
        content_type: str,
        subfolder: str = "",
    ) -> str | None:
        try:
            key = self.key(filename, subfolder)
            await asyncio.to_thread(self._upload_path, key, path, content_type)
            return self.url(key)
        except Exception as exc:  # noqa: BLE001 - storage must never break generation
            log.warning("object storage upload failed: %s", exc)
            return None
