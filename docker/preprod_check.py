"""Contrôle de préproduction : HTTPS, en-têtes, CORS, cookies de session, appareil reconnu,
rafraîchissement, révocation.

    python docker/preprod_check.py --front https://kexpress.exemple.ci --api https://api.exemple.ci/api \
        --email compte.recette@exemple.ci [--remember]

Le mot de passe et le code reçu par email sont DEMANDÉS au clavier (jamais en argument, jamais
affichés). En mode SSO, seul l'accès de secours d'un super administrateur passe par ce
parcours ; les comptes K-access se vérifient dans le navigateur (cf. docs/PREPRODUCTION.md).
Nécessite `requests`. Code de sortie 1 si un contrôle échoue.
"""
import argparse
import getpass
import sys
from urllib.parse import urlsplit

import requests

FAILED = []


def check(label, ok, detail=""):
    print(f"[{'OK' if ok else 'ÉCHEC'}] {label}{' — ' + detail if detail else ''}")
    if not ok:
        FAILED.append(label)


def cookie_attrs(response, name):
    for raw in response.raw.headers.get_all("Set-Cookie") or []:
        if raw.startswith(f"{name}="):
            parts = [p.strip() for p in raw.split(";")]
            attrs = {p.split("=", 1)[0].lower(): (p.split("=", 1)[1] if "=" in p else True) for p in parts[1:]}
            return attrs
    return None


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--front", required=True)
    p.add_argument("--api", required=True, help="Base de l'API, ex. https://api.exemple.ci/api")
    p.add_argument("--email", required=True)
    p.add_argument("--remember", action="store_true", help="Tester « Rester connecté ».")
    a = p.parse_args()
    front, api = a.front.rstrip("/"), a.api.rstrip("/")
    for url in (front, api):
        check(f"HTTPS {url}", urlsplit(url).scheme == "https")

    r = requests.get(front, timeout=15)
    hsts = r.headers.get("Strict-Transport-Security", "")
    check("HSTS sur le frontend", "max-age=" in hsts, hsts or "absent")
    http = requests.get("http://" + urlsplit(front).netloc, timeout=15, allow_redirects=False)
    check("HTTP → HTTPS", http.status_code in (301, 302, 307, 308)
          and http.headers.get("Location", "").startswith("https://"), str(http.status_code))
    pre = requests.options(f"{api}/auth/token/", timeout=15, headers={
        "Origin": front, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
    check("CORS : origine du frontend autorisée", pre.headers.get("Access-Control-Allow-Origin") == front)
    check("CORS : cookies autorisés", pre.headers.get("Access-Control-Allow-Credentials") == "true")
    evil = requests.options(f"{api}/auth/token/", timeout=15, headers={
        "Origin": "https://attaquant.example", "Access-Control-Request-Method": "POST"})
    check("CORS : origine étrangère refusée", evil.headers.get("Access-Control-Allow-Origin") != "https://attaquant.example")

    s = requests.Session()
    s.headers.update({"Origin": front, "Referer": front + "/login"})
    password = getpass.getpass("Mot de passe du compte de recette : ")
    r = s.post(f"{api}/auth/token/", json={"email": a.email, "password": password, "remember_me": a.remember}, timeout=20)
    del password
    access = None
    if r.status_code == 202:
        challenge = r.json().get("challenge")
        code = getpass.getpass("Code reçu par email : ")
        r = s.post(f"{api}/auth/token/otp/", json={"challenge": challenge, "code": code, "trust_device": True},
                   timeout=20)
    check("Connexion (mot de passe + code)", r.status_code == 200, f"HTTP {r.status_code}")
    if r.status_code != 200:
        return finish()
    access = r.json().get("access")
    for name in ("kx_refresh", "kx_device"):
        attrs = cookie_attrs(r, name)
        check(f"Cookie {name} posé", attrs is not None)
        if attrs is None:
            continue
        check(f"{name} : HttpOnly", "httponly" in attrs)
        check(f"{name} : Secure", "secure" in attrs)
        check(f"{name} : SameSite Lax ou Strict", str(attrs.get("samesite", "")).lower() in ("lax", "strict"),
              str(attrs.get("samesite")))
        if name == "kx_refresh":
            persistent = "max-age" in attrs or "expires" in attrs
            check("Session persistante seulement avec « Rester connecté »", persistent == a.remember,
                  f"max-age={attrs.get('max-age')}")
        print(f"      domaine={attrs.get('domain', '(hôte de l’API)')} chemin={attrs.get('path')}")
    check("Aucun jeton de rafraîchissement dans la réponse JSON", "refresh" not in r.json())
    me = s.get(f"{api}/auth/me/", headers={"Authorization": f"Bearer {access}"}, timeout=15)
    check("Jeton d'accès accepté", me.status_code == 200)
    ref = s.post(f"{api}/auth/refresh/", timeout=15)
    check("Rafraîchissement par cookie", ref.status_code == 200, f"HTTP {ref.status_code}")
    if ref.status_code == 200:
        access = ref.json().get("access", access)
    again = requests.post(f"{api}/auth/refresh/", timeout=15, headers={"Origin": front})
    check("Rafraîchissement refusé sans cookie", again.status_code in (400, 401, 403))
    out = s.post(f"{api}/auth/devices/revoke-all/", headers={"Authorization": f"Bearer {access}"}, timeout=15)
    check("Déconnexion de tous les appareils", out.status_code in (200, 204), f"HTTP {out.status_code}")
    check("Jeton d'accès révoqué", s.get(f"{api}/auth/me/", headers={"Authorization": f"Bearer {access}"},
                                         timeout=15).status_code == 401)
    check("Rafraîchissement révoqué", s.post(f"{api}/auth/refresh/", timeout=15).status_code in (400, 401, 403))
    return finish()


def finish():
    print("\n" + ("Tous les contrôles sont passés." if not FAILED else f"{len(FAILED)} contrôle(s) en échec."))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
