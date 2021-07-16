import logging
import logging.config

import rq
import sentry_sdk
from redis import Redis
from sentry_sdk.integrations.asgi import SentryAsgiMiddleware
from sentry_sdk.integrations.logging import LoggingIntegration
from sqlalchemy.orm import sessionmaker
from starlette.applications import Starlette
from starlette.middleware import Middleware
from webargs_starlette import WebargsHTTPException

from common.db_utils import make_session_maker
from core import settings
from core.routes import routes
from common.utils import custom_exception_handler
from common.exceptions import BaseApplicationError


exception_handlers = {
    BaseApplicationError: custom_exception_handler,
    WebargsHTTPException: custom_exception_handler,
}


class PodcastApp(Starlette):
    """ Simple adaptation of Starlette APP for podcast-service. Small addons here. """

    rq_queue: rq.Queue
    session_maker: sessionmaker

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.rq_queue = rq.Queue(
            name=settings.RQ_QUEUE_NAME,
            connection=Redis(*settings.REDIS_CON),
            default_timeout=settings.RQ_DEFAULT_TIMEOUT,
        )
        self.session_maker = make_session_maker()


def get_app():
    app = PodcastApp(
        routes=routes,
        exception_handlers=exception_handlers,
        debug=settings.APP_DEBUG,
        middleware=[Middleware(SentryAsgiMiddleware)],
    )
    logging.config.dictConfig(settings.LOGGING)
    if settings.SENTRY_DSN:
        logging_integration = LoggingIntegration(level=logging.INFO, event_level=logging.ERROR)
        sentry_sdk.init(settings.SENTRY_DSN, integrations=[logging_integration])

    return app


# TODO:
#  - tests DB
#  - DEBUG logs
