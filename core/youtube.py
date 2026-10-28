import os
import json
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher

import requests
from requests.sessions import Session

from tqdm import tqdm
from ytmusicapi import YTMusic, setup_oauth
from ytmusicapi.exceptions import YTMusicUserError
from typing import List, Tuple, Optional
from .track import Track
from .podcast import Podcast
from .playlist import Playlist
from cli.debug import get_logger, redact_headers

logger = get_logger("youtube")

_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_JUNK_HEADERS = {"decoded", "content-encoding", "music.youtube.com"}


class LoggingSession(Session):
    """Session, логирующая методы/URL/статус/классификацию тела HTTP-ответа.

    Автоматически повторяет запросы при сетевых сбоях уровня TLS/соединения
    (Google периодически рвёт соединения клиентов, не похожих на браузер).
    """

    _max_retries = 4

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_status: Optional[int] = None
        self.last_url: Optional[str] = None
        self.last_text: Optional[str] = None

    def request(self, *args, **kwargs):
        method = args[0] if args else kwargs.get("method", "?")
        url = args[1] if len(args) > 1 else kwargs.get("url", "?")
        headers = kwargs.get("headers") or {}
        logger.debug("HTTP %s %s headers=%s", method, url, redact_headers(dict(headers)))
        last_exc = None
        for attempt in range(self._max_retries):
            try:
                response = super().request(*args, **kwargs)
                self._log_response(method, url, response)
                return response
            except (requests.exceptions.SSLError,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.Timeout) as e:
                last_exc = e
                if attempt < self._max_retries - 1:
                    logger.warning(
                        "HTTP %s %s сетевой сбой (попытка %d/%d): %s",
                        method, url, attempt + 1, self._max_retries, str(e)[:160],
                    )
                    time.sleep(0.7 * (attempt + 1))
        raise last_exc

    def _log_response(self, method: str, url: str, response) -> None:
        body = getattr(response, "text", "") or ""
        kind = "json"
        if not body.strip():
            kind = "empty"
        elif body.lstrip().startswith("<"):
            kind = "html"
        elif body.lstrip().startswith("{"):
            kind = "json"
        limit = 2000 if response.status_code >= 400 else 300
        self.last_status = response.status_code
        self.last_url = url
        self.last_text = response.text
        logger.info("HTTP %s %s -> %s [%s] body=%d chars", method, url, response.status_code, kind, len(body))
        logger.debug("  body.start: %s", body[:limit].replace("\n", " "))


def sanitize_browser_headers(data: dict) -> dict:
    """Убирает из сохранённых браузерных заголовков артефакты ручного копирования
    (псевдо-заголовки HTTP/2, 'Decoded', 'content-encoding' и т.п.), из-за которых
    Google закрывает соединение (SSL EOF) или отвечает 400 Bad Request."""
    clean = {}
    removed = []
    for raw_key, value in data.items():
        key = str(raw_key)
        low = key.lower()
        if (low in _JUNK_HEADERS or not _TOKEN_RE.fullmatch(key)
                or "?" in key or "/" in key or " " in key or key.startswith(":")):
            removed.append(key)
            continue
        clean[key] = value
    if removed:
        logger.warning(
            "browser headers: удалены мусорные заголовки (%d): %s",
            len(removed), sorted(removed),
        )
    return clean


def _normalize(text: str) -> str:
    text = unicodedata.normalize('NFKD', text.casefold().replace('ø', 'o'))
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return ' '.join(re.sub(r'[^\w]+', ' ', text).split())


def _similarity(a: str, b: str) -> float:
    a, b = _normalize(a), _normalize(b)
    if a and b and (f' {a} ' in f' {b} ' or f' {b} ' in f' {a} '):
        return max(SequenceMatcher(None, a, b).ratio(), 0.9)  # "USS" vs "USS (Ubiquitous Synergy Seeker)"
    return SequenceMatcher(None, a, b).ratio()


