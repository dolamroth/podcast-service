import asyncio
import random
import time
import uuid
from typing import Tuple, Type
from unittest import mock
from unittest.mock import Mock
from hashlib import blake2b

import pytest
from alembic.config import main
from starlette.testclient import TestClient

from core import settings
from modules.auth.models import User
from modules.auth.utils import encode_jwt
from modules.podcast.models import Podcast, Episode
from modules.youtube import utils as youtube_utils
from tests.integration.mocks import (
    MockYoutubeDL,
    MockRedisClient,
    MockS3Client,
    BaseMock,
    MockEpisodeCreator,
    MockRQQueue,
)


def mock_target_class(mock_class: Type[BaseMock], monkeypatch):
    """ Allows to mock any classes (is used as fixture)

    # in conftest.py:
    >>> @pytest.fixture  # noqa
    >>> def mocked_vechicle(monkeypatch) -> MockVehicle:   # noqa
    >>>     yield from mock_target_class(MockVehicle, monkeypatch)   # noqa

    # in test.py:
    >>> def test_something(mocked_sender):
    >>>     mocked_vechicle.run.assert_called
    >>>     mocked_vechicle.target_class.__init__.assert_called
    """

    mock_obj = mock_class()

    def init_method(target_obj=None, *args, **kwargs):
        mock_obj.target_obj = target_obj
        mock_obj.mock_init(*args, **kwargs)

    with mock.patch.object(mock_class.target_class, "__init__", autospec=True) as init:
        init.side_effect = init_method
        for mock_method in mock_obj.get_mocks():
            monkeypatch.setattr(mock_class.target_class, mock_method, getattr(mock_obj, mock_method))

        yield mock_obj

    del mock_obj


def get_user_data() -> Tuple[str, str]:
    return f"u_{uuid.uuid4().hex[:10]}@test.com", "password"


def video_id() -> str:
    """ Generate YouTube-like videoID """
    return blake2b(key=bytes(str(time.time()), encoding="utf-8"), digest_size=6).hexdigest()[:11]


@pytest.fixture()
def user_data() -> Tuple[str, str]:
    return get_user_data()


def get_episode_data(podcast: Podcast = None, creator: User = None) -> dict:
    source_id = video_id()
    episode_data = {
        "source_id": source_id,
        "title": f"episode_{source_id}",
        "watch_url": f"https://www.youtube.com/watch?v={source_id}",
        "length": random.randint(1, 100),
        "description": f"description_{source_id}",
        "image_url": f"image_url_{source_id}",
        "file_name": f"file_name_{source_id}",
        "file_size": random.randint(1, 100),
        "author": None,
        "status": "new",
    }

    if podcast:
        episode_data["podcast_id"] = podcast.id

    if creator:
        episode_data["created_by_id"] = creator.id

    return episode_data


@pytest.fixture
def mocked_youtube(monkeypatch) -> MockYoutubeDL:
    yield from mock_target_class(MockYoutubeDL, monkeypatch)


@pytest.fixture
def mocked_redis(monkeypatch) -> MockRedisClient:
    yield from mock_target_class(MockRedisClient, monkeypatch)


@pytest.fixture
def mocked_s3(monkeypatch) -> MockS3Client:
    yield from mock_target_class(MockS3Client, monkeypatch)


@pytest.fixture
def mocked_episode_creator(monkeypatch) -> MockEpisodeCreator:
    yield from mock_target_class(MockEpisodeCreator, monkeypatch)


@pytest.fixture(autouse=True)
def mocked_rq_queue(monkeypatch) -> MockRQQueue:
    yield from mock_target_class(MockRQQueue, monkeypatch)


@pytest.fixture
def mocked_ffmpeg(monkeypatch) -> Mock:
    mocked_ffmpeg_function = Mock()
    monkeypatch.setattr(youtube_utils, "ffmpeg_preparation", mocked_ffmpeg_function)
    yield mocked_ffmpeg_function
    del mocked_ffmpeg_function


class PodcastTestClient(TestClient):

    def login(self, user: User):
        jwt, _ = encode_jwt({'user_id': user.id})
        self.headers["Authorization"] = f"Bearer {jwt}"


def create_user():
    email, password = get_user_data()
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(User.create(email=email, password=password))


def get_podcast_data(**kwargs):
    uid = uuid.uuid4().hex
    return {
        **{
            'publish_id': uid[:32],
            'name': f"Podcast {uid}",
            'description': f"Description: {uid}",
            'image_url': f"http://link-to-image/{uid}"
        },
        **kwargs
    }


@pytest.fixture
def loop():
    return asyncio.get_event_loop()


@pytest.fixture
def user():
    return create_user()


@pytest.fixture
def podcast_data():
    return get_podcast_data()


@pytest.fixture
def episode_data(podcast):
    return get_episode_data(podcast)


@pytest.fixture
def podcast(podcast_data, user, loop):
    podcast_data["created_by_id"] = user.id
    return loop.run_until_complete(Podcast.create(**podcast_data))


@pytest.fixture
def episode(podcast, user, loop) -> Episode:
    episode_data = get_episode_data(podcast, creator=user)
    return loop.run_until_complete(Episode.create(**episode_data))


@pytest.fixture(autouse=True, scope="session")
def db_migration():
    ini_path = settings.PROJECT_ROOT_DIR / "alembic.ini"
    main(["--raiseerr", f"-c{ini_path}", "upgrade", "head"])


@pytest.fixture(autouse=True, scope="session")
def client() -> PodcastTestClient:
    from core.app import get_app

    with PodcastTestClient(get_app()) as client:
        yield client
