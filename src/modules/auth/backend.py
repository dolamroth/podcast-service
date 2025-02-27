from jwt import InvalidTokenError, ExpiredSignatureError
from sqlalchemy.ext.asyncio import AsyncSession

from common.exceptions import (
    AuthenticationFailedError,
    AuthenticationRequiredError,
    PermissionDeniedError,
    SignatureExpiredError,
)
from common.utils import get_logger
from modules.auth.models import User, UserSession
from modules.auth.utils import decode_jwt, TOKEN_TYPE_ACCESS, TOKEN_TYPE_RESET_PASSWORD

logger = get_logger(__name__)


class BaseAuthJWTBackend:
    """Core of authenticate system, based on JWT auth approach"""

    keyword = "Bearer"

    def __init__(self, request, db_session: AsyncSession | None = None):
        self.request = request
        self.db_session: AsyncSession = db_session or request.db_session

    async def authenticate(self) -> tuple[User, str]:
        request = self.request
        auth_header = request.headers.get("Authorization") or request.headers.get("authorization")
        if not auth_header:
            raise AuthenticationRequiredError("Invalid token header. No credentials provided.")

        auth = auth_header.split()
        if len(auth) != 2:
            logger.warning("Trying to authenticate with header %s", auth_header)
            raise AuthenticationFailedError("Invalid token header. Token should be format as JWT.")

        if auth[0] != self.keyword:
            raise AuthenticationFailedError("Invalid token header. Keyword mismatch.")

        user, _, session_id = await self.authenticate_user(jwt_token=auth[1])
        return user, session_id

    async def authenticate_user(
        self,
        jwt_token: str,
        token_type: str = TOKEN_TYPE_ACCESS,
    ) -> tuple[User, dict, str | None]:
        """Allows to find active user by jwt_token"""

        logger.debug("Logging via JWT auth. Got token: %s", jwt_token)
        try:
            jwt_payload = decode_jwt(jwt_token)
        except ExpiredSignatureError as exc:
            logger.debug("JWT signature has been expired for %s token", token_type)
            exception_class = (
                SignatureExpiredError
                if token_type == TOKEN_TYPE_ACCESS
                else AuthenticationFailedError
            )
            raise exception_class("JWT signature has been expired for token") from exc

        except InvalidTokenError as exc:
            msg = "Token could not be decoded: %s"
            logger.exception(msg, exc)
            raise AuthenticationFailedError(msg % (exc,)) from exc

        if jwt_payload["token_type"] != token_type:
            raise AuthenticationFailedError(
                f"Token type '{token_type}' expected, got '{jwt_payload['token_type']}' instead."
            )

        user_id = jwt_payload.get("user_id")
        user = await User.get_active(self.db_session, user_id)
        if not user:
            msg = "Couldn't found active user with id=%s."
            logger.warning(msg, user_id)
            raise AuthenticationFailedError(details=(msg % (user_id,)))

        if token_type == TOKEN_TYPE_RESET_PASSWORD:
            return user, jwt_payload, None

        session_id = jwt_payload.get("session_id")
        if not session_id:
            raise AuthenticationFailedError("Incorrect data in JWT: session_id is missed")

        user_session = await UserSession.async_get(
            self.db_session, public_id=session_id, is_active=True
        )
        if not user_session:
            raise AuthenticationFailedError(
                f"Couldn't found active session: {user_id=} | {session_id=}."
            )

        return user, jwt_payload, session_id


class LoginRequiredAuthBackend(BaseAuthJWTBackend):
    """Each request must have filled `user` attribute"""


class AdminRequiredAuthBackend(BaseAuthJWTBackend):
    """Login-ed used must have `is_superuser` attribute"""

    async def authenticate_user(
        self, jwt_token: str, token_type: str = TOKEN_TYPE_ACCESS
    ) -> tuple[User, dict, str]:
        user, jwt_payload, session_id = await super().authenticate_user(jwt_token)
        if not user.is_superuser:
            raise PermissionDeniedError("You don't have an admin privileges.")

        return user, jwt_payload, session_id
