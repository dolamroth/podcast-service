import asyncio
import json
import logging
from json import JSONDecodeError

import aioredis
import async_timeout
from starlette.websockets import WebSocket

from common.views import BaseHTTPEndpoint, BaseWSEndpoint
from core import settings
from modules.podcast.models import Podcast, Episode
from modules.podcast.schemas import ProgressResponseSchema
from modules.podcast.utils import check_state

logger = logging.getLogger(__name__)


class ProgressAPIView(BaseHTTPEndpoint):
    """
    Temp solution (web-socket for poor) to quick access to downloading process
    (statistic is saved in Redis)
    # TODO: Rewrite this to web-socket
    """

    schema_response = ProgressResponseSchema

    async def get(self, request):
        podcast_items = {
            podcast.id: podcast
            for podcast in await Podcast.async_filter(self.db_session, owner_id=request.user.id)
        }
        episodes = {
            episode.id: episode
            for episode in await Episode.get_in_progress(self.db_session, request.user.id)
        }
        progress = await check_state(episodes.values())

        for progress_item in progress:
            podcast: Podcast = podcast_items.get(progress_item.pop("podcast_id"))
            episode: Episode = episodes.get(progress_item.pop("episode_id"))
            progress_item["episode"] = {
                "id": episode.id,
                "title": episode.title,
                "image_url": episode.image_url,
                "status": episode.status,
            }
            progress_item["podcast"] = {
                "id": podcast.id,
                "name": podcast.name,
                "image_url": podcast.image_url,
            }

        return self._response(data=progress)


class EpisodeInProgressAPIView(BaseHTTPEndpoint):
    """Current downloading progress for requested episode"""

    schema_response = ProgressResponseSchema
    db_model = Episode

    async def get(self, request):
        episode_id = request.path_params["episode_id"]
        episode = await self._get_object(episode_id)
        progress_data = {}
        if episode.status in Episode.PROGRESS_STATUSES:
            if progress := await check_state([episode]):
                progress_data = progress[0]

        progress_data["episode"] = episode
        return self._response(progress_data)


class ProgressWS(BaseWSEndpoint):
    """ Provide updates for episodes progress (storage - Redis) """

    async def _background_handler(self, websocket: WebSocket):
        all_in_progress = [
            episode.id
            for episode in await Episode.get_in_progress(self.db_session, self.user.id)
        ]
        await self._send_progress_for_episodes(websocket, all_in_progress)
        await self._pubsub(websocket)

    async def _send_progress_for_episodes(self, websocket: WebSocket, episode_ids: list[int]):
        progress_data = await self._get_progress_items(episode_ids)
        progress_items = ProgressResponseSchema(many=True).dump(progress_data)
        await websocket.send_json({"progressItems": progress_items})

    async def _pubsub(self, websocket: WebSocket):
        redis = aioredis.Redis(**settings.REDIS)
        psub = redis.pubsub()

        async def reader(channel: aioredis.client.PubSub):
            while True:
                try:
                    async with async_timeout.timeout(1):
                        message = await channel.get_message(ignore_subscribe_messages=True)
                        if message is not None:
                            print(f"(Reader) Message Received: {message}")
                            msg_body = json.loads(message["data"])
                            await self._send_progress_for_episodes(
                                websocket, episode_ids=msg_body["episode_ids"]
                            )
                        await asyncio.sleep(0.01)

                except JSONDecodeError as exc:
                    logger.exception(
                        "Couldn't decode JSON body from pubsub channel: msg: %s | err %r",
                        message, exc
                    )
                except asyncio.TimeoutError:
                    logger.error("Couldn't read message from redis pubsub channel: timeout")

                if self.background_task.cancelled():
                    # TODO: recheck unsubscribe logic
                    break

        async with psub as p:
            await p.subscribe(settings.REDIS_PROGRESS_PUBSUB_CH)
            await reader(p)  # wait for reader to complete
            await p.unsubscribe(settings.REDIS_PROGRESS_PUBSUB_CH)

        # closing all open connections
        await psub.close()

    async def _get_progress_items(self, episode_ids: list[int]) -> list[dict]:
        podcast_items = {
            podcast.id: podcast
            for podcast in await Podcast.async_filter(self.db_session, owner_id=self.user.id)
        }
        episodes = {
            episode.id: episode
            for episode in await Episode.async_filter(self.db_session, id__in=episode_ids)
        }
        progress = await check_state(episodes.values())

        for progress_item in progress:
            podcast: Podcast = podcast_items.get(progress_item.pop("podcast_id"))
            episode: Episode = episodes.get(progress_item.pop("episode_id"))
            progress_item["episode"] = {
                "id": episode.id,
                "title": episode.title,
                "image_url": episode.image_url,
                "status": episode.status,
            }
            progress_item["podcast"] = {
                "id": podcast.id,
                "name": podcast.name,
                "image_url": podcast.image_url,
            }

        return progress
