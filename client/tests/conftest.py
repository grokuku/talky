# -*- coding: utf-8 -*-
"""
tests/conftest.py
=================
Fixtures pytest : simulation des dépendances système (numpy, sounddevice,
evdev, pyperclip, httpx) quand elles ne sont pas disponibles, protection de
config.json et clients HTTP/WebSocket TestClient.

Les tests ne doivent JAMAIS contacter le serveur whisper-live distant ni toucher
au matériel : chaque module est mocké conditionnellement (« try import,
sinon mock », style ref/tests/conftest.py). Le module ``websockets`` (client
WS temps réel WhisperLive) est mocké INCONDITIONNELLEMENT : contrairement à
httpx (qui expose MockTransport comme seam de test), la lib websockets n'a pas
de transport injectable — on ne veut jamais de vraie connexion WS en test.
"""

import sys
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# Simulation des dépendances (si absentes de l'environnement de test)
# ---------------------------------------------------------------------------
def _importable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:  # noqa: BLE001 - OSError (PortAudio), ImportError, ...
        return False


def _install_numpy_mock() -> None:
    """numpy : le moteur appelle np.concatenate(...).flatten()."""
    np_mock = types.ModuleType("numpy")

    class _Flat:
        def __init__(self, data):
            self._data = data

        def flatten(self):
            return self._data

        def __len__(self):
            return len(self._data)

        def __getitem__(self, key):
            # Tranches (ex. `arr[::3]`) : renvoie une liste Python, itérable
            # et compatible avec le repli boucle (le vrai numpy renvoie une
            # vue non contiguë -> `flat`).
            return self._data[key]

        def reshape(self, *shape):
            # Le moteur (encode_wav/_pcm16_from_float) aplatit par
            # reshape(-1)/flatten() : le mock expose un objet dont `.flatten()`
            # renvoie toujours les données à plat, quelle que soit la shape
            # demandée (équivalent numpy d'un `reshape(-1)`).
            return self

    def _concatenate(arrays, axis=0):
        flat = []
        for arr in arrays:
            flat.extend(arr.flatten() if hasattr(arr, "flatten") else arr)
        return _Flat(flat)

    np_mock.concatenate = _concatenate
    np_mock.array = lambda data, dtype=None: _Flat(list(data))
    np_mock.ndarray = _Flat
    # Aliases de types : le mock stocke des floats Python (les conversions
    # dtype sont inopérantes, ce qui suffit pour les tests P2/P3).
    np_mock.float32 = float
    np_mock.float64 = float
    np_mock.int16 = int
    np_mock.int32 = int
    # Utilisé par pytest.approx quand un module « numpy » est présent.
    np_mock.isscalar = lambda obj: isinstance(obj, (int, float, bool))
    sys.modules["numpy"] = np_mock


def _install_sounddevice_mock() -> None:
    """sounddevice : flux simulé + liste de devices factice."""
    sd_mock = types.ModuleType("sounddevice")

    class FakeStream:
        def __init__(self, *a, **kw):
            self.started = False

        def start(self):
            self.started = True

        def stop(self):
            self.started = False

        def close(self):
            self.started = False

    sd_mock.InputStream = FakeStream
    sd_mock.OutputStream = FakeStream
    sd_mock.query_devices = lambda: [
        {"name": "Micro simule", "max_input_channels": 1,
         "default_samplerate": 16000},
    ]
    sd_mock.default = types.SimpleNamespace(device=(0, 0))
    sys.modules["sounddevice"] = sd_mock


