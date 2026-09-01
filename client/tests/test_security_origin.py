# -*- coding: utf-8 -*-
"""
tests/test_security_origin.py
=============================
Tests unitaires PURS de la protection CSRF / DNS-rebinding du REST (S4) :
la décision ``origin_allowed()`` et le garde ``origin_guard()`` de
app/api/security.py — SANS fastapi (requête factice minimale : seule
``.headers`` est lue) ni serveur. Le middleware lui-même n'est pas
enregistré sous le mock fastapi de conftest.py ; la logique de décision
étant pure, elle se teste directement ici.

Cas couverts : Origin absente (curl GUIDE_E2E / navigation GET), localhost
tout port (panneau web, front de dev), same-origin LAN par IP (trailing
slash), TALKY_ALLOWED_ORIGINS, origine malveillante -> 403 JSON, casse
insensible, port différent rejeté, parsing de l'env (CSV / trim / vide).

L'environnement est piloté par monkeypatch (TALKY_ALLOWED_ORIGINS) :
allowed_origins() relit la variable à chaque appel, aucun état à
réinitialiser entre les tests.
"""

import json

from app.api.security import (
    allowed_origins,
    host_allowed,
    origin_allowed,
    origin_guard,
)


# ---------------------------------------------------------------------------
# Requête factice minimale (origin_guard ne lit que ``request.headers``)
# ---------------------------------------------------------------------------
class _FakeRequest:
    """Équivalent minimal d'une Request starlette pour origin_guard()."""

    def __init__(self, headers=None):
        self.headers = headers or {}


def _response_payload(response):
    """Payload JSON d'une réponse brute, mock conftest OU fastapi réel.

    Le mock JSONResponse (conftest.py) expose ``.content`` (dict) ; la vraie
    starlette expose ``.body`` (bytes JSON) — on couvre les deux sans
    dépendre de l'un ni de l'autre.
    """
    content = getattr(response, "content", None)
    if isinstance(content, (dict, list)):  # mock conftest
        return content
    return json.loads(response.body)  # fastapi réel


# ---------------------------------------------------------------------------
# Règle 1 : Origin absente (curl GUIDE_E2E, navigation GET, server-to-server)
# ---------------------------------------------------------------------------
def test_origin_absente_autorisee(monkeypatch):
    """Pas d'Origin -> requête acceptée (curl de GUIDE_E2E, GET navigué)."""
    monkeypatch.delenv("TALKY_ALLOWED_ORIGINS", raising=False)
    assert origin_allowed("", "127.0.0.1:8000")
    assert origin_guard(_FakeRequest({"host": "127.0.0.1:8000"})) is None


# ---------------------------------------------------------------------------
# Règle 2 : Origin locale, tout port (panneau web servi par le client)
# ---------------------------------------------------------------------------
def test_origin_localhost_autorisee(monkeypatch):
    """localhost / 127.0.0.1 acceptés ; un front de dev (:3000, Vite) aussi."""
    monkeypatch.delenv("TALKY_ALLOWED_ORIGINS", raising=False)
    assert origin_allowed("http://localhost:8000", "127.0.0.1:8000")
    assert origin_allowed("http://127.0.0.1:8000", "127.0.0.1:8000")
    assert origin_allowed("http://localhost:3000", "127.0.0.1:8000")


def test_origin_loopback_ipv6_et_sans_port(monkeypatch):
    """[::1] quel que soit le port ; localhost sans port explicite."""
    monkeypatch.delenv("TALKY_ALLOWED_ORIGINS", raising=False)
    assert origin_allowed("http://[::1]:8000", "127.0.0.1:8000")
    assert origin_allowed("http://localhost", "localhost")


# ---------------------------------------------------------------------------
# Règle 4 : same-origin (accès LAN par IP, zéro configuration)
# ---------------------------------------------------------------------------
def test_origin_lan_same_origin_autorisee(monkeypatch):
    """Origin http://192.168.x.x:8000 == Host -> accepté, guard -> None."""
    monkeypatch.delenv("TALKY_ALLOWED_ORIGINS", raising=False)
    assert origin_allowed("http://192.168.1.50:8000", "192.168.1.50:8000")
    assert origin_guard(_FakeRequest({
        "origin": "http://192.168.1.50:8000",
        "host": "192.168.1.50:8000",
    })) is None


