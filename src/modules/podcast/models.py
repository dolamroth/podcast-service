import enum
import uuid
from datetime import datetime
from hashlib import md5
from urllib.parse import urljoin
from xml.sax.saxutils import escape

from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import relationship

from core import settings
from common.models import ModelMixin
from common.db_utils import EnumTypeColumn
from core.database import ModelBase


class EpisodeStatus(enum.Enum):
    NEW = "new"
    DOWNLOADING = "downloading"
    PUBLISHED = "published"
    ARCHIVED = "archived"
    ERROR = "error"

    DL_PENDING = "pending"
    DL_EPISODE_DOWNLOADING = "episode_downloading"
    DL_EPISODE_POSTPROCESSING = "episode_postprocessing"
    DL_EPISODE_UPLOADING = "episode_uploading"
    DL_COVER_DOWNLOADING = "cover_downloading"
    DL_COVER_UPLOADING = "cover_uploading"

    def __str__(self):
        return self.value


class Podcast(ModelBase, ModelMixin):
    """ Simple schema_request for saving podcast in DB """

    __tablename__ = "podcast_podcasts"

    id = Column(Integer, primary_key=True)
    publish_id = Column(String(length=32), unique=True, nullable=False)
    name = Column(String(length=256), nullable=False)
    description = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    download_automatically = Column(Boolean, default=True)
    rss_link = Column(String(length=128))
    image_url = Column(String(length=512))
    created_by_id = Column(Integer(), ForeignKey("auth_users.id"))

    episodes = relationship("Episode")

    def __str__(self):
        return f'<Podcast #{self.id} "{self.name}">'

    @property
    def safe_image_url(self) -> str:
        image_url = self.image_url
        if not image_url:
            image_url = urljoin(settings.S3_STORAGE_URL, settings.S3_DEFAULT_PODCAST_IMAGE)

        return image_url

    @classmethod
    async def create_first_podcast(cls, db_session: AsyncSession, user_id: int):
        return await Podcast.create(
            db_session,
            publish_id=cls.generate_publish_id(),
            name="Your podcast",
            description=(
                "Add new episode -> wait for downloading -> copy podcast RSS-link "
                "-> past this link to your podcast application -> enjoy".strip()
            ),
            created_by_id=user_id,
        )

    @classmethod
    def generate_publish_id(cls):
        return md5(uuid.uuid4().hex.encode("utf-8")).hexdigest()[::2]


class Episode(ModelBase, ModelMixin):
    """ Simple schema_request for saving episodes in DB """

    __tablename__ = "podcast_episodes"
    Status = EpisodeStatus
    PROGRESS_STATUSES = (Status.DOWNLOADING,)

    id = Column(Integer, primary_key=True)
    source_id = Column(String(length=32), index=True, nullable=False)
    podcast_id = Column(
        Integer, ForeignKey("podcast_podcasts.id", ondelete="CASCADE"), index=True
    )
    title = Column(String(length=256), nullable=False)
    watch_url = Column(String(length=128))
    remote_url = Column(String(length=128))
    image_url = Column(String(length=512))
    length = Column(Integer, default=0)
    description = Column(String)
    file_name = Column(String(length=128))
    file_size = Column(Integer, default=0)
    author = Column(String(length=256))
    status = EnumTypeColumn(Status, impl=String(16), default=Status.NEW, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    published_at = Column(DateTime)
    created_by_id = Column(Integer, ForeignKey("auth_users.id"), index=True)

    class Meta:
        order_by = ("-created_at",)
        db_table = "podcast_episodes"

    def __str__(self):
        return f'<Episode #{self.id} {self.source_id} [{self.status}] "{self.title[:10]}..." >'

    @classmethod
    async def get_in_progress(cls, db_session: AsyncSession, user_id: int):
        """ Return downloading episodes """
        return await cls.async_filter(db_session, status__in=Episode.PROGRESS_STATUSES, created_by_id=user_id)

    @property
    def safe_image_url(self) -> str:
        return escape(self.image_url or "")

    @property
    def content_type(self) -> str:
        file_name = self.file_name or "unknown"
        return f"audio/{file_name.split('.')[-1]}"