def _install_evdev_mock() -> None:
    """python-evdev : hotkeys globales simulées (lecture /dev/input, P3)."""
    ecodes = types.ModuleType("evdev.ecodes")
    ecodes.EV_KEY = 0x01
    ecodes.EV_REL = 0x02
    ecodes.EV_ABS = 0x03
    ecodes.KEY_RESERVED = 0
    ecodes.KEY_ESC = 1
    ecodes.KEY_ENTER = 28
    ecodes.KEY_LEFTCTRL = 29
    ecodes.KEY_LEFTALT = 56
    ecodes.KEY_SPACE = 57
    ecodes.KEY_F8 = 65
    ecodes.KEY_F9 = 67
    ecodes.KEY_LEFTMETA = 125
    sys.modules["evdev.ecodes"] = ecodes

    evdev_mock = types.ModuleType("evdev")
    evdev_mock.ecodes = ecodes
    evdev_mock.list_devices = lambda: []

    class FakeInputDevice:
        def __init__(self, path):
            self.path = path
            self.name = "Clavier simulé"
            self.fd = -1
            self._closed = False

        def capabilities(self, verbose=False):
            return {ecodes.EV_KEY: [ecodes.KEY_F8, ecodes.KEY_LEFTCTRL,
                                    ecodes.KEY_SPACE]}

        def grab(self):
            pass

        def ungrab(self):
            pass

        def read_loop(self):
            return iter(())

        def close(self):
            self._closed = True

    evdev_mock.InputDevice = FakeInputDevice
    evdev_mock.categorize = lambda event: event
    sys.modules["evdev"] = evdev_mock


def _install_pyperclip_mock() -> None:
    """pyperclip : presse-papier simulé (wl-copy/wl-paste non requis)."""
    pc_mock = types.ModuleType("pyperclip")
    pc_mock._clipboard = ""
    pc_mock.copy = lambda text: setattr(pc_mock, "_clipboard", str(text))
    pc_mock.paste = lambda: pc_mock._clipboard
    sys.modules["pyperclip"] = pc_mock


def _install_httpx_mock() -> None:
    """httpx : mock fonctionnel compatible MockTransport — le serveur whisper-live
    n'est JAMAIS contacté pendant les tests.

    Reproduit l'API httpx utilisée par transcriber_client (P2) : Client(
    transport=...), post/get, Timeout, Response (json/raise_for_status),
    exceptions ConnectError/ConnectTimeout/ReadTimeout/... En environnement
    réel (httpx installé), rien n'est mocké : le vrai httpx est utilisé avec
    MockTransport (pattern officiel de la doc httpx).
    """
    import json as _json
    from urllib.parse import urlsplit

    httpx_mock = types.ModuleType("httpx")

    # --- Hiérarchie d'exceptions (simplifiée mais réaliste) ---
    class _RequestError(Exception):
        """Erreur réseau de base (équivalent httpx.RequestError)."""

    class _TransportError(_RequestError):
        pass

    class _ConnectError(_TransportError):
        pass

    class _ConnectTimeout(_TransportError):
        pass

    class _ReadTimeout(_TransportError):
        pass

    class _WriteTimeout(_TransportError):
        pass

    class _HTTPStatusError(Exception):
        def __init__(self, message, *, request=None, response=None):
            super().__init__(message)
            self.request = request
            self.response = response

    # --- Types de base ---
    class _Timeout:
        def __init__(self, timeout=None, connect=None, read=None, write=None,
                     pool=None):
            self.timeout = timeout
            self.connect = connect
            self.read = read
            self.write = write
            self.pool = pool

    class _URL:
        def __init__(self, url):
            self._url = str(url)

        def __str__(self):
            return self._url

        def __repr__(self):
            return f"URL({self._url!r})"

        @property
        def path(self):
            return urlsplit(self._url).path

    class _Request:
        def __init__(self, method, url, headers=None, content=b"",
                     extensions=None):
            self.method = method
            self.url = _URL(url)
            self.headers = headers or {}
            self.content = content
            self.extensions = extensions or {}

        def read(self):
            return self.content

        def json(self):
            if isinstance(self.content, (bytes, bytearray)):
                return _json.loads(self.content.decode("utf-8"))
            return self.content

    class _Response:
        def __init__(self, status_code=200, text="", json=None, headers=None,
                     request=None):
            self.status_code = status_code
            self.text = text
            self._json = json
            self.headers = headers or {}
            self.request = request

        def json(self):
            if self._json is not None:
                return self._json
            if self.text:
                return _json.loads(self.text)  # lève si JSON invalide
            return None

        def raise_for_status(self):
            if 400 <= self.status_code < 600:
                raise _HTTPStatusError(
                    f"HTTP {self.status_code}", request=self.request,
                    response=self)

    class _MockTransport:
        def __init__(self, handler):
            self.handler = handler

    class _Client:
        def __init__(self, *args, timeout=None, transport=None, **kwargs):
            self.timeout = timeout
            self.transport = transport
            self.calls = []

        def _send(self, method, url, headers=None, content=b"",
                  extensions=None):
            request = _Request(method, url, headers=headers, content=content,
                               extensions=extensions)
            if self.transport is not None:
                return self.transport.handler(request)
            return _Response()

        @staticmethod
        def _multipart_fields(files=None, data=None):
            """Reconstruit les champs multipart dans le format httpx 0.28 :
            liste de (nom, valeur), les fichiers étant des tuples
            (filename, content, content_type)."""
            fields = []
            if data:
                for name, value in data.items():
                    fields.append((name, str(value)))
            if files:
                for name, value in files.items():
                    filename, content, content_type = value
                    fields.append((name, (filename, content, content_type)))
            return fields

        def post(self, url, *args, files=None, data=None, json=None,
                 headers=None, timeout=None, **kwargs):
            self.calls.append(("post", url, kwargs))
            content = b""
            if json is not None:
                content = _json.dumps(json).encode("utf-8")
            extensions = {"multipart": {
                "boundary": "----talky-mock-boundary",
                "fields": self._multipart_fields(files=files, data=data),
            }}
            return self._send("POST", url, headers=headers, content=content,
                              extensions=extensions)

        def get(self, url, *args, headers=None, timeout=None, **kwargs):
            self.calls.append(("get", url, kwargs))
            return self._send("GET", url, headers=headers)

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()

    def _post(url, *args, files=None, data=None, headers=None, timeout=None,
              transport=None, **kwargs):
        client = _Client(timeout=timeout, transport=transport)
        return client.post(url, files=files, data=data, headers=headers,
                           timeout=timeout, **kwargs)

    def _get(url, *args, headers=None, timeout=None, transport=None, **kwargs):
        client = _Client(timeout=timeout, transport=transport)
        return client.get(url, headers=headers, timeout=timeout, **kwargs)

    # --- Exposition publique (API httpx) ---
    httpx_mock.Client = _Client
    httpx_mock.Response = _Response
    httpx_mock.Timeout = _Timeout
    httpx_mock.URL = _URL
    httpx_mock.Request = _Request
    httpx_mock.MockTransport = _MockTransport
    httpx_mock.post = _post
    httpx_mock.get = _get
    httpx_mock.RequestError = _RequestError
    httpx_mock.ConnectError = _ConnectError
    httpx_mock.ConnectTimeout = _ConnectTimeout
    httpx_mock.ReadTimeout = _ReadTimeout
    httpx_mock.WriteTimeout = _WriteTimeout
    httpx_mock.HTTPStatusError = _HTTPStatusError
    sys.modules["httpx"] = httpx_mock


