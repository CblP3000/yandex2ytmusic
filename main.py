import json
import os
import time

from core import YandexMusicExporter
from core import YoutubeImporter
from core.playlist import Playlist
from core.podcast import Podcast
from core.track import Track
from cli.menu import Menu, MultiSelectMenu
from cli.debug import get_logger, redact_headers

logger = get_logger("main")


def choose_playlists(title: str, playlists: list) -> list:
    """Показывает мультивыбор конкретных плейлистов.

    Возвращает список выбранных плейлистов (пустой, если отменено).
    """
    if not playlists:
        print("Плейлисты не найдены")
        return []

    menu = MultiSelectMenu(title)
    for playlist in playlists:
        menu.add(playlist.title, payload=playlist)

    selected = menu.run()
    if selected is None:
        return []
    return [item.payload for item in selected]


def export_from_yandex(out_path: str) -> None:
    """Export chosen data from Yandex Music to JSON file."""
    token = input("Введи токен Yandex Music: ").strip()
    if not token:
        print("Ошибка: токен не указан")
        return

    importer = YandexMusicExporter(token)

    menu = MultiSelectMenu("Что экспортировать?")
    menu.add("Треки из «Мне нравится»", payload="tracks")
    menu.add("Подкасты", payload="podcasts")
    menu.add("Твои плейлисты", payload="my_playlists")
    menu.add("Лайкнутые плейлисты", payload="liked_playlists")
    menu.add("Инвертировать порядок треков в плейлистах", payload="reverse_playlist_tracks")

    selected = menu.run()
    if not selected:
        print("Экспорт отменён")
        return

    kinds = {item.payload for item in selected}

    tracks = []
    podcasts = []
    playlists = []

    if "tracks" in kinds:
        print('Экспорт лайкнутых треков из Яндекс Музыки...')
        tracks = importer.export_liked_tracks()
        tracks.reverse()

    if "podcasts" in kinds:
        print('Экспорт лайкнутых подкастов из Яндекс Музыки...')
        podcasts = importer.export_liked_podcasts()
        podcasts.reverse()

    if "my_playlists" in kinds:
        playlists.extend(choose_playlists("Выбери свои плейлисты", importer.get_my_playlists()))

    if "liked_playlists" in kinds:
        playlists.extend(choose_playlists("Выбери лайкнутые плейлисты", importer.get_liked_playlists()))

    if playlists:
        print(f'Экспорт {len(playlists)} плейлистов из Яндекс Музыки...')
        playlists = importer.export_playlists(playlists)
        playlists.reverse()

    if "reverse_playlist_tracks" in kinds:
        for playlist in playlists:
            playlist.tracks.reverse()

    if not tracks and not podcasts and not playlists:
        print("Экспорт отменён — ничего не выбрано")
        return

    data = {
        'playlists': [{'title': playlist.title, 'description': playlist.description, 'tracks': [{'artist': t.artist, 'name': t.name} for t in playlist.tracks]} for playlist in playlists],
        'liked_podcasts': [{'label': p.label, 'name': p.name} for p in podcasts],
        'liked_tracks': [{'artist': t.artist, 'name': t.name} for t in tracks],
        'not_found': [],
        'errors': [],
    }

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f'Экспортировано {len(tracks)} треков, {len(podcasts)} подкастов и {len(playlists)} плейлистов в {out_path}')


def resolve_creds(browser_creds: str = "browser.json", oauth_creds: str = "oauth.json") -> str:
    """Если настроен OAuth — используем его (токен живёт дольше и не зависит от IP)."""
    return oauth_creds if os.path.exists(oauth_creds) else browser_creds