def test_origin_lan_trailing_slash_autorisee(monkeypatch):
    """Slash final sur l'Origin toléré (règle 4, same-origin LAN)."""
    monkeypatch.delenv("TALKY_ALLOWED_ORIGINS", raising=False)
    assert origin_allowed("http://192.168.1.50:8000/", "192.168.1.50:8000")


# ---------------------------------------------------------------------------
# Règle 3 : origine explicitement autorisée via TALKY_ALLOWED_ORIGINS
# ---------------------------------------------------------------------------
def test_talky_allowed_origins_autorisee(monkeypatch):
    """Origine listée dans l'env : acceptée même depuis un Host différent."""
    monkeypatch.setenv("TALKY_ALLOWED_ORIGINS",
                       "https://panneau.example.org, http://10.0.0.2:8000")
    assert origin_allowed("https://panneau.example.org", "127.0.0.1:8000")
    assert origin_allowed("http://10.0.0.2:8000", "192.168.1.50:8000")
    assert origin_guard(_FakeRequest({
        "origin": "https://panneau.example.org",
        "host": "127.0.0.1:8000",
    })) is None


# ---------------------------------------------------------------------------
# Refus : origine malveillante -> 403 JSON {"detail": "Forbidden origin"}
# ---------------------------------------------------------------------------
def test_origin_malveillante_rejetee_403(monkeypatch):
    """Page web distante (CSRF) : Origin tierce -> refus + 403 JSON."""
    monkeypatch.delenv("TALKY_ALLOWED_ORIGINS", raising=False)
    assert not origin_allowed("http://evil.example.com", "127.0.0.1:8000")
    # Host absent : la règle 4 ne peut s'appliquer, l'origine tierce reste
    # refusée (robustesse au parsing).
    assert not origin_allowed("http://evil.example.com", "")
    response = origin_guard(_FakeRequest({
        "origin": "http://evil.example.com",
        "host": "127.0.0.1:8000",
    }))
    assert response is not None
    assert response.status_code == 403
    assert _response_payload(response) == {"detail": "Forbidden origin"}


# ---------------------------------------------------------------------------
# Tolérances : casse insensible (règles 2 et 3)
# ---------------------------------------------------------------------------
def test_casse_insensible(monkeypatch):
    """Casse libre sur hostnames (Origin) ET entrées de TALKY_ALLOWED_ORIGINS."""
    monkeypatch.setenv("TALKY_ALLOWED_ORIGINS", "HTTPS://Panneau.Example.ORG")
    assert origin_allowed("http://LOCALHOST:8000", "127.0.0.1:8000")
    assert origin_allowed("https://PANNEAU.example.org", "127.0.0.1:8000")


# ---------------------------------------------------------------------------
# Refus : port différent = cross-origin (règle 4 exige le même host:port)
# ---------------------------------------------------------------------------
def test_port_different_rejete(monkeypatch):
    """192.168.x.x:9999 vers un serveur :8000 est refusé ; une origine
    tierce (non locale) ne passe jamais par la règle 2, quel que soit son
    port."""
    monkeypatch.delenv("TALKY_ALLOWED_ORIGINS", raising=False)
    assert not origin_allowed("http://192.168.1.50:9999", "192.168.1.50:8000")
    assert not origin_allowed("http://evil.example.com:8000",
                              "192.168.1.50:8000")