def _install_websockets_mock() -> None:
    """websockets : mock fonctionnel minimal pour WhisperLiveClient.

    Reproduit l'API asyncio de la lib ``websockets`` utilisée par
    app/engine/whisperlive_client.py : connect() -> async context manager ->
    session avec send/recv/close, plus websockets.exceptions.ConnectionClosed.

    Contrairement à httpx (qui expose MockTransport comme seam de test), la
    lib websockets n'a PAS de transport injectable : on l'installe donc
    TOUJOURS en environnement de test, pour ne JAMAIS ouvrir de vraie
    connexion WS vers le serveur whisper-live.

    Pilotage par les tests :
      * websockets._set_script(url, [dict|str, ...]) -> messages serveur
        renvoyés par recv() (dict = JSON encodé). Clé "*" = script par défaut.
      * websockets._reset_scripts() -> vide les scripts enregistrés.
      * websockets._active_sessions -> sessions créées (sent, inbox, closed).
    """
    import asyncio
    import json as _json
    import types

    ws_mock = types.ModuleType("websockets")

    # --------------------------- exceptions ---------------------------
    exceptions = types.ModuleType("websockets.exceptions")

    class ConnectionClosed(Exception):
        def __init__(self, code=1000, reason=""):
            super().__init__(
                f"connection closed (code={code}, reason={reason!r})")
            self.code = code
            self.rc = code
            self.reason = reason

    class ConnectionClosedOK(ConnectionClosed):
        pass

    exceptions.ConnectionClosed = ConnectionClosed
    exceptions.ConnectionClosedOK = ConnectionClosedOK
    ws_mock.exceptions = exceptions
    ws_mock.ConnectionClosed = ConnectionClosed
    ws_mock.ConnectionClosedOK = ConnectionClosedOK
    sys.modules["websockets.exceptions"] = exceptions

    # ----------------------- registre de scripts ----------------------
    _scripts = {}          # url -> [dict|str, ...] ("*" = défaut)
    _active_sessions = []  # sessions WS créées pendant les tests
    _active_connectors = []  # connecteurs (url + kwargs) créés pendant les tests
    _CLOSED = object()     # sentinelle interne : recv() lève ConnectionClosed

    def _set_script(url, script):
        _scripts[url] = list(script or [])

    def _reset_scripts():
        _scripts.clear()
        _active_sessions.clear()
        _active_connectors.clear()

    # --------------------------- session factice ----------------------
    class FakeWebSocket:
        """Session WS simulée : enregistre les messages envoyés par le client
        (``sent``) et sert les messages serveur depuis son inbox (script)."""

        def __init__(self, loop, script):
            self._loop = loop
            self.sent = []               # messages client -> serveur (str/bytes)
            self._inbox = asyncio.Queue()
            self.closed = False
            self.close_code = None
            for item in script:
                self._inbox.put_nowait(
                    _json.dumps(item, ensure_ascii=False)
                    if isinstance(item, dict) else item)

        async def send(self, data):
            self.sent.append(data)

        async def recv(self):
            if self.closed and self._inbox.empty():
                raise ConnectionClosed(
                    self.close_code or 1000, "session fermée")
            item = await self._inbox.get()
            if item is _CLOSED:
                raise ConnectionClosed(
                    self.close_code or 1000, "session fermée")
            return item

        async def close(self, code=1000, reason=""):
            self.closed = True
            self.close_code = code
            self._inbox.put_nowait(_CLOSED)  # débloque un recv() en attente

        # --- helpers de test ---
        def feed(self, payload):
            """Pousse un message serveur (str JSON) dans l'inbox."""
            self._inbox.put_nowait(payload)

        def feed_json(self, data):
            """Pousse un message serveur (dict) dans l'inbox."""
            self._inbox.put_nowait(
                _json.dumps(data, ensure_ascii=False))

    # --------------------------- connect() ----------------------------
    class _Connector:
        """Objet retourné par websockets.connect(url) : async context manager
        qui fabrique la session factice (script serveur par URL)."""

        def __init__(self, url, **kwargs):
            self.url = url
            self.kwargs = kwargs
            self.session = None
            _active_connectors.append(self)

        async def __aenter__(self):
            loop = asyncio.get_running_loop()
            script = _scripts.get(self.url, _scripts.get("*", []))
            self.session = FakeWebSocket(loop, script)
            _active_sessions.append(self.session)
            return self.session

        async def __aexit__(self, *exc):
            if self.session is not None and not self.session.closed:
                await self.session.close()
            return False

    def _connect(url, **kwargs):
        return _Connector(url, **kwargs)

    # ------------------------- exposition publique --------------------
    ws_mock.connect = _connect
    ws_mock._set_script = _set_script
    ws_mock._reset_scripts = _reset_scripts
    ws_mock._active_sessions = _active_sessions
    ws_mock._active_connectors = _active_connectors
    ws_mock._FakeWebSocket = FakeWebSocket
    ws_mock._Connector = _Connector
    sys.modules["websockets"] = ws_mock


