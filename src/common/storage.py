import asyncio
import logging
import mimetypes
import os
from functools import partial
from pathlib import Path
from typing import Callable

import boto3
import botocore

from common.redis import RedisClient
from common.utils import get_logger
from core import settings

logger = get_logger(__name__)


class StorageS3:
    """Simple client (singleton) for access to S3 bucket"""

    __instance = None
    BUCKET_NAME = settings.S3_BUCKET_NAME
    CODE_OK = 0
    CODE_CLIENT_ERROR = 1
    CODE_COMMON_ERROR = 2

    def __new__(cls, *args, **kwargs):
        if not cls.__instance:
            cls.__instance = super().__new__(cls, *args, **kwargs)

        return cls.__instance

    def __init__(self):
        logger.debug("Creating s3 client's session (boto3)...")
        session = boto3.session.Session(
            aws_access_key_id=settings.S3_AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.S3_AWS_SECRET_ACCESS_KEY,
            region_name=settings.S3_REGION_NAME,
        )
        logger.debug("Boto3 (s3) Session <%s> created", session)
        self.s3 = session.client(service_name="s3", endpoint_url=settings.S3_STORAGE_URL)
        logger.debug("S3 client %s created", self.s3)

    def __call(
        self, handler: Callable, error_log_level: int = logging.ERROR, **handler_kwargs
    ) -> tuple[int, dict | None]:
        try:
            logger.info("Executing request (%s) to S3 kwargs: %s", handler.__name__, handler_kwargs)
            response = handler(**handler_kwargs)

        except botocore.exceptions.ClientError as exc:
            logger.log(
                error_log_level,
                "Couldn't execute request (%s) to S3: ClientError %r",
                handler.__name__,
                exc,
            )
            return self.CODE_CLIENT_ERROR, None

        except Exception as exc:
            logger.exception("Shit! We couldn't execute %s to S3: %r", handler.__name__, exc)
            return self.CODE_COMMON_ERROR, None

        return self.CODE_OK, response

    def upload_file(
        self,
        src_path: str | Path,
        dst_path: str,
        filename: str | None = None,
        callback: Callable | None = None,
    ) -> str | None:
        """Upload file to S3 storage"""

        mimetype, _ = mimetypes.guess_type(src_path)
        filename = filename or os.path.basename(src_path)
        dst_path = os.path.join(dst_path, filename)
        code, _ = self.__call(
            self.s3.upload_file,
            Filename=str(src_path),
            Bucket=settings.S3_BUCKET_NAME,
            Key=dst_path,
            Callback=callback,
            ExtraArgs={"ContentType": mimetype},
        )
        if code != self.CODE_OK:
            return None

        logger.info("File %s successful uploaded. Remote path: %s", filename, dst_path)
        return dst_path

    def copy_file(self, src_path: str, dst_path: str) -> str | None:
        """Upload file to S3 storage"""

        code, _ = self.__call(
            self.s3.copy_object,
            Bucket=settings.S3_BUCKET_NAME,
            Key=dst_path,
            CopySource={"Bucket": settings.S3_BUCKET_NAME, "Key": src_path},
        )
        if code != self.CODE_OK:
            return None

        logger.info("File successful copied: %s -> %s", src_path, dst_path)
        return dst_path

    async def upload_file_async(
        self,
        src_path: str | Path,
        dst_path: str,
        filename: str | None = None,
        callback: Callable | None = None,
    ):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            partial(
                self.upload_file,
                src_path=src_path,
                dst_path=dst_path,
                filename=filename,
                callback=callback,
            ),
        )

    def get_file_info(
        self,
        filename: str,
        remote_path: str = settings.S3_BUCKET_AUDIO_PATH,
        error_log_level: int = logging.ERROR,
        dst_path: str | None = None,
    ) -> dict | None:
        """
        Allows finding file information (headers) on remote storage (S3)
        Headers content info about downloaded file
        """
        dst_path = dst_path or os.path.join(remote_path, filename)
        _, result = self.__call(
            self.s3.head_object,
            error_log_level=error_log_level,
            Key=dst_path,
            Bucket=self.BUCKET_NAME,
        )
        return result

    def get_file_size(
        self,
        filename: str | None = None,
        remote_path: str = settings.S3_BUCKET_AUDIO_PATH,
        dst_path: str | None = None,
    ) -> int:
        """
        Allows finding file on remote storage (S3) and calculate size
        (content-length / file size)
        """

        if filename or dst_path:
            file_info = self.get_file_info(
                filename, remote_path, dst_path=dst_path, error_log_level=logging.WARNING
            )
            if file_info:
                return int(file_info["ResponseMetadata"]["HTTPHeaders"]["content-length"])

        logger.info("File %s was not found on s3 storage", filename)
        return 0

    async def get_file_size_async(
        self,
        filename: str | None = None,
        remote_path: str = settings.S3_BUCKET_AUDIO_PATH,
        dst_path: str | None = None,
    ):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            partial(
                self.get_file_size,
                filename=filename,
                remote_path=remote_path,
                dst_path=dst_path,
            ),
        )

    def delete_file(
        self,
        filename: str | None = None,
        remote_path: str = settings.S3_BUCKET_AUDIO_PATH,
        dst_path: str | None = None,
    ):
        if not dst_path and not filename:
            raise ValueError("At least one argument must be set: dst_path | filename")

        dst_path = dst_path or os.path.join(remote_path, filename)
        _, result = self.__call(self.s3.delete_object, Key=dst_path, Bucket=self.BUCKET_NAME)
        return result

    async def delete_files_async(self, filenames: list[str], remote_path: str):
        loop = asyncio.get_running_loop()
        for filename in filenames:
            dst_path = os.path.join(remote_path, filename)
            await loop.run_in_executor(
                None,
                partial(
                    self.__call,
                    self.s3.delete_object,
                    Key=dst_path,
                    Bucket=self.BUCKET_NAME,
                ),
            )

    async def get_presigned_url(self, remote_path: str) -> str:
        # TODO: make async call
        #  https://boto3.amazonaws.com/v1/documentation/api/latest/guide/s3-presigned-urls.html

        redis = RedisClient()
        if not (url := await redis.get(remote_path)):
            _, url = self.__call(
                self.s3.generate_presigned_url,
                ClientMethod="get_object",
                Params={"Bucket": settings.S3_BUCKET_NAME, "Key": remote_path},
                ExpiresIn=settings.S3_LINK_EXPIRES_IN,
            )
            await redis.set(remote_path, value=url, ttl=settings.S3_LINK_CACHE_EXPIRES_IN)

        return url
