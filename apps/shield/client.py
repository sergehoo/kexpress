"""Client HTTP de l'API Kaydan Shield (lecture seule, serveur à serveur).

Authentification : JWT Bearer obtenu par `POST /api/v1/auth/login/` avec un compte de SERVICE
(corps non documenté par le schéma OpenAPI : le nom du champ identifiant est réglable par
`SHIELD_LOGIN_ID_FIELD`, le mot de passe part dans `password`). Sur 401, le jeton d'accès est
renouvelé UNE fois (`POST /api/v1/auth/token/refresh/`), puis, si le renouvellement échoue, une
nouvelle connexion est tentée. Les clés d'API Shield ne sont PAS utilisées : elles servent aux
périmètres IoT / intégration et ne sont pas déclarées sur les points « employés ».

Résilience : nouvelle tentative avec attente exponentielle sur 5xx, 429 (en-tête `Retry-After`
respecté) et erreurs réseau ; au-delà, `ShieldUnavailable`. Pagination limit/offset : la page
suivante est lue depuis `next` (réécrite sur l'origine configurée — le jeton ne part jamais vers
un autre hôte) ou, à défaut, recalculée par offset. Toute requête vers un autre hôte que
`SHIELD_BASE_URL` (curseur de reprise enregistré avant un changement d'URL, ou altéré) est
refusée ; hors DEBUG, `SHIELD_BASE_URL` doit être en HTTPS (le mot de passe du compte de
service ne circule jamais en clair).

Jamais de journalisation du mot de passe ni des jetons : seuls méthode, chemin et code HTTP.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import requests
from django.conf import settings

logger = logging.getLogger("apps.shield.client")

LOGIN_PATH = "/api/v1/auth/login/"
REFRESH_PATH = "/api/v1/auth/token/refresh/"
COMPANIES_PATH = "/api/v1/core/companies/"
DEPARTMENTS_PATH = "/api/v1/employees/departments/"
EMPLOYEES_PATH = "/api/v1/employees/employees/"

#: Codes qui justifient une nouvelle tentative (indisponibilité passagère, débit limité).
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
#: Garde-fou : une pagination qui ne finit pas est une anomalie, pas une donnée.
MAX_PAGES = 5000
#: Plafond d'attente imposé par un `Retry-After` (secondes).
MAX_RETRY_AFTER = 120.0

#: Clés de réponse de connexion qui signalent une étape MFA (compte de service mal configuré).
_MFA_KEYS = ("mfa_required", "mfa", "otp_required", "mfa_token", "challenge", "challenge_id",
             "two_factor_required", "requires_mfa")


class ShieldError(Exception):
    """Erreur générique du connecteur Shield (message sûr, sans secret)."""


class ShieldUnavailable(ShieldError):
    """Shield injoignable ou en erreur après toutes les tentatives (synchro reprenable)."""


class ShieldAuthError(ShieldError):
    """Authentification / autorisation refusée ou compte de service mal configuré."""


class ShieldRequestError(ShieldError):
    """Requête refusée par Shield (4xx hors authentification) — ex. tri non supporté."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ShieldNotFound(ShieldRequestError):
    """Ressource inexistante ou hors du périmètre du compte de service (404)."""


@dataclass
class Page:
    """Une page de résultats et le curseur permettant de lire la suivante."""

    results: list
    offset: int
    count: int | None
    next_url: str | None
    next_offset: int | None
    raw_next: str | None = field(default=None, repr=False)

    @property
    def has_next(self) -> bool:
        return self.next_url is not None or self.next_offset is not None