def _install_fastapi_mock() -> None:
    """fastapi : mock fonctionnel minimal (routing + TestClient + WebSocket).

    Utilisé uniquement quand fastapi n'est pas installé (environnement de
    test nu) : il reproduit la partie de l'API fastapi utilisée par
    app/api/* (FastAPI, APIRouter, responses, staticfiles, TestClient) de
    façon assez fidèle pour exécuter les routes et le WebSocket sans serveur
    ni uvicorn. En environnement réel (fastapi installé via requirements),
    rien n'est mocké : le vrai TestClient de fastapi est utilisé.
    """
    import asyncio
    import json as _json
    import types
    from pathlib import Path

    fastapi_mock = types.ModuleType("fastapi")

    # --------------------------- responses ---------------------------
    responses = types.ModuleType("fastapi.responses")

    class JSONResponse:
        def __init__(self, content, status_code=200, headers=None,
                     media_type=None, background=None):
            self.content = content
            self.status_code = status_code
            self.headers = headers or {}
            self.media_type = media_type
            self.background = background

        def body(self) -> bytes:
            if isinstance(self.content, (dict, list)):
                return _json.dumps(self.content, ensure_ascii=False).encode("utf-8")
            return str(self.content).encode("utf-8")

    class FileResponse:
        def __init__(self, path, status_code=200, headers=None,
                     media_type=None, filename=None,
                     content_disposition_type="attachment"):
            self.path = Path(path)
            self.status_code = status_code
            self.headers = headers or {}
            self.media_type = media_type
            self.filename = filename

        @property
        def text(self) -> str:
            return self.path.read_text(encoding="utf-8")

    class HTMLResponse(JSONResponse):
        def __init__(self, content, status_code=200, headers=None,
                     media_type="text/html"):
            super().__init__(content, status_code=status_code,
                             headers=headers, media_type=media_type)

    responses.JSONResponse = JSONResponse
    responses.FileResponse = FileResponse
    responses.HTMLResponse = HTMLResponse
    sys.modules["fastapi.responses"] = responses
    fastapi_mock.responses = responses

    # -------------------------- staticfiles --------------------------
    staticfiles = types.ModuleType("fastapi.staticfiles")

    class StaticFiles:
        def __init__(self, directory=None, packages=None, html=False,
                     check_dir=True):
            self.directory = Path(directory) if directory else None
            self.packages = packages
            self.html = html
            self.check_dir = check_dir

    staticfiles.StaticFiles = StaticFiles
    sys.modules["fastapi.staticfiles"] = staticfiles
    fastapi_mock.staticfiles = staticfiles

    # --------------------------- WebSocket ---------------------------
    class WebSocketDisconnect(Exception):
        def __init__(self, code=1000):
            self.code = code

    class WebSocket:
        """Type de base (annotations) ; la vraie session est _FakeWebSocket."""

    fastapi_mock.WebSocket = WebSocket
    fastapi_mock.WebSocketDisconnect = WebSocketDisconnect

    # ---------------------------- routing ----------------------------
    class Route:
        def __init__(self, path, func, methods, websocket=False):
            self.path = path
            self.func = func
            self.methods = methods or set()
            self.websocket = websocket

    class APIRouter:
        def __init__(self, prefix="", tags=None):
            self.prefix = prefix
            self.tags = tags or []
            self.routes = []

        def _decorator(self, path, methods):
            full = (self.prefix + path) or "/"

            def deco(func):
                self.routes.append(Route(full, func, methods))
                return func

            return deco

        def get(self, path="", **kwargs):
            return self._decorator(path, {"GET"})

        def post(self, path="", **kwargs):
            return self._decorator(path, {"POST"})

        def delete(self, path="", **kwargs):
            return self._decorator(path, {"DELETE"})

        def websocket(self, path="", **kwargs):
            full = (self.prefix + path) or "/"

            def deco(func):
                self.routes.append(Route(full, func, None, websocket=True))
                return func

            return deco

    class FastAPI:
        def __init__(self, title=None, version=None, lifespan=None, **kwargs):
            self.title = title
            self.version = version
            self.lifespan = lifespan
            self.routes = []
            self._static_mounts = []

        def include_router(self, router):
            self.routes.extend(router.routes)

        def mount(self, path, app, name=None):
            self._static_mounts.append((path, app, name))

        def _decorator(self, path, methods):
            def deco(func):
                self.routes.append(Route(path, func, methods))
                return func

            return deco

        def get(self, path, **kwargs):
            return self._decorator(path, {"GET"})

        def post(self, path, **kwargs):
            return self._decorator(path, {"POST"})

        def delete(self, path, **kwargs):
            return self._decorator(path, {"DELETE"})

        def websocket(self, path, **kwargs):
            def deco(func):
                self.routes.append(Route(path, func, None, websocket=True))
                return func

            return deco

    fastapi_mock.FastAPI = FastAPI
    fastapi_mock.APIRouter = APIRouter
    fastapi_mock.Route = Route

    # -------------------------- TestClient ---------------------------
    testclient = types.ModuleType("fastapi.testclient")

    class _TestResponse:
        def __init__(self, status_code, content):
            self.status_code = status_code
            self.content = content
            self.headers = {}

        @property
        def text(self) -> str:
            if isinstance(self.content, bytes):
                return self.content.decode("utf-8")
            return str(self.content)

        def json(self):
            if isinstance(self.content, (dict, list)):
                return self.content
            return _json.loads(self.text)

    class _FakeWebSocket(WebSocket):
        """WebSocket simulé côté serveur : file de messages sortants + future
        de fermeture. receive_text() bloque jusqu'à la fermeture de la session
        (le TestClient réel se comporte de la même façon côté serveur)."""

        def __init__(self, loop):
            self._loop = loop
            self._outgoing = asyncio.Queue()
            self._closed = loop.create_future()
            self.accepted = False
            # Headers factices (comme starlette) : la route /ws lit
            # « origin » pour l'anti-hijacking (TALKY_ALLOWED_ORIGINS).
            self.headers = {}
            self.scope = {"type": "websocket", "headers": []}

        async def accept(self):
            self.accepted = True

        async def send_text(self, payload):
            await self._outgoing.put(str(payload))

        async def send_json(self, data):
            await self.send_text(_json.dumps(data, ensure_ascii=False))

        async def receive_text(self):
            await self._closed  # bloque jusqu'à la fermeture (simulée)
            raise WebSocketDisconnect()

    class _WSContext:
        def __init__(self, client, path):
            self._client = client
            self._path = path
            self._ws = None
            self._task = None

        def __enter__(self):
            route = self._client._find_route(self._path, websocket=True)
            if route is None:
                raise RuntimeError(f"Route WebSocket {self._path} introuvable")
            self._ws = _FakeWebSocket(self._client._loop)
            self._task = self._client._loop.create_task(route.func(self._ws))
            return self

        def __exit__(self, *exc):
            if self._ws is not None and not self._ws._closed.done():
                self._ws._closed.set_result(True)
            if self._task is not None:
                try:
                    self._client._loop.run_until_complete(asyncio.sleep(0))
                except RuntimeError:
                    pass
                self._task.cancel()

        def _recv(self, decode):
            loop = self._client._loop

            async def _get():
                return await asyncio.wait_for(self._ws._outgoing.get(), timeout=2.0)

            raw = loop.run_until_complete(_get())
            return _json.loads(raw) if decode else raw

        def receive_json(self):
            return self._recv(decode=True)

        def receive_text(self):
            return self._recv(decode=False)

    class TestClient:
        def __init__(self, app, base_url="http://testserver"):
            self.app = app
            self.base_url = base_url
            self._loop = None
            self._lifespan_cm = None

        def __enter__(self):
            self._loop = asyncio.new_event_loop()
            if self.app.lifespan is not None:
                # Le lifespan est un @asynccontextmanager : on le pilote via
                # __aenter__/__aexit__ (comme le fait starlette/TestClient).
                self._lifespan_cm = self.app.lifespan(self.app)
                self._loop.run_until_complete(self._lifespan_cm.__aenter__())
            return self

        def __exit__(self, *exc):
            if self._lifespan_cm is not None:
                try:
                    self._loop.run_until_complete(self._lifespan_cm.__aexit__(*exc))
                except Exception:  # noqa: BLE001
                    pass
            if self._loop is not None:
                try:
                    self._loop.run_until_complete(asyncio.sleep(0))
                except Exception:  # noqa: BLE001
                    pass
                self._loop.close()
                self._loop = None

        def _find_route(self, path, method=None, websocket=False):
            for route in self.app.routes:
                if route.websocket != websocket:
                    continue
                if route.path != path:
                    continue
                if not websocket and method is not None and method not in route.methods:
                    continue
                return route
            return None

        def request(self, method, path, json=None, **kwargs):
            route = self._find_route(path, method=method)
            if route is None:
                return _TestResponse(404, {"detail": "Not Found"})
            if json is not None:
                result = self._loop.run_until_complete(route.func(json))
            else:
                result = self._loop.run_until_complete(route.func())
            return self._to_response(result)

        @staticmethod
        def _to_response(result):
            if isinstance(result, JSONResponse):
                return _TestResponse(result.status_code, result.body())
            if isinstance(result, FileResponse):
                return _TestResponse(result.status_code, result.text)
            if isinstance(result, (dict, list)):
                return _TestResponse(200, result)
            if isinstance(result, str):
                return _TestResponse(200, result)
            return _TestResponse(200, result)

        def get(self, path, **kwargs):
            return self.request("GET", path, **kwargs)

        def post(self, path, **kwargs):
            return self.request("POST", path, **kwargs)

        def delete(self, path, **kwargs):
            return self.request("DELETE", path, **kwargs)

        def websocket_connect(self, path):
            return _WSContext(self, path)

    testclient.TestClient = TestClient
    sys.modules["fastapi.testclient"] = testclient
    fastapi_mock.testclient = testclient

    sys.modules["fastapi"] = fastapi_mock


