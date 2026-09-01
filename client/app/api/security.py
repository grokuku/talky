# -*- coding: utf-8 -*-
"""
app/api/security.py
===================
Protection CSRF / DNS-rebinding des endpoints REST (S4) : filtrage du header
``Host`` puis ``Origin`` appliqué par un middleware de factory.py aux chemins
``/api/*`` (toutes méthodes). Le WebSocket /ws garde SON propre filtrage
(websocket.py). La règle same-origin (Origin == Host) NE protège PAS contre le
DNS-rebinding : un hôte « evil.example.com » rebondi vers 127.0.0.1 passerait
la comparaison same-origin (les deux « 127.0.0.1 »). Le DNS-rebinding est donc
bloqué par une validation dédiée du header Host (anti-rebinding), et le CSRF
par la validation Origin qui suit.

Logique PURE, testable sans fastapi ni serveur : ``allowed_origins()``,
``origin_allowed()`` et ``host_allowed()`` ne dépendent d'aucun framework
(lecture d'environnement + parsing d'URL) ; ``origin_guard()`` ne fait que
brancher la décision sur une réponse 403 JSON et ne requiert d'une requête
que ``.headers`` (duck typing : starlette Request en production, objet
factice en test — cf. tests/test_security_origin.py ; le mock fastapi de
tests/conftest.py n'expose pas ``middleware``, d'où l'enregistrement défensif
dans factory.py).

Filtrage en DEUX temps (origin_guard) :
  a. Validation du header Host (anti DNS-rebinding) : hostname extrait de
     ``Host``, sans port. Doit être un littéral IP (v4/v6), localhost /
     127.0.0.1 / ::1, ou une entrée nommée de TALKY_ALLOWED_ORIGINS. Un
     header Host absent (ex. curl) est accepté — les règles Origin
     s'appliquent alors seules.
  b. Validation du header Origin (anti CSRF) : règles ci-dessous.

Trade-off : l'accès LAN par IP (http://192.168.x.x:8000) est accepté par
défaut (le hostname est un littéral IP), mais l'accès par NOM de machine
(ex. http://mon-pc.lan:8000) exige d'ajouter l'origine nommée dans
TALKY_ALLOWED_ORIGINS — un hostname nommé n'est ni IP ni localhost.

Règles d'acceptation Origin (il suffit qu'UNE s'applique) :
  1. Origin absente/vide   : curl (GUIDE_E2E), navigation directe GET,
     server-to-server — un navigateur ne peut PAS omettre Origin sur une
     requête cross-origin déclenchée par du JS, donc absence d'Origin
     signifie « pas une page web distante ».
  2. Origin locale         : localhost / 127.0.0.1 / [::1], quel que soit le
     port (panneau web servi par le client lui-même ; un port différent
     couvre un front de dev type Vite qui proxifie l'API).
  3. TALKY_ALLOWED_ORIGINS : origine listée explicitement (CSV, même
     variable que le filtrage du WS dans websocket.py) — cas reverse-proxy
     ou interface nommée. Les entrées doivent inclure le SCHEME
     (http://host[:port]) : une entrée sans scheme ne matche jamais une
     Origin, car une Origin possède toujours un scheme. Comparaison
     normalisée (autorité host:port), jamais en string brute.
  4. Same-origin           : host:port de l'Origin == header Host de la
     requête — l'accès LAN par IP reste accepté en complément.

Comparaisons insensibles à la casse, slash final toléré ;
``allowed_origins()`` relit l'environnement à chaque appel (aucun état figé
à l'import), ce qui permet le monkeypatch en test.

Comparaisons insensibles à la casse, slash final toléré ;
``allowed_origins()`` relit l'environnement à chaque appel (aucun état figé
à l'import), ce qui permet le monkeypatch en test.
"""

import ipaddress
import os
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse

# Hosts locaux : une Origin pointant dessus est toujours de confiance (le
# panneau est servi par le client lui-même).
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def allowed_origins() -> set[str]:
    """
    Origines explicitement autorisées via TALKY_ALLOWED_ORIGINS (CSV).

    Séparateur virgule, espaces tolérés autour de chaque entrée, entrées
    vides ignorées ; variable absente ou vide -> set vide. Relecture à
    chaque appel (pas de cache à l'import) : rester testable (monkeypatch)
    et réactif à un changement d'environnement.
    """
    return {
        origin.strip()
        for origin in os.environ.get("TALKY_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    }


def _authority(value: str) -> tuple[str, int] | None:
    """
    Décompose une autorité — header Origin (« scheme://host[:port] ») ou
    header Host (« host[:port] ») — en (host, port) normalisés ; None si
    non analysable (valeur vide, port invalide, ...).

    Une valeur sans schéma (header Host) est préfixée http:// pour
    réutiliser le parsing urlsplit (gère l'IPv6 entre crochets) ; un port
    absent vaut le port par défaut du schéma (80, 443 pour https) afin que
    « http://h » et « Host: h:80 » soient reconnus identiques. Le host est
    replié en minuscules (hostnames insensibles à la casse).

    NB : le slash final éventuel d'une Origin est ignoré de fait (il
    appartient au path, pas à l'autorité).
    """
    text = (value or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = f"http://{text}"
    try:
        parts = urlsplit(text)
        hostname = (parts.hostname or "").lower()
        port = parts.port  # ValueError si port invalide (3.6+)
    except ValueError:
        return None
    if not hostname:
        return None
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    return hostname, port


def _host_only(value: str) -> str:
    """
    Extrait le hostname (sans port) d'un header Host / d'une autorité
    quelconque. ``h:8000`` -> ``h`` ; ``[::1]:8000`` -> ``::1`` ; ``[::1]``
    -> ``::1``. Hostname replié en minuscules ; ``""`` si non analysable
    (vide, port invalide, ...).
    """
    authority = _authority(value)
    return authority[0] if authority is not None else ""


def host_allowed(host: str) -> bool:
    """
    Décision pure anti DNS-rebinding : le hostname fourni (extraduit du
    header ``Host``, sans port) est-il acceptable à l'adresse du serveur ?

    True si :
      * aucun host fourni (header Host absent) — les règles Origin
        s'appliquent alors seules ;
      * le hostname est un littéral IP (v4 ou v6, via ipaddress) : 127.0.0.1,
        n'importe quelle IP de LAN 192.168.x.x / 10.x.x.x / ... — un
        rebinding vers une adresse ré-écrite en nom n'est ainsi jamais
        accepté ;
      * localhost / 127.0.0.1 / ::1 (couverture explicite, redondante avec
        le test IP pour lisibilité) ;
      * le hostname correspond au HOST d'une entrée de TALKY_ALLOWED_ORIGINS
        (comparaison d'autorité normalisée via ``_authority``) — couvre un
        accès LAN par NOM de machine (ex. http://mon-pc.lan:8000) ou un nom
        reverse-proxy, à condition que le hostname nommé soit listé.

    False dans tout autre cas (hostname nommé non listé) -> 403 côté
    middleware : un attaquant rebondissant un nom de domaine vers l'IP du
    serveur est bloqué avant même l'analyse de l'Origin.
    """
    hostname = _host_only(host)
    if not hostname:                       # Host absent : Origin décide seule
        return True
    try:                                   # littéral IP (v4 ou v6)
        ipaddress.ip_address(hostname)
        return True
    except ValueError:
        pass
    if hostname in _LOCAL_HOSTS:           # localhost / 127.0.0.1 / ::1
        return True
    # Hostname nommé : accepté uniquement s'il est listé explicitement.
    for entry in allowed_origins():
        authority = _authority(entry)
        if authority is not None and authority[0] == hostname:
            return True
    return False


def origin_allowed(origin: str, host: str) -> bool:
    """
    Décision pure : la paire (Origin, Host) est-elle acceptable ?

    ``origin`` : header Origin (« scheme://host[:port] », possiblement vide
    — curl, navigation GET) ; ``host`` : header Host de la requête. True si
    l'une des règles 1-4 du docstring module s'applique, sinon False
    (origine tierce -> 403 côté middleware).

    NB : la règle 4 (same-origin) exige le MÊME host:port — un port
    différent est cross-origin (http://192.168.0.10:9999 vers un serveur
    sur :8000 reste refusé).
    """
    origin = (origin or "").strip()
    if not origin:  # Règle 1 : pas d'Origin -> pas une page web distante
        return True

    origin_authority = _authority(origin)

    # Règle 2 : Origin locale, tout port (panneau web du client, front de dev).
    if origin_authority is not None and origin_authority[0] in _LOCAL_HOSTS:
        return True

    # Règle 3 : origine explicitement autorisée. Comparaison NORMALISÉE par
    # autorité (host:port par défaut implicites / casse / IPv6…) via
    # ``_authority``, et jamais en string brute. Garde le contrat : une entrée
    # SANS scheme ("10.0.0.2:8000", "mon-pc.lan") ne matche jamais une
    # Origin, car une Origin a toujours un scheme (http://…) ; seules les
    # entrées "http://host[:port]" sont donc comparées.
    for entry in allowed_origins():
        if "://" not in entry:            # entrée sans scheme : ne matche pas
            continue
        if _authority(entry) == origin_authority:
            return True

    # Règle 4 : same-origin — host:port de l'Origin == header Host.
    host_authority = _authority(host or "")
    if (
        origin_authority is not None
        and host_authority is not None
        and origin_authority == host_authority
    ):
        return True

    return False


def origin_guard(request) -> JSONResponse | None:
    """
    Garde-fou à appeler depuis un middleware : None si la requête est
    acceptable, sinon une 403 JSON {"detail": "Forbidden origin"} (même
    façon de créer des réponses JSON que le reste de l'API client, cf.
    routes_config.py).

    Contre le DNS-rebinding : on vérifie la validité du header ``Host``
    (hostname extrait, sans port) AVANT les règles Origin — un Host nommé
    non autorisé (ou mal formé) est bloqué en 403, même si l'Origin
    passerait. Un Host absent est accepté (les règles Origin s'appliquent
    seules). Contre le CSRF : puis ``origin_allowed()``.

    Ne lit que ``request.headers`` (Headers starlette en production,
    insensible à la casse ; un dict simple suffit en test).
    """
    host = request.headers.get("host", "")
    hostname = _host_only(host)
    if not host_allowed(hostname):
        return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
    origin = request.headers.get("origin", "")
    if origin_allowed(origin, host):
        return None
    return JSONResponse({"detail": "Forbidden origin"}, status_code=403)
