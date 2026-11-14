from inleverboks.adapters.storage.base import ObjectNotFoundError, StorageBackend, StorageError
from inleverboks.adapters.storage.local import LocalStorage
from inleverboks.adapters.storage.s3 import S3Storage
from inleverboks.config import Settings


def build_storage(settings: Settings) -> StorageBackend:
    if settings.storage_backend == "s3":
        if not (settings.s3_bucket and settings.s3_access_key and settings.s3_secret_key):
            msg = "S3 storage is not fully configured"
            raise ValueError(msg)
        return S3Storage(
            endpoint=settings.s3_endpoint,
            bucket=settings.s3_bucket,
            region=settings.s3_region,
            access_key=settings.s3_access_key.get_secret_value(),
            secret_key=settings.s3_secret_key.get_secret_value(),
        )
    return LocalStorage(settings.storage_path)


__all__ = [
    "LocalStorage",
    "ObjectNotFoundError",
    "S3Storage",
    "StorageBackend",
    "StorageError",
    "build_storage",
]
