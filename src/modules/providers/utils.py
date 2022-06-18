import os
import re
import asyncio
import subprocess
import dataclasses
from pathlib import Path
from functools import partial
from contextlib import suppress
from multiprocessing import Process
from typing import Optional, NamedTuple

import youtube_dl
from youtube_dl.utils import YoutubeDLError

from core import settings
from common.utils import get_logger
from common.enums import SourceType, EpisodeStatus
from common.exceptions import InvalidParameterError
from modules.podcast.models import Cookie
from modules.auth.hasher import get_random_hash
from modules.providers.exceptions import FFMPegPreparationError
from modules.podcast.utils import (
    get_file_size,
    episode_process_hook,
    post_processing_process_hook,
)

logger = get_logger(__name__)


class SourceMediaInfo(NamedTuple):
    """Structure of extended information about media source"""

    watch_url: str
    source_id: str
    description: str
    thumbnail_url: str
    title: str
    author: str
    length: int


class SourceConfig(NamedTuple):
    type: SourceType
    regexp: Optional[str] = None
    regexp_playlist: Optional[str] = None
    need_postprocessing: bool = False
    # TODO: are we need to have this field?
    # need_downloading: bool = True


@dataclasses.dataclass
class SourceInfo:
    id: str
    type: SourceType
    url: Optional[str] = None
    cookie: Optional[Cookie] = None


SOURCE_CFG_MAP = {
    SourceType.YOUTUBE: SourceConfig(
        type=SourceType.YOUTUBE,
        regexp=(
            r"^https://(?:www\.)?"
            r"[(?:youtube\.com)|(?:youtu\.be)]+[/watch\?v=|\/]+"
            r"(?P<source_id>[0-9a-zA-Z-_]{11})"
        ),
        regexp_playlist=(
            r"^https://(?:www\.)?youtube\.com/playlist\?list=(?P<source_id>[0-9a-zA-Z-_]+)"
        ),
        need_postprocessing=True,
    ),
    SourceType.YANDEX: SourceConfig(
        type=SourceType.YANDEX,
        regexp=r"https?://music\.yandex\.ru\/[a-z\/0-9]+\/track\/(?P<source_id>[0-9]+)",
        regexp_playlist=r"^https://music\.yandex\.ru/album/(?P<source_id>[0-9a-zA-Z-_]+)",
    ),
    SourceType.UPLOAD: SourceConfig(
        type=SourceType.UPLOAD,
    ),
}


def extract_source_info(source_url: Optional[str] = None, playlist: bool = False) -> SourceInfo:
    """Extracts providers (source) info and finds source ID"""

    if not source_url:
        random_hash = get_random_hash(size=6)
        return SourceInfo(id=f"U-{random_hash}", type=SourceType.UPLOAD)

    for source_type, source_cfg in SOURCE_CFG_MAP.items():
        regexp = source_cfg.regexp if not playlist else source_cfg.regexp_playlist
        if match := (re.match(regexp, source_url) if source_cfg.regexp else None):
            if source_id := match.groupdict().get("source_id"):
                return SourceInfo(id=source_id, url=source_url, type=source_cfg.type)

            logger.error(
                "Couldn't extract source ID: Source link is not correct: %s | source_info: %s",
                source_url,
                source_cfg,
            )

    raise InvalidParameterError(f"Requested domain is not supported now {source_url}")


def download_process_hook(event: dict):
    """
    Allows handling processes of downloading episode's file.
    It is called by `youtube_dl.YoutubeDL`
    """
    total_bytes = event.get("total_bytes") or event.get("total_bytes_estimate", 0)
    episode_process_hook(
        status=EpisodeStatus.DL_EPISODE_DOWNLOADING,
        filename=event["filename"],
        total_bytes=total_bytes,
        processed_bytes=event.get("downloaded_bytes", total_bytes),
    )


def download_audio(source_url: str, filename: str, cookie: Optional[Cookie]) -> Path:
    """
    Download providers video and perform to audio (.mp3) file

    :param source_url: URL to providers video which are needed to download
    :param filename: autogenerated filename for episode
    :param cookie: instance of Cookie for accessing to protected episodes
    :return path to downloaded file
    """
    result_path = settings.TMP_AUDIO_PATH / filename
    params = {
        "format": "bestaudio/best",
        "outtmpl": str(result_path),
        "logger": get_logger("youtube_dl.YoutubeDL"),
        "progress_hooks": [download_process_hook],
        "noprogress": True,
    }
    if cookie:
        params["cookiefile"] = cookie.as_file()

    with youtube_dl.YoutubeDL(params) as ydl:
        ydl.download([source_url])

    return result_path


