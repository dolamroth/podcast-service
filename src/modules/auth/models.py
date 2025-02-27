import logging
import secrets
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey

from core.database import ModelBase
from common.models import ModelMixin
from modules.auth.hasher import PBKDF2PasswordHasher

logger = logging.getLogger(__name__)


class User(ModelBase, ModelMixin):
    __tablename__ = "auth_users"

    id = Column(Integer, primary_key=True)
    email = Column(String(length=128), index=True, nullable=False, unique=True)
    password = Column(String(length=256), nullable=False)
    is_active = Column(Boolean, default=True)
    is_superuser = Column(Boolean, default=False)

    def __repr__(self):
        return f"<User #{self.id} {self.email}>"

    @classmethod
    def make_password(cls, raw_password: str) -> str:
        hasher = PBKDF2PasswordHasher()
        return hasher.encode(raw_password)

    def verify_password(self, raw_password: str) -> bool:
        hasher = PBKDF2PasswordHasher()
        verified, _ = hasher.verify(raw_password, encoded=str(self.password))
        return verified

    @property
    def is_authenticated(self) -> bool:
        return True

    @property
    def display_name(self) -> str:
        return self.email

    @classmethod
    async def get_active(cls, db_session: AsyncSession, user_id: int) -> "User":
        return await cls.async_get(db_session, id=user_id, is_active=True)


class UserInvite(ModelBase, ModelMixin):
    __tablename__ = "auth_invites"
    TOKEN_MAX_LENGTH = 32

    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("auth_users.id"), unique=True)
    email = Column(String(length=128), unique=True)
    token = Column(String(length=32), unique=True, nullable=False, index=True)
    is_applied = Column(Boolean, default=False, nullable=False)
    expired_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    owner_id = Column(ForeignKey("auth_users.id"), nullable=False)

    def __repr__(self):
        return f"<UserInvite #{self.id} {self.email}>"

    @classmethod
    def generate_token(cls):
        return secrets.token_urlsafe()[: cls.TOKEN_MAX_LENGTH]


class UserSession(ModelBase, ModelMixin):
    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True)
    public_id = Column(String(length=36), index=True, nullable=False, unique=True)
    user_id = Column(ForeignKey("auth_users.id"))
    refresh_token = Column(String(length=512))
    is_active = Column(Boolean, default=True, nullable=False)
    expired_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    refreshed_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<UserSession #{self.id} {self.user_id}>"


class UserIP(ModelBase, ModelMixin):
    __tablename__ = "auth_user_ips"

    id = Column(Integer, primary_key=True)
    ip_address = Column(String(length=16), index=True, nullable=False)
    user_id = Column(ForeignKey("auth_users.id"))
    registered_by = Column(String(length=128), index=True, nullable=False, server_default="")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<UserIP {self.ip_address} user: {self.user_id}>"