# --- Installation conditionnelle, par module (« try import, sinon mock ») ---
for _mod, _install in (
    # numpy reste CONDITIONNEL : en environnement réel (.venv-lot2) le vrai
    # numpy est utilisé (c'est le comportement « réel » que l'on veut valider —
    # cf. test_encoding_pure 2D/stéréo) ; en environnement nu (.venv-bare) il
    # est mocké.
    ("numpy", _install_numpy_mock),
    ("sounddevice", _install_sounddevice_mock),
    ("evdev", _install_evdev_mock),
    ("pyperclip", _install_pyperclip_mock),
):
    if not _importable(_mod):
        _install()

# httpx et fastapi : mocks TOUJOURS installés en test, quel que soit
# l'environnement, pour garder un comportement DÉTERMINISTE.
#
# httpx : le client HTTP de test utilise httpx.MockTransport comme « seam » ;
# le mock reproduit fidèlement l'API httpx réellement utilisée par le code
# (dont request.extensions["multipart"]) . Le vrai httpx 0.28 ne peuple pas
# request.extensions["multipart"] de la même façon et n'accepte pas
# ``transport`` sur httpx.post/get — d'où un mock toujours actif (comme
# websockets).
#
# fastapi : la suite pilote les routes via le TestClient mocké (pas de
# middleware de sécurité, ni introspections starlette). Le vrai FastAPI
# injecte des middlewares (anti DNS-rebinding/CSRF, cf. app/api/security.py)
# qui rejettent le Host « testserver » et expose des objets router internes
# (``_IncludedRouter``) ; on mocke donc fastapi partout pour rester centré sur
# la logique métier, indépendamment de la version de fastapi.
_install_httpx_mock()
_install_fastapi_mock()

