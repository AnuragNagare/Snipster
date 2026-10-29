"""Cloudflare R2 (S3-compatible) storage service."""

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import settings
from app.core.logging import logger


class StorageService:
    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=settings.STORAGE_ENDPOINT,
                aws_access_key_id=settings.STORAGE_ACCESS_KEY,
                aws_secret_access_key=settings.STORAGE_SECRET_KEY,
                config=Config(signature_version="s3v4"),
                region_name="auto",
            )
        return self._client

    def upload_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        """Upload raw bytes to R2."""
        self.client.put_object(
            Bucket=settings.STORAGE_BUCKET,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        logger.info(f"[STORAGE] Uploaded {key} ({len(data)} bytes)")

    def upload_file(self, key: str, file_path: str, content_type: str = "video/mp4") -> None:
        """Upload a local file to R2."""
        self.client.upload_file(
            Filename=file_path,
            Bucket=settings.STORAGE_BUCKET,
            Key=key,
            ExtraArgs={"ContentType": content_type},
        )
        logger.info(f"[STORAGE] Uploaded file {key} from {file_path}")

    def download_to_file(self, key: str, dest_path: str) -> None:
        """Download a file from R2 to a local path."""
        self.client.download_file(
            Bucket=settings.STORAGE_BUCKET,
            Key=key,
            Filename=dest_path,
        )
        logger.info(f"[STORAGE] Downloaded {key} → {dest_path}")

    def get_presigned_upload_url(self, key: str, content_type: str, expires_in: int = 3600) -> str:
        """A temporary URL the browser can PUT a file to directly, with no credentials of its own."""
        return self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": settings.STORAGE_BUCKET, "Key": key, "ContentType": content_type},
            ExpiresIn=expires_in,
        )

    def object_size(self, key: str) -> int | None:
        """Size in bytes of a stored object, or None if it does not exist."""
        try:
            return self.client.head_object(Bucket=settings.STORAGE_BUCKET, Key=key)["ContentLength"]
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise

    def get_presigned_url(self, key: str, expires_in: int = 3600) -> str:
        """Generate a presigned URL for temporary access to a private object."""
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.STORAGE_BUCKET, "Key": key},
            ExpiresIn=expires_in,
        )

    def get_public_url(self, key: str) -> str:
        """Get the public URL for an object (requires bucket to have public access)."""
        base = settings.STORAGE_PUBLIC_BASE_URL.rstrip("/")
        return f"{base}/{key}"

    def delete(self, key: str) -> None:
        """Delete an object from R2."""
        self.client.delete_object(Bucket=settings.STORAGE_BUCKET, Key=key)
        logger.info(f"[STORAGE] Deleted {key}")


storage_service = StorageService()
