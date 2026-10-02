"""Faux serveur Kaydan Shield pour les tests (aucun réseau).

`FakeShield` se branche à la place d'un `requests.Session` (`ShieldClient(session=fake)`) et
simule : connexion JWT (`/auth/login/`), renouvellement (`/auth/token/refresh/`), expiration du
jeton (401), pagination limit/offset avec `next`, tri `-updated_at` (honoré, refusé ou ignoré),
filtres `status` / `tenant`, détail d'un employé (404 si absent) et pannes injectées.
"""
from __future__ import annotations

import itertools
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests

LOGIN = "/api/v1/auth/login/"
REFRESH = "/api/v1/auth/token/refresh/"
COMPANIES = "/api/v1/core/companies/"
DEPARTMENTS = "/api/v1/employees/departments/"
EMPLOYEES = "/api/v1/employees/employees/"

_NO_JSON = object()


class FakeResponse:
    def __init__(self, status_code: int = 200, data=None, headers: dict | None = None):
        self.status_code = status_code
        self._data = data
        self.headers = headers or {}

    def json(self):
        if self._data is _NO_JSON:
            raise ValueError("pas de JSON")
        return self._data


def no_json(status_code: int = 502) -> FakeResponse:
    return FakeResponse(status_code, _NO_JSON)


def company(cid: int, code: str = "", name: str = "", *, tenant: int = 1, is_active: bool = True) -> dict:
    return {"id": cid, "uuid": f"00000000-0000-4000-8000-{cid:012d}", "code": code or f"C{cid}",
            "name": name or f"Filiale {cid}", "is_active": is_active, "tenant": tenant}


def department(did: int, company_id: int, name: str = "", code: str = "") -> dict:
    return {"id": did, "code": code or f"D{did}", "name": name or f"Service {did}", "company": company_id,
            "parent": None}


def employee(eid: int, company_id: int, *, email: str | None = None, status: str = "active",
             updated_at: str = "2026-09-01T08:00:00Z", department_id: int | None = None,
             first_name: str = "", last_name: str = "", tenant: int = 1, **extra) -> dict:
    row = {
        "id": eid, "uuid": f"10000000-0000-4000-8000-{eid:012d}",
        "email": email if email is not None else f"emp{eid}@kaydan.test",
        "first_name": first_name or f"Prénom{eid}", "last_name": last_name or f"Nom{eid}",
        "status": status, "company": company_id, "department": department_id, "manager": None,
        "position": None, "job_title": "Agent", "matricule": f"M{eid:05d}", "updated_at": updated_at,
        "ended_at": None, "user": None, "tenant": tenant,
        # Champs sensibles que Shield renvoie et que K-Express ne doit JAMAIS stocker :
        "id_number": "CNI-SECRET", "date_of_birth": "1990-01-01", "phone": "+2250700000000",
        "address": "Rue secrète", "photo": "https://shield.test/p.jpg",
        "emergency_contact_name": "Contact", "emergency_contact_phone": "+2250100000000",
    }
    row.update(extra)
    return row