# ---------------------------------------------------------------------------
# Parsing de TALKY_ALLOWED_ORIGINS (CSV, trim, vide)
# ---------------------------------------------------------------------------
def test_allowed_origins_parse(monkeypatch):
    """allowed_origins() : CSV, espaces trimés, entrées vides ignorées ;
    variable absente ou vide -> set vide."""
    monkeypatch.delenv("TALKY_ALLOWED_ORIGINS", raising=False)
    assert allowed_origins() == set()
    monkeypatch.setenv("TALKY_ALLOWED_ORIGINS", "")
    assert allowed_origins() == set()
    monkeypatch.setenv("TALKY_ALLOWED_ORIGINS",
                       " http://a.example.org , ,https://b.example.org,")
    assert allowed_origins() == {"http://a.example.org",
                                 "https://b.example.org"}


# ---------------------------------------------------------------------------
# Validation du header Host (anti DNS-rebinding, S4)
# ---------------------------------------------------------------------------
def test_host_evil_domain_rejected_403(monkeypatch):
    """DNS-rebinding : Host nommé non autorisé (même avec une Origin valide /
    absente) -> 403. C'est ce que la seule règle same-origin ne bloquait pas."""
    monkeypatch.delenv("TALKY_ALLOWED_ORIGINS", raising=False)
    assert not host_allowed("evil.example.com")
    assert not host_allowed("evil.example.com:8000")
    # Aucune Origin : la validation Host décide seule -> 403 JSON.
    response = origin_guard(_FakeRequest({"host": "evil.example.com:8000"}))
    assert response is not None
    assert response.status_code == 403
    assert _response_payload(response) == {"detail": "Forbidden origin"}


def test_host_lan_ip_autorisé(monkeypatch):
    """Accès LAN par IP (192.168.x.x) : littéral IP -> accepté par défaut."""
    monkeypatch.delenv("TALKY_ALLOWED_ORIGINS", raising=False)
    assert host_allowed("192.168.1.50")
    assert host_allowed("192.168.1.50:8000")
    assert origin_guard(_FakeRequest({"host": "192.168.1.50:8000"})) is None


def test_host_localhost_autorisé(monkeypatch):
    """Host localhost / 127.0.0.1 -> accepté par défaut."""
    monkeypatch.delenv("TALKY_ALLOWED_ORIGINS", raising=False)
    assert host_allowed("localhost")
    assert host_allowed("localhost:8000")
    assert host_allowed("127.0.0.1")
    assert origin_guard(_FakeRequest({"host": "localhost:8000"})) is None


def test_host_ipv6_autorisé(monkeypatch):
    """Host IPv6 littéral (entre crochets) -> accepté par défaut."""
    monkeypatch.delenv("TALKY_ALLOWED_ORIGINS", raising=False)
    assert host_allowed("::1")
    assert host_allowed("[::1]:8000")
    assert host_allowed("fd00::1")
    assert host_allowed("[fd00::1]:9090")
    assert origin_guard(_FakeRequest({"host": "[::1]:8000"})) is None


def test_host_in_talky_allowed_origins_autorisé(monkeypatch):
    """Accès LAN par NOM de machine : nécessite une entrée
    TALKY_ALLOWED_ORIGINS (trade-off documenté dans security.py)."""
    monkeypatch.setenv("TALKY_ALLOWED_ORIGINS", "http://mon-pc.lan:8000")
    assert host_allowed("mon-pc.lan")
    assert host_allowed("mon-pc.lan:8000")
    assert origin_guard(_FakeRequest({"host": "mon-pc.lan:8000"})) is None
    # Hostname NON listé -> refusé.
    monkeypatch.setenv("TALKY_ALLOWED_ORIGINS", "")
    assert not host_allowed("autrepc.lan")


def test_origin_null_rejetee_403(monkeypatch):
    """« Origin: null » (littéral envoyé par certains navigateurs) : la
    validation Host passe (IP LAN) mais l'Origin « null » est tierce -> 403."""
    monkeypatch.delenv("TALKY_ALLOWED_ORIGINS", raising=False)
    assert not origin_allowed("null", "127.0.0.1:8000")
    response = origin_guard(_FakeRequest({
        "origin": "null",
        "host": "127.0.0.1:8000",
    }))
    assert response is not None
    assert response.status_code == 403
    assert _response_payload(response) == {"detail": "Forbidden origin"}