# websockets : mock TOUJOURS installé en test (pas de seam transport comme
# httpx.MockTransport — on ne veut JAMAIS de vraie connexion WS en test).
_install_websockets_mock()

# ---------------------------------------------------------------------------
# Fixtures pytest
# ---------------------------------------------------------------------------
import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _protect_config():
    """Sauvegarde/restaure config.json entre chaque test (les POST /api/config
    écriraient le vrai fichier du projet sinon)."""
    from app.core.config import CONFIG_PATH

    backup = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else None
    yield
    if backup is None:
        CONFIG_PATH.unlink(missing_ok=True)
    else:
        CONFIG_PATH.write_text(backup, encoding="utf-8")


@pytest.fixture()
def client():
    """Client HTTP de test (TestClient sur build_app())."""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi.testclient indisponible (pip install fastapi httpx)")
    from app.api.factory import build_app

    with TestClient(build_app()) as test_client:
        yield test_client


@pytest.fixture()
def mock_subprocess(monkeypatch):
    """Mocke subprocess.run/Popen/call/check_output pour ne JAMAIS exécuter
    wl-copy, ydotool, wtype ou toute autre commande pendant les tests
    (injector, P4). Retourne la liste des appels enregistrés."""
    import subprocess as real_subprocess

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(("run", args, kwargs))
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_popen(*args, **kwargs):
        calls.append(("popen", args, kwargs))
        return types.SimpleNamespace(
            communicate=lambda: ("", ""), returncode=0, wait=lambda: 0)

    monkeypatch.setattr(real_subprocess, "run", fake_run)
    monkeypatch.setattr(real_subprocess, "Popen", fake_popen)
    monkeypatch.setattr(real_subprocess, "call", lambda *a, **k: 0)
    monkeypatch.setattr(real_subprocess, "check_output", lambda *a, **k: b"")
    return calls
