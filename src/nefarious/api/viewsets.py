import os
import logging
from django.conf import settings
from django.contrib.auth.models import User
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework import viewsets, views
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser

from nefarious import websocket
from nefarious.api.mixins import UserReferenceViewSetMixin, BlacklistAndRetryMixin, DestroyTransmissionResultMixin, WebSocketMediaMessageUpdatedMixin
from nefarious.api.permissions import IsAuthenticatedDjangoObjectUser
from nefarious.quality import PROFILES
from nefarious.transmission import get_transmission_client
from nefarious.tmdb import get_tmdb_client
from nefarious.api.serializers import (
    NefariousSettingsSerializer, WatchTVEpisodeSerializer, WatchTVShowSerializer,
    UserSerializer, WatchMovieSerializer, NefariousPartialSettingsSerializer,
    TransmissionTorrentSerializer, WatchTVSeasonSerializer, WatchTVSeasonRequestSerializer,
)
from nefarious.models import NefariousSettings, WatchTVEpisode, WatchTVShow, WatchMovie, WatchTVSeason, WatchTVSeasonRequest
from nefarious.search import MEDIA_TYPE_MOVIE, MEDIA_TYPE_TV, SearchTorrents
from nefarious.tasks import watch_tv_episode_task, watch_tv_show_season_task, watch_movie_task, send_websocket_message_task
from nefarious.utils import (
    trace_torrent_url, swap_jackett_host, is_magnet_url,
    verify_settings_jackett, verify_settings_transmission, verify_settings_tmdb,
    fetch_jackett_indexers, destroy_transmission_result)

CACHE_MINUTE = 60
CACHE_HOUR = CACHE_MINUTE * 60
CACHE_HALF_DAY = CACHE_HOUR * 12
CACHE_DAY = CACHE_HALF_DAY * 2
CACHE_WEEK = CACHE_DAY * 7


class WatchMovieViewSet(WebSocketMediaMessageUpdatedMixin, DestroyTransmissionResultMixin, BlacklistAndRetryMixin, UserReferenceViewSetMixin, viewsets.ModelViewSet):
    queryset = WatchMovie.objects.all()
    serializer_class = WatchMovieSerializer
    filter_fields = ('collected',)
    permission_classes = (IsAuthenticatedDjangoObjectUser,)

    def perform_create(self, serializer):
        super().perform_create(serializer)
        # create a task to download the movie
        watch_movie_task.delay(serializer.instance.id)

    def perform_update(self, serializer):
        super().perform_update(serializer)
        # create a task to download the movie
        watch_movie_task.delay(serializer.instance.id)

    def _watch_media_task(self, watch_media_id: int):
        watch_movie_task.delay(watch_media_id)


class WatchTVShowViewSet(WebSocketMediaMessageUpdatedMixin, UserReferenceViewSetMixin, viewsets.ModelViewSet):
    queryset = WatchTVShow.objects.all()
    serializer_class = WatchTVShowSerializer
    permission_classes = (IsAuthenticatedDjangoObjectUser,)

    def perform_destroy(self, watch_tv_show: WatchTVShow):
        # delete all seasons, season requests, episodes and remove from transmission

        # delete season requests
        WatchTVSeasonRequest.objects.filter(watch_tv_show=watch_tv_show).delete()

        # delete instance and from transmission and send websocket messages
        queries = [WatchTVSeason.objects.filter(watch_tv_show=watch_tv_show), WatchTVEpisode.objects.filter(watch_tv_show=watch_tv_show)]
        for qs in queries:
            for media in qs:
                # send a websocket message that this media was removed
                media_type, data = websocket.get_media_type_and_serialized_watch_media(media)
                send_websocket_message_task.delay(websocket.ACTION_REMOVED, media_type, data)
                # delete from transmission
                destroy_transmission_result(media)
                # delete the media
                media.delete()

        return super().perform_destroy(watch_tv_show)


class WatchTVSeasonViewSet(WebSocketMediaMessageUpdatedMixin, DestroyTransmissionResultMixin, BlacklistAndRetryMixin, UserReferenceViewSetMixin, viewsets.ModelViewSet):
    queryset = WatchTVSeason.objects.all()
    serializer_class = WatchTVSeasonSerializer
    permission_classes = (IsAuthenticatedDjangoObjectUser,)
    filter_fields = ('collected',)

    def _watch_media_task(self, watch_media_id: int):
        """
        blacklist & retry function to queue the new task
        """
        watch_tv_show_season_task.delay(watch_media_id)


