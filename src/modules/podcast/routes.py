from starlette.routing import Route, WebSocketRoute
from modules.podcast import views

# fmt:off
routes = [
    Route("/progress/", views.ProgressAPIView),
    Route("/playlist/", views.PlayListAPIView),
    Route("/podcasts/", views.PodcastListCreateAPIView),
    Route("/podcasts/{podcast_id:int}/", views.PodcastRUDAPIView),
    Route("/podcasts/{podcast_id:int}/upload-image/", views.PodcastUploadImageAPIView),
    Route("/podcasts/{podcast_id:int}/episodes/", views.EpisodeListCreateAPIView),
    Route("/podcasts/{podcast_id:int}/episodes/uploaded/", views.UploadedEpisodesAPIView),
    Route("/podcasts/{podcast_id:int}/episodes/uploaded/{hash:str}/", views.UploadedEpisodesAPIView),
    Route("/podcasts/{podcast_id:int}/generate-rss/", views.PodcastGenerateRSSAPIView),
    # episodes
    Route("/episodes/", views.EpisodeListCreateAPIView),
    Route("/episodes/{episode_id:int}/", views.EpisodeRUDAPIView),
    Route("/episodes/{episode_id:int}/download/", views.EpisodeDownloadAPIView),
    # TODO: remove this endpoint after fully realized WS logic
    Route("/episodes/{episode_id:int}/progress/", views.EpisodeInProgressAPIView),
    # cookies
    Route("/cookies/", views.CookieListCreateAPIView),
    Route("/cookies/{cookie_id:int}/", views.CookieRDAPIView),
]

ws_routes = [
    # TODO: add tests for WS-progress logic
    WebSocketRoute("/progress/", views.ProgressWS)
]
# fmt:on
