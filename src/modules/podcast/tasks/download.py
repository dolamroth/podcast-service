import asyncio
import logging
import os.path
from pathlib import Path

from yt_dlp.utils import YoutubeDLError

from core import settings
from common.redis import RedisClient
from common.storage import StorageS3
from common.enums import EpisodeStatus
from common.utils import get_logger, download_content
from common.exceptions import NotFoundError, MaxAttemptsReached
from modules.media.models import File
from modules.podcast.models import Episode, Cookie
from modules.podcast.tasks.base import RQTask, FinishCode
from modules.podcast.tasks.rss import GenerateRSSTask
from modules.podcast.utils import get_file_size
from modules.providers import utils as provider_utils
from modules.podcast import utils as podcast_utils
from modules.providers.utils import ffmpeg_preparation, SOURCE_CFG_MAP

__all__ = ["DownloadEpisodeTask", "UploadedEpisodeTask", "DownloadEpisodeImageTask"]

logger = get_logger(__name__)
log_levels = {
    FinishCode.OK: logging.INFO,
    FinishCode.SKIP: logging.INFO,
    FinishCode.ERROR: logging.ERROR,
}


class DownloadingInterrupted(Exception):
    def __init__(self, code: FinishCode, message: str = ""):
        self.code = code
        self.message = message

    def __repr__(self):
        return f'DownloadingInterrupted({self.code.name}, "{self.message}")'