class WatchTVSeasonRequestViewSet(WebSocketMediaMessageUpdatedMixin, UserReferenceViewSetMixin, viewsets.ModelViewSet):
    """
    Special viewset to monitor the request of a season, not collection of the season/media itself
    """
    queryset = WatchTVSeasonRequest.objects.all()
    serializer_class = WatchTVSeasonRequestSerializer
    permission_classes = (IsAuthenticatedDjangoObjectUser,)
    filter_fields = ('collected',)

    def perform_create(self, serializer):
        super().perform_create(serializer)

        # save a watch tv season instance to try and download the whole season immediately
        watch_tv_season, _ = WatchTVSeason.objects.get_or_create(
            watch_tv_show=WatchTVShow.objects.get(id=serializer.data['watch_tv_show']),
            season_number=serializer.data['season_number'],
            defaults=dict(
                # add non-unique constraint fields for the default values
                user=self.request.user,
            ),
        )
        # send a websocket message for this new season
        media_type, data = websocket.get_media_type_and_serialized_watch_media(watch_tv_season)
        send_websocket_message_task.delay(websocket.ACTION_UPDATED, media_type, data)

        # delete any individual episodes (including in transmission) now that we're watching the entire season
        for episode in WatchTVEpisode.objects.filter(watch_tv_show=watch_tv_season.watch_tv_show, season_number=watch_tv_season.season_number):
            # send a websocket message for this removed episode
            media_type, data = websocket.get_media_type_and_serialized_watch_media(episode)
            send_websocket_message_task.delay(websocket.ACTION_REMOVED, media_type, data)
            # delete from transmission
            destroy_transmission_result(episode)
            # delete the episode
            episode.delete()

        # create a task to download the whole season (fallback to individual episodes if it fails)
        watch_tv_show_season_task.delay(watch_tv_season.id)

    def perform_destroy(self, watch_tv_season_request: WatchTVSeasonRequest):
        # destroy watch tv season instances as well, including any related torrents in transmission
        query_args = dict(
            watch_tv_show=watch_tv_season_request.watch_tv_show,
            season_number=watch_tv_season_request.season_number,
        )
        for season in WatchTVSeason.objects.filter(**query_args):
            # send a websocket message that this season was removed
            media_type, data = websocket.get_media_type_and_serialized_watch_media(season)
            send_websocket_message_task.delay(websocket.ACTION_REMOVED, media_type, data)
            # delete from transmission
            destroy_transmission_result(season)
            # delete the season
            season.delete()
        return super().perform_destroy(watch_tv_season_request)


class WatchTVEpisodeViewSet(WebSocketMediaMessageUpdatedMixin, DestroyTransmissionResultMixin, BlacklistAndRetryMixin, UserReferenceViewSetMixin, viewsets.ModelViewSet):
    queryset = WatchTVEpisode.objects.all()
    serializer_class = WatchTVEpisodeSerializer
    permission_classes = (IsAuthenticatedDjangoObjectUser,)
    filter_fields = ('collected',)

    def _watch_media_task(self, watch_media_id: int):
        watch_tv_episode_task.delay(watch_media_id)

    def perform_create(self, serializer):
        super().perform_create(serializer)
        # create a task to download the episode
        watch_tv_episode_task.delay(serializer.instance.id)


class SettingsViewSet(viewsets.ModelViewSet):
    queryset = NefariousSettings.objects.all()

    @action(methods=['get'], detail=True, permission_classes=(IsAdminUser,))
    def verify(self, request, pk):
        nefarious_settings = self.queryset.get(id=pk)
        try:
            verify_settings_jackett(nefarious_settings)
            verify_settings_tmdb(nefarious_settings)
            verify_settings_transmission(nefarious_settings)
        except Exception as e:
            raise ValidationError(str(e))
        return Response()

    @action(methods=['get'], detail=True, url_path='verify-jackett-indexers', permission_classes=(IsAdminUser,))
    def verify_jackett_indexers(self, request, pk):
        nefarious_settings = self.queryset.get(id=pk)
        try:
            results = verify_settings_jackett(nefarious_settings)
        except Exception as e:
            raise ValidationError(str(e))
        return Response(results.get('Indexers'))

    def get_serializer_class(self):
        if self.request.user.is_staff:
            return NefariousSettingsSerializer
        return NefariousPartialSettingsSerializer

    @action(methods=['get'], detail=False, url_path='configured-indexers', permission_classes=(IsAdminUser,))
    def configured_indexers(self, request):
        nefarious_settings = NefariousSettings.get()
        return Response(fetch_jackett_indexers(nefarious_settings))