def import_to_youtube(in_path: str, youtube_creds: str, client_secrets_path: str = None) -> None:
    """Import tracks from JSON file to YouTube Music."""
    if not os.path.exists(in_path):
        print(f"Ошибка: файл {in_path} не найден. Сначала выполни экспорт из Яндекс Музыки.")
        return

    if not os.path.exists(youtube_creds):
        print(f"Ошибка: файл {youtube_creds} не найден. Сначала настрой авторизацию YouTube.")
        return

    with open(in_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    tracks = [Track(artist=t['artist'], name=t['name']) for t in data['liked_tracks']]
    print(f'Загружено {len(tracks)} треков из {in_path}')

    podcasts = [Podcast(label=p['label'], name=p['name']) for p in data['liked_podcasts']]
    print(f'Загружено {len(podcasts)} подкастов из {in_path}')

    playlists = []
    for playlist in data['playlists']:
        tracklist = []
        for track in playlist['tracks']:
            tracklist.append(Track(track['artist'], track['name']))
        playlists.append(Playlist(playlist['title'], playlist['description'], tracklist))
    print(f'Загружено {len(playlists)} плейлистов из {in_path}')

    logger.info("import_to_youtube: tracks=%d podcasts=%d playlists=%d", len(tracks), len(podcasts), len(playlists))

    menu = Menu("Режим импорта")
    menu.add(
        "Быстрый (параллельный, порядок не сохраняется)",
        lambda: _run_import(data, tracks, podcasts, playlists, in_path, youtube_creds, client_secrets_path, keep_order=False),
    )
    menu.add(
        "С сохранением порядка (медленнее)",
        lambda: _run_import(data, tracks, podcasts, playlists, in_path, youtube_creds, client_secrets_path, keep_order=True),
    )
    menu.run()


def _run_import(data, tracks, podcasts, playlists, in_path, youtube_creds, client_secrets_path, keep_order: bool) -> None:
    logger.info("_run_import: keep_order=%s", keep_order)
    try:
        exporter = YoutubeImporter(youtube_creds, client_secrets_path)
        exporter.verify_auth()
    except Exception as e:
        logger.error("YoutubeImporter init/verify failed: %s", e)
        raise

    print('Импорт треков в YouTube Music...')
    tracks_not_found, tracks_errors = exporter.import_liked_tracks(tracks, keep_order=keep_order)
    data['not_found'] = [{'artist': t.artist, 'name': t.name} for t in tracks_not_found]
    data['errors'] = [{'artist': t.artist, 'name': t.name} for t in tracks_errors]

    for track in tracks_not_found:
        print(f'Не найдено: {track.artist} - {track.name}')
    print(f'Треков: {len(tracks_not_found)} не найдено, {len(tracks_errors)} ошибок.')

    print()

    print('Импорт плейлистов в YouTube Music...')
    playlist_errors = exporter.import_playlists(playlists)
    data['errors'].extend([{'title': p.title} for p in playlist_errors])

    print(f'{len(playlist_errors)} ошибок при создании плейлистов.')

    print()

    print('Импорт подкастов в YouTube Music...')
    podcasts_not_found, podcasts_errors = exporter.import_liked_podcasts(podcasts, keep_order=keep_order)

    data['not_found'].extend([{'label': p.label, 'name': p.name} for p in podcasts_not_found])
    data['errors'].extend([{'label': p.label, 'name': p.name} for p in podcasts_errors])

    for podcast in podcasts_not_found:
        print(f'Не найдено: {podcast.label} - {podcast.name}')
    print(f'Подкастов: {len(podcasts_not_found)} не найдено, {len(podcasts_errors)} ошибок.')

    print(f'Итого: {len(tracks_not_found) + len(podcasts_not_found)} не найдено, {len(tracks_errors) + len(podcasts_errors)} ошибок.')

    with open(in_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def setup_youtube_auth(creds_path: str, client_secrets_path: str = None) -> None:
    """Setup YouTube Music authentication."""
    menu = Menu("Способ авторизации YouTube Music")
    menu.add("Автоматически через браузер (откроется окно)", lambda: auto_browser_auth(creds_path))
    menu.add("Вручную (вставить headers)", lambda: manual_browser_auth(creds_path))
    menu.add("OAuth через Google (надёжно, рекомендуется)", lambda: oauth_auth("oauth.json", client_secrets_path))
    menu.run()


def oauth_auth(oauth_creds: str, client_secrets_path: str) -> None:
    """Настройка OAuth-авторизации (device flow): токен живёт долго и не зависит от IP."""
    if not client_secrets_path or not os.path.exists(client_secrets_path):
        print("Для OAuth нужен файл client_secrets.json от Google:")
        print("  1. Открой https://console.cloud.google.com → APIs & Services → Credentials")
        print("  2. Создать учётные данные → OAuth client ID → тип «Desktop app»")
        print("  3. Скачай JSON и сохрани рядом с программой как client_secrets.json")
        print("  4. Запусти снова и выбери пункт OAuth.")
        return
    try:
        importer = YoutubeImporter(oauth_creds, client_secrets_path)
        importer.verify_auth()
        print(f"OAuth работает, токен сохранён в {oauth_creds}")
    except Exception as e:
        print(f"Ошибка OAuth: {e}")


def auto_browser_auth(creds_path: str) -> None:
    """Automatically extract headers from browser using Playwright."""
    try:
        from playwright.sync_api import sync_playwright
        logger.info("playwright imported OK")
    except ImportError:
        print("Установи playwright:")
        print("  pip install playwright")
        print("  playwright install chromium")
        return

    print("\nОткрою браузер для авторизации...")
    print("1. Войди в аккаунт Google (если не залогинен)")
    print("2. Дождись загрузки YouTube Music")
    print("3. Браузер закроется автоматически\n")

    captured_headers = {}
    post_count = 0
    authed_post_count = 0

    def capture_request(request):
        nonlocal captured_headers, post_count, authed_post_count
        if request.method == "POST" and "/youtubei/v1/" in request.url:
            post_count += 1
            headers = dict(request.headers)
            if "cookie" in headers and "authorization" in headers:
                authed_post_count += 1
                logger.info(
                    "captured POST %s (authed=%d) keys=%s",
                    request.url.split("/youtubei/v1/")[-1].split("?")[0],
                    authed_post_count,
                    sorted(redact_headers(headers).keys()),
                )
                captured_headers = headers

    try:
        with sync_playwright() as p:
            logger.info("launching chromium...")
            try:
                browser = p.chromium.launch(headless=False)
            except Exception as e:
                logger.error("chromium launch failed: %s", e)
                raise
            logger.info("browser launched")
            context = browser.new_context()
            page = context.new_page()

            page.on("request", capture_request)

            logger.info("goto https://music.youtube.com ...")
            try:
                page.goto("https://music.youtube.com")
                logger.info("goto OK, url=%s", page.url)
            except Exception as e:
                logger.error("goto failed: %s (url=%s)", e, page.url)
                raise

            # Ждём пока пользователь залогинится и headers будут перехвачены
            print("Ожидание авторизации...")
            last_log = 0.0
            for _ in range(120):  # Максимум 2 минуты
                page.wait_for_timeout(1000)
                if captured_headers:
                    break
                if time.monotonic() - last_log > 10:
                    last_log = time.monotonic()
                    logger.info(
                        "waiting... url=%s posts=%d authed=%d",
                        page.url[:120], post_count, authed_post_count,
                    )

            logger.info(
                "wait loop finished: captured=%s posts=%d authed=%d url=%s",
                bool(captured_headers), post_count, authed_post_count, page.url[:120],
            )

            try:
                browser.close()
            except Exception:
                pass

        if not captured_headers:
            print("Не удалось перехватить headers. Попробуй ручной способ.")
            logger.error("no headers captured: posts=%d authed=%d", post_count, authed_post_count)
            return

        # Сохраняем только нужные headers (которые реально были в захваченном запросе)
        essential = [
            "accept", "accept-language", "authorization", "content-type", "cookie",
            "origin", "referer", "user-agent", "x-goog-authuser", "x-goog-visitor-id",
            "x-origin", "x-youtube-bootstrap-logged-in", "x-youtube-client-name",
            "x-youtube-client-version",
        ]
        headers_to_save = {key: captured_headers[key] for key in essential if captured_headers.get(key)}
        if "authorization" not in headers_to_save or "cookie" not in headers_to_save:
            logger.error("captured headers missing required authorization/cookie: keys=%s",
                         sorted(redact_headers(headers_to_save).keys()))
            print("Ошибка: захваченные заголовки неполные (нет authorization или cookie).")
            print("Переустанови авторизацию заново или используй ручной способ.")
            return

        with open(creds_path, 'w') as f:
            json.dump(headers_to_save, f, indent=4)

        logger.info("saved %s: keys=%s", creds_path, sorted(redact_headers(headers_to_save).keys()))

        print(f"\nАвторизация сохранена в {creds_path}")

    except Exception as e:
        logger.error("auto_browser_auth failed: %s", e)
        print(f"Ошибка: {e}")
        print("\nПопробуй ручной способ (вариант 2)")


def manual_browser_auth(creds_path: str) -> None:
    """Manual browser authentication setup."""
    from ytmusicapi import setup

    print("\n=== Ручная авторизация ===")
    print("1. Открой https://music.youtube.com в браузере (войди в аккаунт)")
    print("2. Открой DevTools (F12)")
    print("3. Вкладка Network, фильтр по 'browse'")
    print("4. Кликни на любой POST запрос к browse?...")
    print("5. Скопируй ВСЕ Request Headers и сохрани их в текстовый файл")
    print("\nТерминал может обрезать длинную строку cookie при вставке, поэтому лучше через файл.")
    headers_path = input("Путь к файлу с заголовками (Enter — вставить вручную): ").strip().strip("'\"")

    try:
        if headers_path:
            with open(os.path.expanduser(headers_path), encoding='utf-8') as f:
                setup(filepath=creds_path, headers_raw=f.read())
        else:
            print("\nВставь заголовки и нажми Ctrl+D (Ctrl+Z и Enter в Windows):\n")
            setup(filepath=creds_path)
        logger.info("manual setup() completed")
    except Exception as e:
        logger.error("manual setup() failed: %s", e)
        raise

    if os.path.exists(creds_path):
        print(f"\nАвторизация сохранена в {creds_path}")
        logger.info("credentials saved to %s", creds_path)
        try:
            importer = YoutubeImporter(creds_path)
            importer.verify_auth()
            print("Авторизация работает — можно импортировать.")
        except Exception as e:
            print(f"\nВнимание: авторизация НЕ работает — {e}")
            print("Проверь, что скопированы ВСЕ Request Headers именно POST-запроса browse,")
            print("включая authorization (SAPISIDHASH ...), cookie и x-goog-authuser,")
            print("и что ты залогинен в YouTube Music в этом браузере.")
    else:
        print("\nОшибка: файл не создан")


def full_transfer(tracks_path: str, youtube_creds: str, client_secrets_path: str = None) -> None:
    """Full transfer: export from Yandex and import to YouTube."""
    export_from_yandex(tracks_path)

    if not os.path.exists(tracks_path):
        return

    if not os.path.exists(youtube_creds):
        print("\nТеперь настроим авторизацию YouTube Music...")
        setup_youtube_auth(youtube_creds, client_secrets_path)

    if os.path.exists(youtube_creds):
        import_to_youtube(tracks_path, youtube_creds, client_secrets_path)


def main() -> None:
    tracks_path = "tracks.json"
    youtube_creds = "browser.json"

    client_secrets_path = None
    if os.path.exists("client_secrets.json"):
        client_secrets_path = "client_secrets.json"

    menu = Menu("=== Yandex Music → YouTube Music ===", "Что хочешь сделать?")
    menu.add("Полный перенос (экспорт из Яндекса + импорт в YouTube)", lambda: full_transfer(tracks_path, youtube_creds, client_secrets_path))
    menu.add("Только экспорт из Яндекс Музыки", lambda: export_from_yandex(tracks_path))
    menu.add("Только импорт в YouTube Music (из файла)", lambda: import_to_youtube(tracks_path, youtube_creds, client_secrets_path))
    menu.add("Настроить авторизацию YouTube Music", lambda: setup_youtube_auth(youtube_creds, client_secrets_path))
    menu.add("Выход")
    menu.run()


if __name__ == '__main__':
    main()