class DownloadEpisodeTask(RQTask):
    """
    Allows downloading media from the source and recreate podcast's rss (by requested episode_id)
    """

    storage: StorageS3

    async def run(self, episode_id: int) -> int:  # pylint: disable=arguments-differ
        self.storage = StorageS3()

        try:
            code = await self.perform_run(int(episode_id))
        except DownloadingInterrupted as exc:
            message = "Episode downloading was interrupted: %r"
            logger.log(log_levels[exc.code], message, exc)
            return exc.code.value

        except Exception as exc:
            logger.exception("Unable to prepare/publish episode: %r", exc)
            await Episode.async_update(
                self.db_session,
                filter_kwargs={"id": episode_id},
                update_data={"status": Episode.Status.ERROR},
            )
            return FinishCode.ERROR

        return code.value

    async def perform_run(self, episode_id: int) -> FinishCode:
        """
        Main operation for downloading, performing and uploading audio to the storage.

        :raise: DownloadingInterrupted (if downloading is broken or unnecessary)
        """

        episode: Episode = await Episode.async_get(self.db_session, id=episode_id)

        logger.info(
            "=== [%s] START downloading process URL: %s ===",
            episode.source_id,
            episode.watch_url,
        )
        await self._check_is_needed(episode)
        await self._remove_unfinished(episode)
        await self._update_episodes(episode, update_data={"status": Episode.Status.DOWNLOADING})
        tmp_audio_path = await self._download_episode(episode)

        await self._process_file(episode, tmp_audio_path)
        remote_file_size = await self._upload_file(episode, tmp_audio_path)
        await self._update_episodes(
            episode,
            update_data={
                "status": Episode.Status.PUBLISHED,
                "published_at": episode.created_at,
            },
        )
        await self._update_files(episode, {"size": remote_file_size, "available": True})
        await self._update_all_rss(episode.source_id)

        podcast_utils.delete_file(tmp_audio_path)

        logger.info("=== [%s] DOWNLOADING total finished ===", episode.source_id)
        await RedisClient().async_publish(
            channel=settings.REDIS_PROGRESS_PUBSUB_CH,
            message=settings.REDIS_PROGRESS_PUBSUB_SIGNAL,
        )
        return FinishCode.OK

    async def _check_is_needed(self, episode: Episode):
        """Finding already downloaded file for episode's audio file path"""

        if not (audio_path := episode.audio.path):
            return

        stored_file_size = self.storage.get_file_size(dst_path=audio_path)
        if stored_file_size and stored_file_size == episode.audio.size:
            logger.info(
                "[%s] Episode already downloaded and file correct. Downloading will be ignored.",
                episode.source_id,
            )
            await self._update_episodes(
                episode,
                update_data={
                    "status": Episode.Status.PUBLISHED,
                    "published_at": episode.created_at,
                },
            )
            await self._update_files(episode, {"size": stored_file_size})
            await self._update_all_rss(episode.source_id)
            raise DownloadingInterrupted(code=FinishCode.SKIP)

    async def _download_episode(self, episode: Episode) -> Path:
        """Fetching info from external resource and extract audio from target source"""
        if not SOURCE_CFG_MAP[episode.source_type].need_downloading:
            if result_path := episode.audio.path:
                return Path(result_path)

            raise DownloadingInterrupted(
                code=FinishCode.ERROR,
                message="Episode [source: UPLOAD] does not contain audio with predefined path",
            )

        cookie = (
            await Cookie.async_get(self.db_session, id=episode.cookie_id)
            if episode.cookie_id
            else None
        )

        try:
            result_path = await provider_utils.download_audio(
                episode.watch_url, filename=episode.audio_filename, cookie=cookie
            )
        except YoutubeDLError as exc:
            logger.exception(
                "=== [%s] Downloading FAILED: Could not download track: %r. "
                "All episodes will be moved to the ERROR state",
                episode.source_id,
                exc,
            )
            await self._update_episodes(episode, {"status": Episode.Status.ERROR})
            await self._update_files(episode, {"available": False})
            raise DownloadingInterrupted(code=FinishCode.ERROR) from exc

        logger.info("=== [%s] DOWNLOADING was done ===", episode.source_id)
        return result_path

    async def _remove_unfinished(self, episode: Episode):
        """Finding unfinished downloading and remove file from the storage (S3)"""

        if not (audio_path := episode.audio.path):
            return

        if episode.status not in (Episode.Status.NEW, Episode.Status.DOWNLOADING):
            logger.warning(
                "[%s] Episode is %s but file-size seems not correct. "
                "Removing not-correct file %s and reloading it from providers.",
                episode.source_id,
                episode.status,
                audio_path,
            )
            self.storage.delete_file(audio_path)

    @staticmethod
    async def _process_file(episode: Episode, tmp_audio_path: Path):
        """Postprocessing for downloaded audio file"""
        source_config = SOURCE_CFG_MAP[episode.source_type]
        if source_config.need_postprocessing:
            logger.info("=== [%s] POST PROCESSING === ", episode.source_id)
            provider_utils.ffmpeg_preparation(src_path=tmp_audio_path)
            logger.info("=== [%s] POST PROCESSING was done === ", episode.source_id)
        else:
            logger.info("=== [%s] POST PROCESSING SKIP === ", episode.source_id)

    async def _upload_file(self, episode: Episode, tmp_audio_path: Path):
        """Uploading file to the storage (S3)"""

        logger.info("=== [%s] UPLOADING === ", episode.source_id)
        remote_path = podcast_utils.upload_episode(tmp_audio_path)
        if not remote_path:
            logger.warning("=== [%s] UPLOADING was broken === ")
            await self._update_episodes(episode, {"status": Episode.Status.ERROR})
            raise DownloadingInterrupted(code=FinishCode.ERROR)

        await self._update_files(episode, {"path": remote_path})
        result_file_size = self.storage.get_file_size(tmp_audio_path.name)
        logger.info(
            "=== [%s] UPLOADING was done (%i bytes) === ", episode.source_id, result_file_size
        )
        return result_file_size

    async def _update_all_rss(self, source_id: str):
        """Regenerating rss for all podcast with requested episode (by source_id)"""

        logger.info("Episodes with source #%s: updating rss for all podcast", source_id)
        affected_episodes = await Episode.async_filter(self.db_session, source_id=source_id)
        podcast_ids = sorted([episode.podcast_id for episode in affected_episodes])
        logger.info("Found podcasts for rss updates: %s", podcast_ids)
        await GenerateRSSTask(db_session=self.db_session).run(*podcast_ids)

    async def _update_episodes(self, episode: Episode, update_data: dict):
        """Updating data for episodes (filtered by source_id and source_type)"""

        filter_kwargs = {
            "source_id": episode.source_id,
            "source_type": episode.source_type,
            "status__ne": Episode.Status.ARCHIVED,
        }
        logger.debug("Episodes update filter: %s | data: %s", filter_kwargs, update_data)
        await Episode.async_update(
            self.db_session, filter_kwargs=filter_kwargs, update_data=update_data
        )

    async def _update_files(self, episode: Episode, update_data: dict):
        """Updating data for stored files"""

        source_url = episode.audio.source_url
        logger.debug("Files update: source_url: %s | data: %s", source_url, update_data)
        await File.async_update(
            self.db_session, filter_kwargs={"source_url": source_url}, update_data=update_data
        )