class UserViewSet(viewsets.ModelViewSet):
    permission_classes = (IsAdminUser,)
    queryset = User.objects.all()
    serializer_class = UserSerializer


class CurrentUserViewSet(viewsets.ModelViewSet):
    queryset = User.objects.all()
    serializer_class = UserSerializer

    def get_queryset(self):
        return self.queryset.filter(username=self.request.user.username)


class MediaDetailView(views.APIView):

    @method_decorator(cache_page(CACHE_DAY))
    def get(self, request, media_type, media_id):
        nefarious_settings = NefariousSettings.get()
        tmdb = get_tmdb_client(nefarious_settings)

        params = {
            'language': nefarious_settings.language,
        }

        if media_type == MEDIA_TYPE_MOVIE:
            movie = tmdb.Movies(media_id)
            response = movie.info(**params)
        else:
            tv = tmdb.TV(media_id)
            response = tv.info(**params)
            # omit season "0" -- special episodes
            response['seasons'] = [season for season in response['seasons'] if season['season_number'] > 0]
            for season in response['seasons']:
                seasons_request = tmdb.TV_Seasons(response['id'], season['season_number'])
                seasons = seasons_request.info(**params)
                season['episodes'] = seasons['episodes']

        return Response(response)


class SearchMediaView(views.APIView):

    @method_decorator(cache_page(CACHE_DAY))
    def get(self, request):
        media_type = request.query_params.get('media_type', MEDIA_TYPE_TV)
        assert media_type in [MEDIA_TYPE_TV, MEDIA_TYPE_MOVIE]

        nefarious_settings = NefariousSettings.get()

        # prepare query
        tmdb = get_tmdb_client(nefarious_settings)
        query = request.query_params.get('q')

        params = {
            'query': query,
            'language': nefarious_settings.language,
        }

        # search for media
        search = tmdb.Search()

        if media_type == MEDIA_TYPE_MOVIE:
            results = search.movie(**params)
        else:
            results = search.tv(**params)

        return Response(results)


class SearchSimilarMediaView(views.APIView):

    @method_decorator(cache_page(CACHE_DAY))
    def get(self, request):
        media_type = request.query_params.get('media_type', MEDIA_TYPE_TV)
        assert media_type in [MEDIA_TYPE_TV, MEDIA_TYPE_MOVIE]

        if 'tmdb_media_id' not in request.query_params:
            raise ValidationError({'tmdb_media_id': ['required parameter']})

        nefarious_settings = NefariousSettings.get()

        params = {
            'language': nefarious_settings.language,
        }

        # prepare query
        tmdb = get_tmdb_client(nefarious_settings)
        tmdb_media_id = request.query_params.get('tmdb_media_id')

        # search for media
        if media_type == MEDIA_TYPE_MOVIE:
            similar_results = tmdb.Movies(id=tmdb_media_id).similar_movies(**params)
        else:
            similar_results = tmdb.TV(id=tmdb_media_id).similar(**params)

        return Response(similar_results)


class SearchRecommendedMediaView(views.APIView):

    @method_decorator(cache_page(CACHE_DAY))
    def get(self, request):
        media_type = request.query_params.get('media_type', MEDIA_TYPE_TV)
        assert media_type in [MEDIA_TYPE_TV, MEDIA_TYPE_MOVIE]

        if 'tmdb_media_id' not in request.query_params:
            raise ValidationError({'tmdb_media_id': ['required parameter']})

        nefarious_settings = NefariousSettings.get()

        params = {
            'language': nefarious_settings.language,
        }

        # prepare query
        tmdb = get_tmdb_client(nefarious_settings)
        tmdb_media_id = request.query_params.get('tmdb_media_id')

        # search for media
        if media_type == MEDIA_TYPE_MOVIE:
            similar_results = tmdb.Movies(id=tmdb_media_id).recommendations(**params)
        else:
            similar_results = tmdb.TV(id=tmdb_media_id).recommendations(**params)

        return Response(similar_results)