class ShieldClient:
    """Client Shield. `session` injectable (tests : faux `requests.Session`, aucun réseau)."""

    def __init__(self, session=None, *, base_url=None, username=None, password=None,
                 login_field=None, timeout=None, verify=None, page_size=None,
                 max_retries: int = 4, backoff: float = 1.0, sleep=time.sleep):
        self.session = session or requests.Session()
        self.base_url = (base_url if base_url is not None else settings.SHIELD_BASE_URL).rstrip("/")
        self._username = username if username is not None else settings.SHIELD_USERNAME
        self._password = password if password is not None else settings.SHIELD_PASSWORD
        self.login_field = login_field or settings.SHIELD_LOGIN_ID_FIELD or "email"
        self.timeout = timeout if timeout is not None else settings.SHIELD_TIMEOUT_SECONDS
        self.verify = verify if verify is not None else settings.SHIELD_VERIFY_TLS
        self.page_size = max(1, int(page_size or settings.SHIELD_PAGE_SIZE or 100))
        self.max_retries = max(0, int(max_retries))
        self.backoff = float(backoff)
        self._sleep = sleep
        self._access: str | None = None
        self._refresh: str | None = None
        parts = urlsplit(self.base_url)
        self._origin = (parts.scheme, parts.netloc)

    def __repr__(self):  # jamais de secret dans une représentation
        return f"<ShieldClient {self.base_url}>"

    # --- Authentification -----------------------------------------------------------

    @property
    def authenticated(self) -> bool:
        return bool(self._access)

    def login(self) -> None:
        """Ouvre une session avec le compte de service (jetons gardés en mémoire seulement)."""
        if not self._username or not self._password:
            raise ShieldAuthError("Compte de service Shield non configuré (SHIELD_USERNAME / SHIELD_PASSWORD).")
        body = {self.login_field: self._username, "password": self._password}
        resp = self._send("POST", self._url(LOGIN_PATH), json=body, auth=False)
        if resp.status_code in (400, 401, 403):
            raise ShieldAuthError(
                f"Connexion Shield refusée (HTTP {resp.status_code}) : vérifier l'identifiant "
                f"(champ « {self.login_field} », cf. SHIELD_LOGIN_ID_FIELD) et le mot de passe du compte de service.")
        if resp.status_code >= 400:
            raise ShieldUnavailable(f"Connexion Shield impossible (HTTP {resp.status_code}).")
        data = _json(resp)
        access, refresh = _extract_tokens(data)
        if not access:
            if isinstance(data, dict) and any(data.get(k) for k in _MFA_KEYS):
                logger.error("Shield exige une double authentification pour le compte de service : "
                             "utiliser un compte de service SANS MFA (lecture filiales, départements, employés).")
                raise ShieldAuthError("Le compte de service Shield exige une double authentification (MFA) : "
                                      "configuration à corriger (compte de service sans MFA).")
            raise ShieldAuthError("Réponse de connexion Shield inattendue : aucun jeton d'accès.")
        self._access, self._refresh = access, refresh
        logger.info("Session Shield ouverte (%s).", self.base_url)

    def refresh(self) -> bool:
        """Renouvelle le jeton d'accès ; False si le renouvellement est refusé."""
        if not self._refresh:
            return False
        resp = self._send("POST", self._url(REFRESH_PATH), json={"refresh": self._refresh}, auth=False)
        if resp.status_code >= 400:
            if resp.status_code in RETRY_STATUSES:
                raise ShieldUnavailable(f"Renouvellement du jeton Shield impossible (HTTP {resp.status_code}).")
            logger.info("Renouvellement du jeton Shield refusé (HTTP %s) : nouvelle connexion.", resp.status_code)
            self._access = None
            return False
        access, refresh = _extract_tokens(_json(resp))
        if not access:
            self._access = None
            return False
        self._access = access
        if refresh:  # rotation des jetons de renouvellement
            self._refresh = refresh
        return True

    def _ensure_auth(self) -> None:
        if not self._access:
            self.login()

    # --- Requêtes --------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _check_destination(self, url: str) -> None:
        """Garde-fou de TOUTE requête : origine configurée seulement, HTTPS hors DEBUG."""
        parts = urlsplit(url)
        if (parts.scheme, parts.netloc) != self._origin:
            raise ShieldRequestError("Requête Shield refusée : hôte différent de SHIELD_BASE_URL "
                                     "(le jeton ne part jamais vers un autre hôte).")
        if self._origin[0] != "https" and not getattr(settings, "DEBUG", False):
            raise ShieldAuthError("SHIELD_BASE_URL doit être en https:// : le mot de passe du compte de "
                                  "service et les jetons ne circulent jamais en clair.")

    def _send(self, method: str, url: str, *, params=None, json=None, auth=True):
        """Envoi avec nouvelles tentatives (5xx, 429, réseau). Renvoie la dernière réponse
        (codes 4xx hors 429 rendus tels quels à l'appelant)."""
        self._check_destination(url)
        attempt = 0
        path = urlsplit(url).path
        while True:
            headers = {"Accept": "application/json"}
            if auth and self._access:
                headers["Authorization"] = f"Bearer {self._access}"
            try:
                resp = self.session.request(method, url, params=params, json=json, headers=headers,
                                            timeout=self.timeout, verify=self.verify)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self.max_retries:
                    raise ShieldUnavailable(f"Shield injoignable ({type(exc).__name__}) sur {path}.") from None
                delay = self._delay(attempt, None)
                logger.warning("Shield injoignable sur %s %s (%s) : nouvelle tentative dans %.1fs.",
                               method, path, type(exc).__name__, delay)
                self._sleep(delay)
                attempt += 1
                continue
            if resp.status_code in RETRY_STATUSES:
                if attempt >= self.max_retries:
                    raise ShieldUnavailable(f"Shield indisponible (HTTP {resp.status_code}) sur {path}.")
                delay = self._delay(attempt, resp.headers.get("Retry-After"))
                logger.warning("Shield HTTP %s sur %s %s : nouvelle tentative dans %.1fs.",
                               resp.status_code, method, path, delay)
                self._sleep(delay)
                attempt += 1
                continue
            return resp

    def _delay(self, attempt: int, retry_after) -> float:
        if retry_after:
            seconds = _parse_retry_after(retry_after)
            if seconds is not None:
                return min(max(seconds, 0.0), MAX_RETRY_AFTER)
        base = self.backoff * (2 ** attempt)
        return min(base + random.uniform(0, self.backoff / 2 if self.backoff else 0), 60.0)

    def get_json(self, url_or_path: str, params: dict | None = None):
        """GET authentifié : 401 → renouvellement (une fois) puis reconnexion ; 403 → erreur
        d'autorisation explicite ; autre 4xx → `ShieldRequestError`."""
        if url_or_path.startswith(("http://", "https://")):
            url = self._safe_next(url_or_path)
            if url is None:
                raise ShieldRequestError("Requête Shield refusée : hôte différent de SHIELD_BASE_URL.")
        else:
            url = self._url(url_or_path)
        self._ensure_auth()
        resp = self._send("GET", url, params=params)
        if resp.status_code == 401:
            if not self.refresh():
                self.login()
            resp = self._send("GET", url, params=params)
            if resp.status_code == 401:
                raise ShieldAuthError("Jeton Shield refusé même après reconnexion du compte de service.")
        path = urlsplit(url).path
        if resp.status_code == 403:
            raise ShieldAuthError(f"Accès refusé par Shield à {path} : le compte de service doit pouvoir "
                                  "lire les filiales, départements et employés.")
        if resp.status_code == 404:
            raise ShieldNotFound(f"Ressource Shield introuvable : {path}.", 404)
        if resp.status_code >= 400:
            raise ShieldRequestError(f"Requête Shield refusée (HTTP {resp.status_code}) sur {path}.",
                                     resp.status_code)
        return _json(resp)

    # --- Pagination ------------------------------------------------------------------

    def _safe_next(self, next_url: str | None) -> str | None:
        """`next` réécrit sur l'origine configurée ; None s'il pointe vers un autre hôte."""
        if not next_url:
            return None
        parts = urlsplit(next_url)
        if parts.netloc and parts.netloc != self._origin[1]:
            logger.warning("Lien de pagination Shield vers un autre hôte ignoré (repli par offset).")
            return None
        return urlunsplit((self._origin[0], self._origin[1], parts.path, parts.query, ""))

    def fetch_page(self, path: str, params: dict | None = None, *, offset: int = 0,
                   url: str | None = None) -> Page:
        """Lit UNE page (par `url` de reprise si fournie, sinon par `path` + offset). Une `url`
        d'un autre hôte (curseur enregistré avant un changement de SHIELD_BASE_URL, ou altéré)
        n'est jamais suivie : repli par offset sur l'origine configurée."""
        if url and self._safe_next(url) is None:
            url = None
        if url:
            url = self._safe_next(url)
            data = self.get_json(url)
            q = parse_qs(urlsplit(url).query)
            offset = _int(q.get("offset", [offset])[0], offset)
        else:
            query = dict(params or {})
            query.update({"limit": self.page_size, "offset": offset})
            data = self.get_json(path, params=query)
        if isinstance(data, list):  # point non paginé
            return Page(results=data, offset=offset, count=len(data), next_url=None, next_offset=None)
        if not isinstance(data, dict):
            raise ShieldRequestError("Réponse Shield illisible (liste attendue).")
        results = data.get("results") or []
        count = data.get("count")
        raw_next = data.get("next")
        next_url = self._safe_next(raw_next)
        next_offset = None
        if raw_next:
            q = parse_qs(urlsplit(raw_next).query)
            next_offset = _int(q.get("offset", [None])[0], None)
            if next_offset is None:
                next_offset = offset + len(results)
            if next_url is None and path:
                # Repli : même requête que la nôtre, offset recalculé, jamais l'hôte étranger.
                query = dict(params or {})
                query.update({"limit": self.page_size, "offset": next_offset})
                next_url = f"{self._url(path)}?{urlencode(query)}"
        elif isinstance(count, int) and results and offset + len(results) < count:
            next_offset = offset + len(results)  # `next` absent mais total non atteint
            query = dict(params or {})
            query.update({"limit": self.page_size, "offset": next_offset})
            next_url = f"{self._url(path)}?{urlencode(query)}"
        return Page(results=results, offset=offset, count=count if isinstance(count, int) else None,
                    next_url=next_url, next_offset=next_offset, raw_next=raw_next)

    def iter_pages(self, path: str, params: dict | None = None, *, offset: int = 0,
                   url: str | None = None):
        """Itère les pages (reprise possible par `url` ou `offset`)."""
        seen = set()
        for _ in range(MAX_PAGES):
            page = self.fetch_page(path, params, offset=offset, url=url)
            yield page
            if not page.has_next or not page.results:
                return
            key = (page.next_url, page.next_offset)
            if key in seen:
                raise ShieldRequestError("Pagination Shield en boucle (page suivante déjà lue).")
            seen.add(key)
            url, offset = page.next_url, page.next_offset or 0
        raise ShieldRequestError("Pagination Shield anormalement longue (garde-fou atteint).")

    def list_all(self, path: str, params: dict | None = None) -> list:
        out: list = []
        for page in self.iter_pages(path, params):
            out.extend(page.results)
        return out

    # --- Points métier ---------------------------------------------------------------

    def list_companies(self) -> list:
        return self.list_all(COMPANIES_PATH, {"ordering": "id"})

    def list_departments(self) -> list:
        return self.list_all(DEPARTMENTS_PATH, {"ordering": "id"})

    def employee_params(self, *, ordering: str = "id") -> dict:
        params = {"ordering": ordering}
        if settings.SHIELD_TENANT_ID:
            params["tenant"] = settings.SHIELD_TENANT_ID
        return params

    def get_employee(self, shield_id: int) -> dict:
        return self.get_json(f"{EMPLOYEES_PATH}{int(shield_id)}/")


# --- Utilitaires -------------------------------------------------------------------------


def _json(resp):
    try:
        return resp.json()
    except ValueError:
        raise ShieldRequestError(f"Réponse Shield non JSON (HTTP {resp.status_code}).", resp.status_code) from None


def _extract_tokens(data) -> tuple[str | None, str | None]:
    """Jetons d'une réponse de connexion / renouvellement (formats usuels tolérés)."""
    if not isinstance(data, dict):
        return None, None
    nested = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
    access = data.get("access") or data.get("access_token") or nested.get("access") or nested.get("access_token")
    refresh = data.get("refresh") or data.get("refresh_token") or nested.get("refresh") or nested.get("refresh_token")
    return (access if isinstance(access, str) and access else None,
            refresh if isinstance(refresh, str) and refresh else None)


def _parse_retry_after(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        from django.utils import timezone

        when = parsedate_to_datetime(str(value))
        return (when - timezone.now()).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None


def _int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