async def get_source_media_info(source_info: SourceInfo) -> tuple[str, Optional[SourceMediaInfo]]:
    """Allows extract info about providers video from Source (powered by youtube_dl)"""

    logger.info(f"Started fetching data for {source_info.url}")
    loop = asyncio.get_running_loop()
    params = {"logger": logger, "noplaylist": True}
    if source_info.cookie:
        params["cookiefile"] = source_info.cookie.as_file()

    try:
        with youtube_dl.YoutubeDL(params) as ydl:
            extract_info = partial(ydl.extract_info, source_info.url, download=False)
            source_details = await loop.run_in_executor(None, extract_info)

    except YoutubeDLError as error:
        logger.exception(f"ydl.extract_info failed: {source_info.url} ({error})")
        return str(error), None

    youtube_info = SourceMediaInfo(
        title=source_details["title"],
        description=source_details.get("description") or source_details.get("title"),
        watch_url=source_details["webpage_url"],
        source_id=source_details["id"],
        thumbnail_url=source_details["thumbnail"],
        author=source_details.get("uploader") or source_details.get("artist"),
        length=source_details["duration"],
    )
    return "OK", youtube_info


def ffmpeg_preparation(
    src_path: str | Path, ffmpeg_params: list[str] = None, call_process_hook: bool = True
) -> None:
    """
    FFmpeg allows fixing problem with length of audio track
    (in metadata value for this is incorrect, but fact length is fully correct)
    """
    filename = os.path.basename(str(src_path))
    logger.info(f"Start FFMPEG preparations for {filename} === ")
    total_bytes = get_file_size(src_path)
    if call_process_hook:
        episode_process_hook(
            status=EpisodeStatus.DL_EPISODE_POSTPROCESSING,
            filename=filename,
            total_bytes=total_bytes,
            processed_bytes=0,
        )
    tmp_path = os.path.join(settings.TMP_AUDIO_PATH, f"tmp_{filename}")

    logger.info(f"Start SUBPROCESS (filesize watching) for {filename} === ")
    p = Process(
        target=post_processing_process_hook,
        kwargs={"filename": filename, "target_path": tmp_path, "total_bytes": total_bytes},
    )
    p.start()
    try:
        ffmpeg_params = ffmpeg_params or ["-vn", "-acodec", "libmp3lame", "-q:a", "5"]
        completed_proc = subprocess.run(
            ["ffmpeg", "-y", "-i", src_path, *ffmpeg_params, tmp_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=True,
            timeout=settings.FFMPEG_TIMEOUT,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
        episode_process_hook(status=EpisodeStatus.ERROR, filename=filename)
        with suppress(IOError):
            os.remove(tmp_path)

        err_details = f"FFMPEG failed with errors: {err}"
        if stdout := getattr(err, "stdout", ""):
            err_details += f"\n{str(stdout, encoding='utf-8')}"

        p.terminate()
        raise FFMPegPreparationError(err_details)

    p.terminate()
    logger.info(
        "FFMPEG success done preparation for file %s:\n%s",
        filename,
        str(completed_proc.stdout, encoding="utf-8"),
    )

    try:
        assert os.path.exists(tmp_path), f"Prepared file {tmp_path} wasn't created"
        os.remove(src_path)
        os.rename(tmp_path, src_path)
    except (IOError, AssertionError) as err:
        episode_process_hook(status=EpisodeStatus.ERROR, filename=filename)
        raise FFMPegPreparationError(f"Failed to rename/remove tmp file: {err}")

    total_file_size = get_file_size(src_path)
    if call_process_hook:
        episode_process_hook(
            status=EpisodeStatus.DL_EPISODE_POSTPROCESSING,
            filename=filename,
            total_bytes=total_file_size,
            processed_bytes=total_file_size,
        )
    logger.info("FFMPEG Preparation for %s was done", filename)