class SearchTorrentsView(views.APIView):

    @method_decorator(cache_page(CACHE_HALF_DAY))
    def get(self, request):
        query = request.query_params.get('q')
        media_type = request.query_params.get('media_type', MEDIA_TYPE_MOVIE)
        search = SearchTorrents(media_type, query)
        if not search.ok:
            return Response({'error': search.error_content}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        return Response(search.results)


class DownloadTorrentsView(views.APIView):
    permission_classes = (IsAdminUser,)

    def post(self, request):
        result = {
            'success': True,
        }
        nefarious_settings = NefariousSettings.get()

        tmdb_media = request.data.get('tmdb_media', {})
        torrent_info = request.data.get('torrent', {})
        torrent_url = torrent_info.get('MagnetUri') or torrent_info.get('Link')

        if not torrent_url:
            return Response({'success': False, 'error': 'Missing torrent link'})

        media_type = request.data.get('media_type', MEDIA_TYPE_TV)

        # validate tv
        if media_type == MEDIA_TYPE_TV:
            if 'season_number' not in request.data:
                return Response({'success': False, 'error': 'Missing season_number'})

        if not is_magnet_url(torrent_url):
            torrent_url = swap_jackett_host(torrent_url, nefarious_settings)

        try:
            torrent_url = trace_torrent_url(torrent_url)
        except Exception as e:
            return Response({'success': False, 'error': 'An unknown error occurred', 'error_detail': str(e)})

        logging.info('adding torrent: {}'.format(torrent_url))

        # add torrent
        transmission_client = get_transmission_client(nefarious_settings)
        transmission_session = transmission_client.session_stats()

        tmdb = get_tmdb_client(nefarious_settings)

        # set download paths and associate torrent with watch instance
        if media_type == MEDIA_TYPE_MOVIE:
            tmdb_request = tmdb.Movies(tmdb_media['id'])
            tmdb_movie = tmdb_request.info()
            watch_media = WatchMovie(
                user=request.user,
                tmdb_movie_id=tmdb_movie['id'],
                name=tmdb_movie['title'],
                poster_image_url=nefarious_settings.get_tmdb_poster_url(tmdb_movie['poster_path']),
            )
            watch_media.save()
            download_dir = os.path.join(
                transmission_session.download_dir, nefarious_settings.transmission_movie_download_dir.lstrip('/'))
            result['watch_movie'] = WatchMovieSerializer(watch_media).data
        else:
            tmdb_request = tmdb.TV(tmdb_media['id'])
            tmdb_show = tmdb_request.info()

            watch_tv_show, _ = WatchTVShow.objects.get_or_create(
                tmdb_show_id=tmdb_show['id'],
                defaults=dict(
                    user=request.user,
                    name=tmdb_show['name'],
                    poster_image_url=nefarious_settings.get_tmdb_poster_url(tmdb_show['poster_path']),
                )
            )

            result['watch_tv_show'] = WatchTVShowSerializer(watch_tv_show).data

            # single episode
            if 'episode_number' in request.data:
                tmdb_request = tmdb.TV_Episodes(tmdb_media['id'], request.data['season_number'], request.data['episode_number'])
                tmdb_episode = tmdb_request.info()
                watch_media = WatchTVEpisode(
                    user=request.user,
                    watch_tv_show=watch_tv_show,
                    tmdb_episode_id=tmdb_episode['id'],
                    season_number=request.data['season_number'],
                    episode_number=request.data['episode_number'],
                )
                watch_media.save()
                result['watch_tv_episode'] = WatchTVEpisodeSerializer(watch_media).data
            # entire season
            else:
                # create the season request
                watch_tv_season_request, _ = WatchTVSeasonRequest.objects.get_or_create(
                    watch_tv_show=watch_tv_show,
                    season_number=request.data['season_number'],
                    defaults=dict(
                        user=request.user,
                        collected=True,  # set collected since we're directly downloading a torrent
                    ),
                )
                # create the actual watch season instance
                watch_media = WatchTVSeason(
                    user=request.user,
                    watch_tv_show=watch_tv_show,
                    season_number=request.data['season_number'],
                )
                watch_media.save()

                # return the season request vs the watch instance
                result['watch_tv_season_request'] = WatchTVSeasonRequestSerializer(watch_tv_season_request).data

            download_dir = os.path.join(
                transmission_session.download_dir, nefarious_settings.transmission_tv_download_dir.lstrip('/'))

        torrent = transmission_client.add_torrent(
            torrent_url,
            paused=settings.DEBUG,
            download_dir=download_dir,
        )
        watch_media.transmission_torrent_hash = torrent.hashString
        watch_media.save()

        return Response(result)


class CurrentTorrentsView(views.APIView):

    def get(self, request):
        nefarious_settings = NefariousSettings.get()
        transmission_client = get_transmission_client(nefarious_settings)

        watch_movies = request.query_params.getlist('watch_movies', [])
        watch_tv_shows = request.query_params.getlist('watch_tv_shows', [])

        results = []
        querysets = []

        # movies
        if watch_movies:
            querysets.append(
                WatchMovie.objects.filter(id__in=watch_movies))
        # tv shows
        if watch_tv_shows:
            querysets.append(
                WatchTVEpisode.objects.filter(watch_tv_show__id__in=watch_tv_shows))
            querysets.append(
                WatchTVSeason.objects.filter(watch_tv_show__id__in=watch_tv_shows))

        for qs in querysets:

            for media in qs:

                if isinstance(media, WatchTVSeason):
                    media_serializer = WatchTVSeasonSerializer
                elif isinstance(media, WatchTVEpisode):
                    media_serializer = WatchTVEpisodeSerializer
                else:
                    media_serializer = WatchMovieSerializer

                result = {
                    'watchMedia': media_serializer(media).data,
                }

                if media.transmission_torrent_hash:

                    try:
                        torrent = transmission_client.get_torrent(media.transmission_torrent_hash)
                    except (KeyError, ValueError):  # torrent no longer exists or was invalid
                        pass
                    except Exception as e:
                        logging.error(str(e))
                        raise e
                    else:
                        result['torrent'] = TransmissionTorrentSerializer(torrent).data

                results.append(result)

        return Response(results)


class DiscoverMediaView(views.APIView):

    @method_decorator(cache_page(CACHE_WEEK))
    def get(self, request, media_type):
        assert media_type in [MEDIA_TYPE_TV, MEDIA_TYPE_MOVIE]

        nefarious_settings = NefariousSettings.get()

        # prepare query
        tmdb = get_tmdb_client(nefarious_settings)
        args = request.query_params.copy()
        args['language'] = nefarious_settings.language

        discover = tmdb.Discover()

        if media_type == MEDIA_TYPE_MOVIE:
            results = discover.movie(**args)
        else:
            results = discover.tv(**args)

        return Response(results)


class GenresView(views.APIView):

    @method_decorator(cache_page(CACHE_WEEK))
    def get(self, request, media_type):
        assert media_type in [MEDIA_TYPE_TV, MEDIA_TYPE_MOVIE]

        nefarious_settings = NefariousSettings.get()

        # prepare query
        tmdb = get_tmdb_client(nefarious_settings)
        args = request.query_params.copy()
        args['language'] = nefarious_settings.language

        genres = tmdb.Genres()

        if media_type == MEDIA_TYPE_MOVIE:
            results = genres.movie_list(**args)
        else:
            results = genres.tv_list(**args)

        return Response(results)


class VideosView(views.APIView):

    @method_decorator(cache_page(CACHE_DAY))
    def get(self, request, media_type, media_id):
        assert media_type in [MEDIA_TYPE_TV, MEDIA_TYPE_MOVIE]

        nefarious_settings = NefariousSettings.get()

        # prepare query
        tmdb = get_tmdb_client(nefarious_settings)

        if media_type == MEDIA_TYPE_MOVIE:
            result = tmdb.Movies(media_id)
        else:
            result = tmdb.TV(media_id)

        return Response(result.videos())


class QualityProfilesView(views.APIView):

    def get(self, request):
        return Response({'profiles': [p.name for p in PROFILES]})