class YoutubeImporter:
    def __init__(self, token_path: str, client_secrets_path: str = None):
        """
        Initialize YouTube Music importer.

        Args:
            token_path: Path to credentials file (OAuth JSON or browser headers JSON)
            client_secrets_path: Path to Google OAuth client secrets (optional, for OAuth auth)
        """
        self.token_path = token_path
        self.client_secrets_path = client_secrets_path

        if os.path.exists(token_path):
            mtime = os.path.getmtime(token_path)
            age_hours = (time.time() - mtime) / 3600
            logger.info("credentials file %s exists, size=%d, age=%.1fh", token_path, os.path.getsize(token_path), age_hours)
            with open(token_path, 'r') as f:
                try:
                    data = json.load(f)
                except json.JSONDecodeError as e:
                    logger.error("credentials file %s is not valid JSON: %s", token_path, e)
                    raise ValueError(f"Invalid JSON in {token_path}")

            if 'cookie' in data or 'Cookie' in data or 'x-origin' in data:
                self.auth_type = 'browser'
                clean_headers = sanitize_browser_headers(data)
                keys = {k.lower(): v for k, v in clean_headers.items()}
                logger.info("detected auth_type=browser; header keys: %s", sorted(redact_headers(clean_headers).keys()))
                if not keys.get('authorization') or not keys.get('cookie'):
                    logger.error("browser credentials missing required headers: authorization=%s cookie=%s",
                                 bool(keys.get('authorization')), bool(keys.get('cookie')))
                    raise ValueError(
                        "Файл авторизации не содержит обязательных браузерных заголовков "
                        "(authorization и cookie). Скопируй ВСЕ Request Headers именно "
                        "POST-запроса browse из DevTools (Network → browse → Request Headers), "
                        "как описано в README."
                    )
                try:
                    self.ytmusic = YTMusic(clean_headers, requests_session=LoggingSession())
                except YTMusicUserError as e:
                    logger.error("YTMusic rejected browser credentials: %s", e)
                    raise ValueError(
                        "Не удалось распознать файл авторизации как браузерный. "
                        "Скопируй ВСЕ Request Headers именно POST-запроса browse из DevTools, "
                        "включая authorization (SAPISIDHASH ...) и cookie."
                    ) from e
                cookie_value = str(keys.get("cookie") or "")
                auth_value = str(keys.get("authorization") or "")
                if "SAPISID" not in cookie_value:
                    logger.warning(
                        "browser cookies не содержат SAPISID (%d chars) — похоже на гостевую "
                        "или неполную сессию. Войди в аккаунт и скопируй headers заново.",
                        len(cookie_value),
                    )
                if not auth_value.startswith("SAPISIDHASH"):
                    logger.warning(
                        "authorization не начинается с SAPISIDHASH (%d chars, starts=%r) — "
                        "сессия может быть невалидной.", len(auth_value), auth_value[:12],
                    )
            else:
                self.auth_type = 'oauth'
                logger.warning("no browser headers (cookie/x-origin) in %s, treating as oauth", token_path)
                if not client_secrets_path or not os.path.exists(client_secrets_path):
                    raise FileNotFoundError(
                        "OAuth token found but client secrets file required. "
                        "Use --client-secrets or switch to browser authentication."
                    )
                self._init_oauth(token_path, client_secrets_path)
        else:
            logger.warning("credentials file not found: %s", token_path)
            if client_secrets_path and os.path.exists(client_secrets_path):
                self.auth_type = 'oauth'
                self._init_oauth(token_path, client_secrets_path)
            else:
                raise FileNotFoundError(
                    f"Credentials file not found: {token_path}\n"
                    "Either:\n"
                    "  1. Create browser auth: ytmusicapi browser --file browser.json\n"
                    "  2. Use OAuth with --client-secrets option"
                )

    def _init_oauth(self, token_path: str, client_secrets_path: str):
        """Initialize OAuth authentication."""
        from ytmusicapi.auth.oauth import OAuthCredentials

        with open(client_secrets_path, 'r') as f:
            secrets = json.load(f)['installed']

        self.oauth_credentials = OAuthCredentials(secrets['client_id'], secrets['client_secret'])

        if not os.path.exists(token_path):
            token = setup_oauth(secrets['client_id'], secrets['client_secret']).as_json()
            with open(token_path, 'w') as f:
                f.write(token)

        self.ytmusic = YTMusic(token_path, oauth_credentials=self.oauth_credentials)

    def verify_auth(self) -> None:
        """Диагностика: пробный запрос к YouTube Music, ничего не изменяет.

        Логирует результат: ok — авторизация рабочая; ошибка — невалидная
        сессия (с деталями ответа сервера).
        """
        logger.info("verify_auth: probing YTMusic API (get_liked_songs limit=1)...")
        session = getattr(self.ytmusic, "_session", None)
        try:
            result = self.ytmusic.get_liked_songs(limit=1)
            tracks = result.get("tracks", []) if result else []
            logger.info("verify_auth: OK, got %d liked track(s)", len(tracks))
        except Exception as e:
            logger.error("verify_auth: FAILED: %s", e)
            status = getattr(session, "last_status", None)
            text = getattr(session, "last_text", "") or ""
            msg = str(e)

            if "SSLError" in msg or "EOF occurred" in msg or "UNEXPECTED_EOF" in msg:
                logger.error(
                    "verify_auth: соединение с сервером оборвано на уровне TLS (SSL EOF). "
                    "Чаще всего это означает, что в браузерных заголовках есть мусор "
                    "(например 'Decoded') или сессия устарела. Переустанови авторизацию: "
                    "меню → Настроить авторизацию YouTube Music → Автоматически через браузер."
                )
            elif "twoColumnBrowseResultsRenderer" in msg:
                logger.error(
                    "verify_auth: сервер вернул гостевую страницу вместо данных аккаунта — "
                    "куки не распознаны как авторизованные. Сессия истекла, заголовки "
                    "скопированы до входа в аккаунт или на другом устройстве/сети. "
                    "Перелогинься в браузере и пересними авторизацию (автоматический способ)."
                )
            elif status:
                if status == 400:
                    logger.error(
                        "verify_auth: сервер вернул 400 Bad Request — запрос отвергнут. "
                        "Причины: мусорные заголовки, сессия браузера устарела, заголовки "
                        "скопированы не целиком или с другого аккаунта/устройства. "
                        "Переустанови авторизацию заново (меню → Настроить авторизацию "
                        "YouTube Music), войди в аккаунт свежим и скопируй ВСЕ Request "
                        "Headers именно POST-запроса browse."
                    )
                elif status in (401, 403):
                    logger.error(
                        "verify_auth: сервер вернул %s — авторизация недействительна. "
                        "Сессия истекла или authorization/cookie не совпадают. "
                        "Переустанови авторизацию заново.", status
                    )
                else:
                    logger.error("verify_auth: HTTP status=%s url=%s", status, getattr(session, "last_url", "?"))
            else:
                logger.error("verify_auth: авторизация не работает — сессия истекла или headers невалидны.")
                logger.error("verify_auth: переустанови авторизацию: меню → Настроить авторизацию YouTube Music.")
            raise

    def _search_track(self, track: Track, idx: int) -> Tuple[int, Track, Optional[str], Optional[str]]:
        """
        Search for a track on YouTube Music.
        Returns (index, track, videoId or None, error or None).
        """
        query = f'{track.artist} {track.name}'

        try:
            results = self.ytmusic.search(query, filter='songs')
        except Exception as e:
            return (idx, track, None, str(e))

        if not results:
            return (idx, track, None, 'not_found')

        result = self._get_best_result(results, track)
        if result is None:
            return (idx, track, None, 'not_found')
        return (idx, track, result['videoId'], None)
    
    def _search_podcast(self, podcast: Podcast, idx: int) -> Tuple[int, Podcast, Optional[str], Optional[str]]:
        """
        Search for a podcast on YouTube Music.
        Returns (index, podcast, browseId or None, error or None).
        """
        query = f'{podcast.name}'

        try:
            results = self.ytmusic.search(query)
        except Exception as e:
            return (idx, podcast, None, str(e))

        # Since we search by the exact name of the podcast,
        # we expect it to be the first result, and consider
        # the search failed if it's not
        search_result = results[0]

        if search_result.get('resultType') != 'podcast':
            return (idx, podcast, None, 'not_found')

        browse_id = search_result.get('browseId')
        playlist_id = browse_id.replace('MPSP', '', 1) # We need to remove the prefix for podcasts
        return (idx, podcast, playlist_id, None)

    def _like_track(self, track: Track, video_id: str) -> Tuple[Track, bool, Optional[str]]:
        """Like a single track. Returns (track, success, error)."""
        try:
            self.ytmusic.rate_song(video_id, 'LIKE')
            return (track, True, None)
        except Exception as e:
            return (track, False, str(e))
        
    def _like_podcast(self, podcast: Podcast, playlist_id: str) -> Tuple[Podcast, bool, Optional[str]]:
        """Like a single podcast. Returns (podcast, success, error)."""
        try:
            self.ytmusic.rate_playlist(playlist_id, 'LIKE')
            return (podcast, True, None)
        except Exception as e:
            return (podcast, False, str(e))

    def _create_playlist(self, playlist: Playlist, max_workers: int = 5) -> Tuple[List[Track], List[Track]]:
        """
        Creates a playlist.

        Args:
            playlist: Playlist to create
            max_workers: Number of parallel workers (default 5). Used for track search only
        
        Returns:
            Tuple[List[Track], List[Track]]: A list of not found tracks and a list of tracks that encountered during search
        """
        not_found = []
        errors = []
        # Этап 1: Параллельный поиск
        search_results = {}  # idx -> (track, videoId, error)
        tracks = playlist.tracks
        with tqdm(total=len(tracks), desc='Search for tracks in playlist') as pbar:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(self._search_track, track, idx): idx
                        for idx, track in enumerate(tracks)}

                for future in as_completed(futures):
                    try:
                        idx, track, video_id, error = future.result()
                        search_results[idx] = (track, video_id, error)
                        pbar.set_postfix_str(f'{track.artist} - {track.name}'[:40])
                    except Exception as e:
                        idx = futures[future]
                        search_results[idx] = (tracks[idx], None, str(e))
                    pbar.update(1)

        # Собираем треки для плейлиста
        tracks_to_add = []  # (idx, track, video_id)
        for idx in range(len(tracks)):
            track, video_id, error = search_results[idx]

            if error == 'not_found' or not video_id:
                not_found.append(track)
            elif error:
                errors.append(track)
            else:
                tracks_to_add.append(video_id)
        
        self.ytmusic.create_playlist(playlist.title, playlist.description or "", video_ids=tracks_to_add)
        
        return (not_found, errors)

    def import_liked_tracks(self, tracks: List[Track], max_workers: int = 5, keep_order: bool = True) -> Tuple[List[Track], List[Track]]:
        """
        Import tracks to YouTube Music.

        Args:
            tracks: List of tracks to import
            max_workers: Number of parallel workers (default 5)
            keep_order: If True, likes are added sequentially to preserve order (slower).
                       If False, likes are added in parallel (faster, random order).
        """
        not_found: List[Track] = []
        errors: List[Track] = []

        # Этап 1: Параллельный поиск
        search_results = {}  # idx -> (track, videoId, error)

        print("Поиск треков...")
        with tqdm(total=len(tracks), desc='Search') as pbar:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(self._search_track, track, idx): idx
                          for idx, track in enumerate(tracks)}

                for future in as_completed(futures):
                    try:
                        idx, track, video_id, error = future.result()
                        search_results[idx] = (track, video_id, error)
                        pbar.set_postfix_str(f'{track.artist} - {track.name}'[:40])
                    except Exception as e:
                        idx = futures[future]
                        search_results[idx] = (tracks[idx], None, str(e))
                    pbar.update(1)

        # Собираем треки для лайков
        tracks_to_like = []  # (idx, track, video_id)
        for idx in range(len(tracks)):
            track, video_id, error = search_results[idx]

            if error == 'not_found' or not video_id:
                not_found.append(track)
            elif error:
                errors.append(track)
            else:
                tracks_to_like.append((idx, track, video_id))

        # Этап 2: Добавление лайков
        print("Добавление лайков...")

        if keep_order:
            # Последовательное добавление для сохранения порядка
            with tqdm(total=len(tracks_to_like), desc='Like') as pbar:
                for idx, track, video_id in tracks_to_like:
                    try:
                        self.ytmusic.rate_song(video_id, 'LIKE')
                        pbar.set_postfix_str(f'{track.artist} - {track.name}'[:40])
                    except Exception as e:
                        errors.append(track)
                        pbar.write(f'Like error: {track.artist} - {track.name}: {e}')
                    pbar.update(1)
        else:
            # Параллельное добавление (быстрее, но порядок случайный)
            with tqdm(total=len(tracks_to_like), desc='Like') as pbar:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {executor.submit(self._like_track, track, video_id): track
                              for idx, track, video_id in tracks_to_like}

                    for future in as_completed(futures):
                        track = futures[future]
                        try:
                            _, success, error = future.result()
                            if not success:
                                errors.append(track)
                                pbar.write(f'Like error: {track.artist} - {track.name}: {error}')
                            pbar.set_postfix_str(f'{track.artist} - {track.name}'[:40])
                        except Exception as e:
                            errors.append(track)
                            pbar.write(f'Like error: {track.artist} - {track.name}: {e}')
                        pbar.update(1)

        # YouTube sometimes drops likes without returning an error: retry the ones that didn't stick
        failed = set(errors)
        liked_ids = {s['videoId'] for s in self.ytmusic.get_liked_songs(limit=None)['tracks']}
        missing = [(track, video_id) for _, track, video_id in tracks_to_like
                   if video_id not in liked_ids and track not in failed]
        if missing:
            print(f"Повторяю лайки, которые не сохранились: {len(missing)}")
            for track, video_id in tqdm(missing, desc='Retry'):
                self._like_track(track, video_id)
                time.sleep(1)
            liked_ids = {s['videoId'] for s in self.ytmusic.get_liked_songs(limit=None)['tracks']}
            errors += [track for track, video_id in missing if video_id not in liked_ids]

        return not_found, errors
    
    def import_liked_podcasts(self, podcasts: List[Podcast], max_workers: int = 5, keep_order: bool = True) -> Tuple[List[Podcast], List[Podcast]]:
        """
        Import podcasts to YouTube Music.

        Args:
            podcasts: List of podcasts to import
            max_workers: Number of parallel workers (default 5)
            keep_order: If True, likes are added sequentially to preserve order (slower).
                       If False, likes are added in parallel (faster, random order).
        """
        not_found: List[Podcast] = []
        errors: List[Podcast] = []

        # Этап 1: Параллельный поиск
        search_results = {}  # idx -> (podcast, videoId, error)

        print("Поиск подкастов...")
        with tqdm(total=len(podcasts), desc='Search') as pbar:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(self._search_podcast, podcast, idx): idx
                          for idx, podcast in enumerate(podcasts)}

                for future in as_completed(futures):
                    try:
                        idx, podcast, playlist_id, error = future.result()
                        search_results[idx] = (podcast, playlist_id, error)
                        pbar.set_postfix_str(f'{podcast.label} - {podcast.name}'[:40])
                    except Exception as e:
                        idx = futures[future]
                        search_results[idx] = (podcast[idx], None, str(e))
                    pbar.update(1)

        # Собираем подкасты для лайков
        podcasts_to_like = []  # (idx, podcast, playlist_id)
        for idx in range(len(podcasts)):
            podcast, playlist_id, error = search_results[idx]

            if error == 'not_found' or not playlist_id:
                not_found.append(podcast)
            elif error:
                errors.append(podcast)
            else:
                podcasts_to_like.append((idx, podcast, playlist_id))

        # Этап 2: Добавление лайков
        print("Добавление лайков...")

        if keep_order:
            # Последовательное добавление для сохранения порядка
            with tqdm(total=len(podcasts_to_like), desc='Like') as pbar:
                for idx, podcast, playlist_id in podcasts_to_like:
                    try:
                        self.ytmusic.rate_playlist(playlist_id, 'LIKE')
                        pbar.set_postfix_str(f'{podcast.label} - {podcast.name}'[:40])
                    except Exception as e:
                        errors.append(podcast)
                        pbar.write(f'Like error: {podcast.label} - {podcast.name}: {e}')
                    pbar.update(1)
        else:
            # Параллельное добавление (быстрее, но порядок случайный)
            with tqdm(total=len(podcasts_to_like), desc='Like') as pbar:
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {executor.submit(self._like_podcast, podcast, playlist_id): podcast
                              for idx, podcast, playlist_id in podcasts_to_like}

                    for future in as_completed(futures):
                        podcast = futures[future]
                        try:
                            _, success, error = future.result()
                            if not success:
                                errors.append(podcast)
                                pbar.write(f'Like error: {podcast.label} - {podcast.name}: {error}')
                            pbar.set_postfix_str(f'{podcast.label} - {podcast.name}'[:40])
                        except Exception as e:
                            errors.append(podcast)
                            pbar.write(f'Like error: {podcast.label} - {podcast.name}: {e}')
                        pbar.update(1)

        return not_found, errors

    def import_playlists(self, playlists: List[Playlist], max_workers: int = 5) -> List[Playlist]:
        """
        Import playlists to YouTube Music.

        Args:
            playlists: List of playlists to import
            max_workers: Number of parallel workers (default 5). Used for track search only

        Returns:
            List[Playlist]: A list of Playlists that encountered errors during creation
        """
        errors: List[Playlist] = []

        # Последовательное создание плейлистов
        # TODO: Подумать над параллелизацией
        with tqdm(total=len(playlists), desc='Playlist creation') as pbar:
            for playlist in playlists:
                try:
                    not_found, errors = self._create_playlist(playlist, max_workers=max_workers)
                    if not_found or errors:
                        print(f'Плейлист: {playlist.title}')
                        for track in not_found:
                            print(f'Не найдено: {track.artist} - {track.name}')
                        for track in errors:
                            print(f'Ошибка при поиске: {track.artist} - {track.name}')
                    pbar.set_postfix_str(f'{playlist.title}'[:40])
                except Exception as e:
                    errors.append(playlist)
                    pbar.write(f'Playlist creation error: {playlist.title}: {e}')
                pbar.update(1)
        return errors

    def _get_best_result(self, results: List[dict], track: Track) -> Optional[dict]:
        """Return the result closest to the track by artist and title, or None if none matches both."""
        best, best_score = None, 0.0
        for result in results:
            if not result.get('videoId'):
                continue
            artist = max((_similarity(track.artist, a['name']) for a in result.get('artists') or []), default=0.0)
            title = result.get('title', '')
            # "Title (feat. X)" still matches "Title", but ranks below an exact title
            title_score = max(_similarity(track.name, title),
                              0.9 * _similarity(track.name, re.sub(r'\s*[(\[].*?[)\]]', '', title)))
            if artist >= 0.85 and title_score >= 0.8 and artist + title_score > best_score:
                best, best_score = result, artist + title_score
        return best


# Алиас для обратной совместимости
YoutubeImoirter = YoutubeImporter