class UploadedEpisodeTask(DownloadEpisodeTask):
    """
    Allows preparations for already uploaded episodes (such as manually uploaded episodes)
    """

    async def perform_run(self, episode_id: int) -> FinishCode:
        """
        Main operation for downloading, performing and uploading audio to the storage.

        :raise: DownloadingInterrupted (if downloading is broken or unnecessary)
        """

        episode: Episode = await Episode.async_get(self.db_session, id=episode_id)
        logger.info(
            "=== [%s] START performing uploaded episodes: %s ===",
            episode.source_id,
            episode.audio.path,
        )
        remote_size = self.storage.get_file_size(dst_path=episode.audio.path)
        if episode.status == EpisodeStatus.PUBLISHED and remote_size == episode.audio.size:
            raise DownloadingInterrupted(
                code=FinishCode.SKIP, message=f"Episode #{episode_id} already published."
            )

        if remote_size != episode.audio.size:
            raise DownloadingInterrupted(
                code=FinishCode.ERROR,
                message=(
                    f"Performing uploaded file failed: incorrect remote file size: {remote_size}"
                ),
            )

        remote_path = await self._copy_file(episode)
        remote_size = self.storage.get_file_size(os.path.basename(remote_path))

        await episode.update(
            self.db_session,
            status=Episode.Status.PUBLISHED,
            published_at=episode.created_at,
        )
        await episode.audio.update(
            self.db_session,
            path=remote_path,
            size=remote_size,
            available=True,
        )
        await self._update_all_rss(episode.source_id)
        await self.db_session.flush()
        # self._delete_tmp_file(old_path)
        logger.info("=== [%s] DOWNLOADING total finished ===", episode.source_id)
        return FinishCode.OK

    async def _copy_file(self, episode: Episode) -> str:
        """Uploading file to the storage (S3)"""

        logger.info("=== [%s] REMOTE COPYING === ", episode.source_id)
        dst_path = os.path.join(settings.S3_BUCKET_AUDIO_PATH, episode.audio_filename)
        remote_path = podcast_utils.remote_copy_episode(
            src_path=episode.audio.path, dst_path=dst_path, src_file_size=episode.audio.size
        )
        if not remote_path:
            logger.warning("=== [%s] REMOTE COPYING was broken === ")
            await episode.update(self.db_session, status=Episode.Status.ERROR)
            raise DownloadingInterrupted(code=FinishCode.ERROR)

        logger.info(
            "=== [%s] REMOTE COPYING was done (%s -> %s):  === ",
            episode.source_id,
            episode.audio.path,
            remote_path,
        )
        return remote_path

    def _delete_tmp_file(self, old_file_path: str):
        logger.debug("Removing old file %s...", old_file_path)
        self.storage.delete_file(dst_path=old_file_path)
        logger.debug("Removing done for old file %s.", old_file_path)


class DownloadEpisodeImageTask(RQTask):
    """Allows fetching episodes image (cover), prepare them and upload to S3"""

    storage: StorageS3 = None
    MAX_UPLOAD_ATTEMPT = 5

    async def run(self, episode_id: int | None = None) -> int:  # pylint: disable=arguments-differ
        self.storage = StorageS3()

        try:
            code = await self.perform_run(episode_id)
        except Exception as exc:
            logger.exception(
                "Unable to upload episode's image: episode %s | error: %r", episode_id, exc
            )
            return FinishCode.ERROR

        return code.value

    async def perform_run(self, episode_id: int | None) -> FinishCode:
        filter_kwargs = {}
        if episode_id:
            filter_kwargs["id"] = int(episode_id)

        episodes = list(await Episode.async_filter(self.db_session, **filter_kwargs))
        episodes_count = len(episodes)

        for index, episode in enumerate(episodes, start=1):
            logger.info("=== Episode %i from %i ===", index, episodes_count)
            image: File = episode.image
            if image.path.startswith(settings.S3_BUCKET_IMAGES_PATH):
                logger.info("Skip episode %i | image URL: %s", episode.id, episode.image_url)
                continue

            if tmp_path := await self._download_and_crop_image(episode):
                remote_path = await self._upload_cover(episode, tmp_path)
                available = True
                size = get_file_size(tmp_path)
            else:
                remote_path, available, size = "", False, None

            logger.info("Saving new image URL: episode %s | remote %s", episode.id, remote_path)
            await image.update(
                self.db_session,
                path=remote_path,
                available=available,
                public=False,
                size=size,
            )
        return FinishCode.OK

    @staticmethod
    async def _download_and_crop_image(episode: Episode) -> Path | None:
        try:
            tmp_path = await download_content(episode.image.source_url, file_ext="jpg")
        except NotFoundError:
            return None

        ffmpeg_preparation(src_path=tmp_path, ffmpeg_params=["-vf", "scale=600:-1"])
        return tmp_path

    async def _upload_cover(self, episode: Episode, tmp_path: Path) -> str:
        attempt = 1
        while attempt <= self.MAX_UPLOAD_ATTEMPT:
            if remote_path := self.storage.upload_file(
                src_path=str(tmp_path),
                dst_path=settings.S3_BUCKET_EPISODE_IMAGES_PATH,
                filename=Episode.generate_image_name(episode.source_id),
            ):
                return remote_path

            attempt += 1
            await asyncio.sleep(attempt)

        raise MaxAttemptsReached("Couldn't upload cover for episode")