class FakeShield:
    """Remplaçant de `requests.Session` : seul `request()` est utilisé par le client."""

    def __init__(self, base: str = "https://shield.test", *, companies=(), departments=(), employees=(),
                 username: str = "svc@kaydan.test", password: str = "s3cret-pw", login_field: str = "email"):
        self.base = base.rstrip("/")
        self.companies = list(companies)
        self.departments = list(departments)
        self.employees = list(employees)
        self.username, self.password, self.login_field = username, password, login_field
        self.calls: list[dict] = []
        self.failures: list[dict] = []
        self.ordering_mode = "honor"  # honor | reject | ignore
        self.login_override = None
        self._seq = itertools.count(1)
        self.valid_access: set[str] = set()
        self.valid_refresh: set[str] = set()
        self.next_host: str | None = None  # simule un `next` pointant vers un autre hôte
        self.omit_next = False  # simule une réponse sans `next` (repli sur `count`)

    # --- Pilotage des tests ------------------------------------------------------------

    def fail(self, *, path: str | None = None, method: str | None = None, times: int = 1, response=None,
             exc: Exception | None = None, when=None):
        """Injecte une panne (réponse ou exception) pour les `times` prochains appels ciblés."""
        self.failures.append({"path": path, "method": method, "times": times, "response": response,
                              "exc": exc, "when": when})

    def expire_tokens(self):
        self.valid_access.clear()

    def calls_to(self, path: str, method: str = "GET") -> list[dict]:
        return [c for c in self.calls if c["path"] == path and c["method"] == method]

    # --- Interface requests.Session ------------------------------------------------------

    def request(self, method, url, params=None, json=None, headers=None, timeout=None, verify=None, **kw):
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        query.update({k: str(v) for k, v in (params or {}).items()})
        call = {"method": method, "path": parts.path, "query": query, "json": json,
                "headers": dict(headers or {}), "host": parts.netloc}
        self.calls.append(call)
        for f in list(self.failures):
            if f["path"] and f["path"] != parts.path:
                continue
            if f["method"] and f["method"] != method:
                continue
            if f["when"] and not f["when"](call):
                continue
            f["times"] -= 1
            if f["times"] <= 0:
                self.failures.remove(f)
            if f["exc"] is not None:
                raise f["exc"]
            return f["response"]
        if parts.path == LOGIN and method == "POST":
            return self._login(json or {})
        if parts.path == REFRESH and method == "POST":
            return self._refresh(json or {})
        auth = (headers or {}).get("Authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] not in self.valid_access:
            return FakeResponse(401, {"detail": "Jeton invalide"})
        if method != "GET":
            return FakeResponse(405, {"detail": "lecture seule"})
        if parts.path == COMPANIES:
            return self._page(parts.path, query, self.companies)
        if parts.path == DEPARTMENTS:
            return self._page(parts.path, query, self.departments)
        if parts.path == EMPLOYEES:
            return self._employees(parts.path, query)
        if parts.path.startswith(EMPLOYEES):
            tail = parts.path[len(EMPLOYEES):].strip("/")
            if tail.isdigit():
                for row in self.employees:
                    if row["id"] == int(tail):
                        return FakeResponse(200, dict(row))
                return FakeResponse(404, {"detail": "Introuvable"})
        return FakeResponse(404, {"detail": "Introuvable"})

    # --- Simulations -------------------------------------------------------------------

    def _issue(self) -> dict:
        n = next(self._seq)
        access, refresh = f"acc-{n}", f"ref-{n}"
        self.valid_access.add(access)
        self.valid_refresh.add(refresh)
        return {"access": access, "refresh": refresh}

    def _login(self, body: dict) -> FakeResponse:
        if self.login_override is not None:
            return self.login_override
        if body.get(self.login_field) != self.username or body.get("password") != self.password:
            return FakeResponse(401, {"detail": "Identifiants invalides"})
        return FakeResponse(200, self._issue())

    def _refresh(self, body: dict) -> FakeResponse:
        token = body.get("refresh")
        if token not in self.valid_refresh:
            return FakeResponse(401, {"detail": "Jeton de renouvellement invalide"})
        n = next(self._seq)
        access = f"acc-{n}"
        self.valid_access.add(access)
        return FakeResponse(200, {"access": access})

    def _employees(self, path: str, query: dict) -> FakeResponse:
        rows = list(self.employees)
        if query.get("status"):
            rows = [r for r in rows if r.get("status") == query["status"]]
        if query.get("tenant"):
            rows = [r for r in rows if str(r.get("tenant")) == query["tenant"]]
        ordering = query.get("ordering", "")
        if ordering == "-updated_at":
            if self.ordering_mode == "reject":
                return FakeResponse(400, {"ordering": ["Tri invalide"]})
            if self.ordering_mode == "honor":
                rows.sort(key=lambda r: (r.get("updated_at") or "", r["id"]), reverse=True)
            else:
                rows.sort(key=lambda r: r["id"])
        else:
            rows.sort(key=lambda r: r["id"])
        return self._page(path, query, rows)

    def _page(self, path: str, query: dict, rows: list) -> FakeResponse:
        limit = int(query.get("limit") or 100)
        offset = int(query.get("offset") or 0)
        chunk = rows[offset:offset + limit]
        nxt = None
        if offset + limit < len(rows) and not self.omit_next:
            q = dict(query)
            q["offset"] = str(offset + limit)
            q["limit"] = str(limit)
            host = self.next_host or self.base
            nxt = f"{host}{path}?{urlencode(q)}"
        prev = None
        return FakeResponse(200, {"count": len(rows), "next": nxt, "previous": prev,
                                  "results": [dict(r) for r in chunk]})


def connection_error() -> Exception:
    return requests.ConnectionError("connexion refusée")
