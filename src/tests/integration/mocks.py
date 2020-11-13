import asyncio
import time
from hashlib import blake2b
from unittest.mock import Mock

import rq
from youtube_dl import YoutubeDL

from common.redis import RedisClient
from common.storage import StorageS3
from modules.podcast.episodes import EpisodeCreator


class BaseMock:
    """ Base class for class mocking

    # users class
    >>> class Vehicle:
    >>>    def run(self): ...

    # mock class
    >>> class MockVehicle(BaseMock):
    >>>     target_class = Vehicle
    >>>     def __init__(self):
    >>>         self.run = Mock(return_value=None)  # noqa

    """

    @property
    def target_class(self):
        raise NotImplementedError

    def get_mocks(self):
        return [attr for attr, val in self.__dict__.items() if callable(val)]

    @staticmethod
    def async_return(result):
        f = asyncio.Future()
        f.set_result(result)
        return f


class MockYoutube(BaseMock):
    target_class = YoutubeDL
    watch_url: str = None
    video_id: str = None
    description = "Test youtube video description"
    thumbnail_url = "http://path.to-image.com"
    title = "Test youtube video"
    author = "Test author"
    length = 110

    def __init__(self):
        self.video_id = blake2b(
            key=bytes(str(time.time()), encoding="utf-8"), digest_size=6
        ).hexdigest()[:11]
        self.watch_url = f"https://www.youtube.com/watch?v={self.video_id}"
        self.extract_info = Mock(return_value=self.info)
        self.download = Mock()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        ...

    @property
    def info(self, *_, **__):
        return {
            "id": self.video_id,
            "title": self.title,
            "description": self.description,
            "webpage_url": self.watch_url,
            "thumbnail": self.thumbnail_url,
            "uploader": self.author,
            "duration": self.length,
        }


class MockRedisClient(BaseMock):
    target_class = RedisClient

    def __init__(self, content=None):
        self._content = content or {}
        self.async_get_many = Mock(return_value=self.async_return(self._content))


class MockS3Client(BaseMock):
    target_class = StorageS3
    CODE_OK = 0

    def __init__(self):
        self.upload_file = Mock(return_value="http://test.com/uploaded")
        self.delete_file = Mock(return_value=self.CODE_OK)
        self.get_file_size = Mock(return_value=0)
        self.get_file_info = Mock(return_value={})
        self.delete_files_async = Mock(return_value=self.async_return(self.CODE_OK))


class MockEpisodeCreator(BaseMock):
    target_class = EpisodeCreator

    def __init__(self):
        self.create = Mock(return_value=self.async_return(None))


class MockRQQueue(BaseMock):
    target_class = rq.Queue

    def __init__(self):
        self.enqueue = Mock(return_value=None)
