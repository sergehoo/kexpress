"""Activation du compte par l'employé : email professionnel → éligibilité Shield → OTP → mot de
passe → compte activé (mode local) ou redirection SSO (mode Keycloak, API d'admin simulée).

Shield est SIMULÉ (`lookup_eligible` / `provision_user` remplacés) : ces tests ne dépendent pas
des modèles Shield. Keycloak n'est pas joignable : l'API d'administration est simulée.

Invariants :
1. réponses publiques non énumérantes (même statut, même corps, aucun email pour un non
   éligible ; Shield absent ou périmé → aucun OTP) ;
2. OTP (8 chiffres) à usage unique, de courte durée, à tentatives limitées (invalidé au
   plafond) et verrou CUMULATIF sur 24 h (alerte) ; renvoi soumis à un délai qui n'invalide
   jamais le code déjà reçu ; emails plafonnés par jour ; débit limité ;
3. ticket d'activation à usage unique et signé ; éligibilité revérifiée à la finalisation ;
4. conflit de provisionnement → 409 générique (vérification par l'administrateur), rien modifié ;
5. mode local : mot de passe Django validé, appareil de confiance, session (jeton d'accès dans
   le corps, rafraîchissement en cookie HttpOnly) — un employé standard lit ensuite ses
   réservations ;
6. mode SSO : compte Keycloak créé, retrouvé PAR IDENTIFIANT, ou — compte K-access existant à l'adresse
   prouvée par OTP — repris (mot de passe remplacé, sessions fermées ; jamais s'il est désactivé ou lié ailleurs), mot de
   passe fixé par l'API (temporary=false), refus de politique → message générique, réponse
   `{sso, login_hint}` sans session locale.
"""
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts import activation
from apps.accounts import keycloak_admin as kc
from apps.accounts.models import ActivationTicket, EmailOTP, TrustedDevice, User
from apps.core.enums import RoleChoices

pytestmark = pytest.mark.django_db

STRONG = "Kx-Activation-Robuste-2026!"
EMAIL = "awa.kone@kaydan.ci"
CODE_RE = re.compile(r"\b(\d{8})\b")  # codes d'activation : 8 chiffres
BAD = "00000000"


class FakeConflict(Exception):
    pass


@pytest.fixture(autouse=True)
def _env(settings):
    cache.clear()
    settings.AUTH_PUBLIC_MIN_RESPONSE_SECONDS = 0
    settings.OIDC_ENABLED = False
    settings.KEYCLOAK_ADMIN_ENABLED = False
    yield
    cache.clear()


@pytest.fixture
def shield(monkeypatch, sub_a):
    """Référentiel RH simulé : `employees` (email → fiche) ; `provision_user` crée le compte lié,
    ou lève un conflit si un compte NON lié porte déjà l'email."""
    state = SimpleNamespace(employees={}, provisioned=[])

    def add(email=EMAIL, pk=101, first_name="Awa", last_name="Koné"):
        emp = SimpleNamespace(pk=pk, email=email, first_name=first_name, last_name=last_name, user=None)
        state.employees[email] = emp
        return emp

    def lookup(email):
        return state.employees.get((email or "").strip().lower())

    def provision(employee):
        if employee.user is not None:
            return employee.user
        if User.objects.filter(email__iexact=employee.email).exists():
            raise FakeConflict("compte existant non lié")
        user = User.objects.create_user(employee.email, None, first_name=employee.first_name,
                                        last_name=employee.last_name, role=RoleChoices.REQUESTER, subsidiary=sub_a)
        employee.user = user
        state.provisioned.append(user)
        return user

    state.add = add
    monkeypatch.setattr(activation, "lookup_eligible", lookup)
    monkeypatch.setattr(activation, "provision_user", provision)
    monkeypatch.setattr(activation, "_conflict_exceptions", lambda: (FakeConflict,))
    return state



def _start_body():
    from django.conf import settings

    return {"detail": activation.GENERIC_START, "expires_in": settings.AUTH_OTP_TTL_SECONDS,
            "resend_after": settings.AUTH_OTP_RESEND_COOLDOWN_SECONDS}


def _client():
    return APIClient(REMOTE_ADDR="10.20.0.1")


def _start(email, client=None):
    return (client or _client()).post("/api/auth/activation/start/", {"email": email}, format="json")


def _verify(email, code, client=None):
    return (client or _client()).post("/api/auth/activation/verify/", {"email": email, "code": code}, format="json")


def _complete(ticket, password=STRONG, client=None, remember_me=False):
    return (client or _client()).post("/api/auth/activation/complete/",
                                      {"ticket": ticket, "password": password, "remember_me": remember_me},
                                      format="json")


def _last_code(mailoutbox):
    return CODE_RE.search(mailoutbox[-1].body).group(1)


def _ticket(mailoutbox, email=EMAIL):
    assert _start(email).status_code == 202
    response = _verify(email, _last_code(mailoutbox))
    assert response.status_code == 200, response.content
    return response.json()["ticket"]


# --- 1. Non-énumération --------------------------------------------------------------------------


def test_eligible_and_unknown_emails_get_identical_responses(shield, mailoutbox):
    shield.add()
    eligible = _start(EMAIL)
    unknown = _start("inconnu@kaydan.ci")
    assert eligible.status_code == unknown.status_code == 202
    assert eligible.json() == unknown.json() == _start_body()
    assert len(mailoutbox) == 1 and mailoutbox[0].to == [EMAIL]  # rien pour l'inconnu
    assert EmailOTP.objects.count() == 1
    assert EMAIL not in str(EmailOTP.objects.values().first())  # empreinte, jamais l'email en clair


def test_stale_or_absent_shield_sends_nothing_and_creates_nothing(shield, monkeypatch, mailoutbox):
    shield.add()
    monkeypatch.setattr(activation, "lookup_eligible", lambda email: None)  # données périmées / Shield absent
    response = _start(EMAIL)
    assert response.status_code == 202 and response.json() == _start_body()
    assert mailoutbox == [] and EmailOTP.objects.count() == 0 and not User.objects.filter(email=EMAIL).exists()


def test_shield_failure_is_never_exposed(monkeypatch, mailoutbox):
    """L'enveloppe réelle d'éligibilité avale toute erreur Shield (module simulé : aucune
    dépendance aux modèles Shield)."""
    import sys
    import types

    def boom(email):
        raise RuntimeError("Shield injoignable")

    fake = types.ModuleType("apps.shield.eligibility")
    fake.lookup_eligible = boom
    monkeypatch.setitem(sys.modules, "apps.shield.eligibility", fake)
    assert activation.lookup_eligible(EMAIL) is None
    response = _start(EMAIL)
    assert response.status_code == 202 and response.json() == _start_body()
    assert mailoutbox == [] and EmailOTP.objects.count() == 0


def test_public_responses_share_a_minimum_duration(shield, settings, monkeypatch):
    from apps.accounts import otp as otp_mod

    settings.AUTH_PUBLIC_MIN_RESPONSE_SECONDS = 0.5
    sleeps = []
    monkeypatch.setattr(otp_mod.time, "sleep", lambda s: sleeps.append(s))
    shield.add()
    _start(EMAIL)
    _start("inconnu@kaydan.ci")
    assert len(sleeps) == 2 and all(0 < s <= 0.5 for s in sleeps)


def test_already_activated_account_gets_a_notice_not_a_code(shield, mailoutbox, requester_a):
    emp = shield.add(email=requester_a.email)
    emp.user = requester_a  # compte lié, mot de passe déjà choisi
    response = _start(requester_a.email)
    assert response.status_code == 202 and response.json() == _start_body()
    assert len(mailoutbox) == 1 and "déjà activé" in mailoutbox[0].subject
    assert not CODE_RE.search(mailoutbox[0].body)
    assert EmailOTP.objects.count() == 0


def test_blocked_linked_account_gets_nothing(shield, mailoutbox, requester_a):
    emp = shield.add(email=requester_a.email)
    requester_a.set_unusable_password()
    requester_a.is_active = False
    requester_a.save()
    emp.user = requester_a
    assert _start(requester_a.email).status_code == 202
    assert mailoutbox == [] and EmailOTP.objects.count() == 0


# --- 2. OTP ------------------------------------------------------------------------------------------


def test_wrong_code_is_generic_and_counted_then_locked(shield, mailoutbox, settings):
    settings.AUTH_OTP_MAX_ATTEMPTS = 3
    shield.add()
    _start(EMAIL)
    good = _last_code(mailoutbox)
    bad = BAD if good != BAD else "11111111"
    for _ in range(3):
        r = _verify(EMAIL, bad)
        assert r.status_code == 400 and r.json() == {"detail": activation.GENERIC_VERIFY}
    otp = EmailOTP.objects.get()
    assert otp.attempts == 3 and otp.consumed_at is not None
    assert _verify(EMAIL, good).status_code == 400  # bloqué : même le bon code ne passe plus
    # Une adresse sans OTP répond exactement pareil.
    assert _verify("inconnu@kaydan.ci", good).json() == {"detail": activation.GENERIC_VERIFY}


def test_code_is_single_use(shield, mailoutbox):
    shield.add()
    _start(EMAIL)
    code = _last_code(mailoutbox)
    assert _verify(EMAIL, code).status_code == 200
    assert _verify(EMAIL, code).status_code == 400


def test_expired_code_is_refused(shield, mailoutbox):
    shield.add()
    _start(EMAIL)
    EmailOTP.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert _verify(EMAIL, _last_code(mailoutbox)).status_code == 400


def test_resend_respects_the_cooldown_and_keeps_the_code_already_received_valid(shield, mailoutbox):
    """Revue n° 15 : n'importe qui peut relancer l'activation d'une adresse ; ce renvoi ne doit
    pas rendre caduc le code que le titulaire est en train de saisir."""
    shield.add()
    _start(EMAIL)
    first = _last_code(mailoutbox)
    _start(EMAIL)  # dans le délai : rien n'est renvoyé
    assert len(mailoutbox) == 1
    EmailOTP.objects.update(last_sent_at=timezone.now() - timedelta(minutes=5))
    _start(EMAIL, APIClient(REMOTE_ADDR="10.99.0.9"))  # relance par un tiers, ailleurs
    assert len(mailoutbox) == 2
    second = _last_code(mailoutbox)
    otp = EmailOTP.objects.get()
    assert otp.sent_count == 2
    assert _verify(EMAIL, first).status_code == 200  # le code reçu en premier vaut toujours
    if first != second:
        assert _verify(EMAIL, second).status_code == 400  # OTP consommé : usage unique


def test_activation_codes_are_eight_digits(shield, mailoutbox):
    shield.add()
    _start(EMAIL)
    code = _last_code(mailoutbox)
    assert len(code) == 8
    assert _verify(EMAIL, code[:6]).status_code == 400  # un code de connexion (6) ne passe pas


def test_activation_emails_are_capped_per_day(shield, mailoutbox, settings):
    """Revue n° 15 (inondation) : au plus `AUTH_ACTIVATION_MAX_EMAILS_PER_DAY` codes par jour."""
    settings.AUTH_ACTIVATION_MAX_EMAILS_PER_DAY = 3
    shield.add()
    for _ in range(6):
        EmailOTP.objects.update(last_sent_at=timezone.now() - timedelta(minutes=5))
        assert _start(EMAIL).status_code == 202
        EmailOTP.objects.update(consumed_at=timezone.now())  # code épuisé : on en redemande un
    assert len(mailoutbox) == 3


def test_already_active_notice_is_sent_at_most_once_an_hour(shield, mailoutbox, requester_a):
    emp = shield.add(email=requester_a.email)
    emp.user = requester_a
    for _ in range(4):
        assert _start(requester_a.email).status_code == 202
    assert len(mailoutbox) == 1


def test_cumulative_failures_lock_activation_for_the_day_and_alert(shield, mailoutbox, settings, caplog):
    """Revue n° 8 : 5 tentatives × 5 codes par heure, renouvelées chaque heure, permettaient
    une devinette répartie. Au-delà de `AUTH_OTP_MAX_FAILURES_PER_DAY` codes faux en 24 h pour
    une adresse, plus aucun code n'est émis ni accepté, et une alerte est journalisée."""
    import logging

    settings.AUTH_OTP_MAX_ATTEMPTS = 2
    settings.AUTH_OTP_MAX_FAILURES_PER_DAY = 4
    shield.add()
    _start(EMAIL)
    assert [_verify(EMAIL, BAD).status_code for _ in range(2)] == [400, 400]  # 1er code bloqué
    _start(EMAIL)
    assert len(mailoutbox) == 2
    good = _last_code(mailoutbox)
    with caplog.at_level(logging.ERROR, logger="apps.accounts.otp"):
        assert _verify(EMAIL, BAD).status_code == 400
        assert _verify(EMAIL, good if good == BAD else BAD).status_code == 400  # 4e échec : verrou
    assert any("ALERTE SÉCURITÉ" in r.getMessage() for r in caplog.records)
    assert EMAIL not in caplog.text
    EmailOTP.objects.update(last_sent_at=timezone.now() - timedelta(minutes=5))
    _start(EMAIL)
    assert len(mailoutbox) == 2  # plus aucun code émis pour cette adresse
    assert EmailOTP.objects.filter(consumed_at__isnull=True).count() == 0
    # Une autre adresse n'est pas touchée.
    shield.add(email="autre.emp@kaydan.ci", pk=102)
    _start("autre.emp@kaydan.ci")
    assert len(mailoutbox) == 3 and _verify("autre.emp@kaydan.ci", _last_code(mailoutbox)).status_code == 200


def test_lockout_also_refuses_the_right_code_of_a_live_otp(shield, mailoutbox, settings):
    settings.AUTH_OTP_MAX_ATTEMPTS = 5
    settings.AUTH_OTP_MAX_FAILURES_PER_DAY = 3
    shield.add()
    _start(EMAIL)
    good = _last_code(mailoutbox)
    bad = BAD if good != BAD else "11111111"
    assert [_verify(EMAIL, bad).status_code for _ in range(3)] == [400, 400, 400]
    assert _verify(EMAIL, good).status_code == 400


def test_activation_routes_are_rate_limited(shield, monkeypatch):
    """Débit PAR DESTINATAIRE (email visé, éligible ou non : aucune énumération) et plafond large
    PAR ADRESSE IP (revue n° 11 : une vague d'activations derrière le NAT d'un bureau ne bloque
    pas les collègues, mais une seule source ne fait pas du serveur un canon à emails)."""
    from rest_framework.throttling import SimpleRateThrottle

    monkeypatch.setattr(SimpleRateThrottle, "THROTTLE_RATES",
                        {**SimpleRateThrottle.THROTTLE_RATES, "activation": "2/min", "otp_verify": "2/min",
                         "activation_ip": "5/min"})
    client = _client()
    assert [_start("x@kaydan.ci", client).status_code for _ in range(3)] == [202, 202, 429]
    # Collègues derrière la même adresse : non bloqués par la limite de la première adresse…
    assert [_start(f"y{i}@kaydan.ci", client).status_code for i in range(2)] == [202, 202]
    # … jusqu'au plafond par adresse IP.
    assert _start("z@kaydan.ci", client).status_code == 429
    assert [_verify("x@kaydan.ci", BAD, client).status_code for _ in range(3)] == [400, 400, 429]
    assert _verify("y0@kaydan.ci", BAD, client).status_code == 400  # autre destinataire : non bloqué


# --- 3. Ticket ---------------------------------------------------------------------------------------


def test_ticket_is_single_use(shield, mailoutbox):
    shield.add()
    ticket = _ticket(mailoutbox)
    assert _complete(ticket).status_code == 200
    again = _complete(ticket, "Kx-Autre-Mdp-Solide-2026?")
    assert again.status_code == 400 and again.json() == {"detail": activation.GENERIC_TICKET}
    user = User.objects.get(email=EMAIL)
    assert user.check_password(STRONG) and not user.check_password("Kx-Autre-Mdp-Solide-2026?")


def test_forged_or_expired_ticket_is_refused(shield, mailoutbox):
    shield.add()
    ticket = _ticket(mailoutbox)
    assert _complete(ticket[:-2] + "xx").status_code == 400
    ActivationTicket.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert _complete(ticket).status_code == 400
    assert not User.objects.filter(email=EMAIL).exists()


def test_eligibility_is_checked_again_at_completion(shield, mailoutbox):
    shield.add()
    ticket = _ticket(mailoutbox)
    shield.employees.clear()  # départ de l'employé / Shield périmé entre-temps
    assert _complete(ticket).status_code == 400
    assert not User.objects.filter(email=EMAIL).exists()


def test_weak_password_is_refused_and_the_ticket_survives(shield, mailoutbox):
    shield.add()
    ticket = _ticket(mailoutbox)
    weak = _complete(ticket, "12345678")
    assert weak.status_code == 400
    assert not User.objects.filter(email=EMAIL).exists()
    assert _complete(ticket).status_code == 200


def test_provisioning_conflict_is_a_generic_409(shield, mailoutbox):
    existing = User.objects.create_user(EMAIL, "Ancien-Mdp-Solide-77", role=RoleChoices.FLEET_MANAGER)
    shield.add()
    ticket = _ticket(mailoutbox)
    response = _complete(ticket)
    assert response.status_code == 409 and response.json() == {"detail": activation.GENERIC_CONFLICT}
    existing.refresh_from_db()
    assert existing.check_password("Ancien-Mdp-Solide-77") and existing.activated_at is None
    assert _complete(ticket).status_code == 400  # ticket consommé


# --- 4. Mode local : session ouverte -------------------------------------------------------------------


def test_local_activation_opens_a_cookie_session_and_trusts_the_device(shield, mailoutbox, settings):
    shield.add()
    client = _client()
    ticket = _ticket(mailoutbox)
    response = _complete(ticket, client=client, remember_me=True)
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["access"] and "refresh" not in body
    cookie = response.cookies[settings.AUTH_REFRESH_COOKIE]
    assert cookie["httponly"] and cookie["path"] == "/api/auth/" and cookie["samesite"] == "Lax"
    assert 0 < int(cookie["max-age"]) <= settings.AUTH_REMEMBER_ME_DAYS * 86400
    assert response.cookies[settings.AUTH_DEVICE_COOKIE]["httponly"]
    user = User.objects.get(email=EMAIL)
    assert user.activated_at is not None and user.check_password(STRONG)
    assert user.role == RoleChoices.REQUESTER
    device = TrustedDevice.objects.get(user=user)
    assert device.trusted and device.verified_at >= user.sessions_revoked_at
    # Un employé standard accède ensuite à ses réservations.
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {body['access']}")
    assert api.get("/api/reservations/").status_code == 200
    # Activé : une nouvelle demande ne renvoie plus de code.
    mailoutbox.clear()
    assert _start(EMAIL).status_code == 202
    assert len(mailoutbox) == 1 and not CODE_RE.search(mailoutbox[0].body)


def test_local_activation_without_remember_me_sets_a_browser_session_cookie(shield, mailoutbox, settings):
    shield.add()
    response = _complete(_ticket(mailoutbox))
    assert response.status_code == 200
    assert response.cookies[settings.AUTH_REFRESH_COOKIE]["max-age"] == ""  # cookie de session navigateur


# --- 5. Mode SSO (Keycloak simulé) ---------------------------------------------------------------------


@pytest.fixture
def keycloak(monkeypatch, settings):
    settings.OIDC_ENABLED = True
    settings.KEYCLOAK_ADMIN_ENABLED = True
    settings.AUTH_KEYCLOAK_LINK_WAIT_SECONDS = 0.3  # attente du lien écrit par une synchro concurrente
    calls = SimpleNamespace(created=[], passwords=[], confirmed=[], roles=[], existing={}, by_email={}, logouts=[])

    def create_user_strict(user, *, email_verified=False):
        calls.created.append((user.email, email_verified))
        return "kc-0001"

    def set_password(kc_id, password):
        if len(password) < 14:
            raise kc.KeycloakPasswordRejected("politique", status=400)
        calls.passwords.append((kc_id, password))

    monkeypatch.setattr(kc, "create_user_strict", create_user_strict)
    monkeypatch.setattr(kc, "set_password", set_password)
    monkeypatch.setattr(kc, "confirm_email", lambda kc_id: calls.confirmed.append(kc_id))
    monkeypatch.setattr(kc, "_sync_realm_role", lambda kc_id, role: calls.roles.append((kc_id, role)))
    monkeypatch.setattr(kc, "get_user", lambda kc_id: calls.existing.get(kc_id))
    monkeypatch.setattr(kc, "get_user_by_email", lambda email: calls.by_email.get(email))
    monkeypatch.setattr(kc, "logout_all_sessions", lambda kc_id: calls.logouts.append(kc_id))
    return calls


def test_sso_activation_sets_the_keycloak_password_and_redirects_to_sso(shield, mailoutbox, keycloak, settings):
    shield.add()
    response = _complete(_ticket(mailoutbox))
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["sso"] is True and body["login_hint"] == EMAIL
    assert "access" not in body and settings.AUTH_REFRESH_COOKIE not in response.cookies
    assert keycloak.created == [(EMAIL, True)]
    assert keycloak.passwords == [("kc-0001", STRONG)]
    assert keycloak.confirmed == ["kc-0001"] and keycloak.roles == [("kc-0001", "EMPLOYEE")]
    user = User.objects.get(email=EMAIL)
    assert user.keycloak_id == "kc-0001" and user.keycloak_sub == "kc-0001"
    assert user.activated_at is not None and not user.has_usable_password()  # aucun mot de passe local


def test_sso_password_policy_refusal_is_generic_and_retryable(shield, mailoutbox, keycloak):
    shield.add()
    ticket = _ticket(mailoutbox)
    refused = _complete(ticket, "Kx-Court-26!x")  # validé par Django, refusé par la politique Keycloak
    assert refused.status_code == 400 and refused.json() == {"detail": activation.GENERIC_SSO_PASSWORD}
    assert User.objects.get(email=EMAIL).activated_at is None
    keycloak.existing["kc-0001"] = {"id": "kc-0001"}
    ok = _complete(ticket)
    assert ok.status_code == 200 and keycloak.created == [(EMAIL, True)]  # pas de second compte SSO
    assert keycloak.passwords == [("kc-0001", STRONG)]


def _conflict(monkeypatch):
    def conflict(user, *, email_verified=False):
        raise kc.KeycloakConflict("existe", status=409)

    monkeypatch.setattr(kc, "create_user_strict", conflict)


def test_existing_kaccess_account_is_taken_over_only_by_the_proven_mailbox(shield, mailoutbox, keycloak, monkeypatch):
    """Employé qui a déjà un compte K-access : après la preuve OTP de son adresse, le compte est
    repris — mot de passe remplacé par celui qu'il choisit, sessions K-access fermées."""
    _conflict(monkeypatch)
    keycloak.by_email[EMAIL] = {"id": "kc-old", "email": EMAIL, "enabled": True}
    shield.add()
    assert _start(EMAIL).status_code == 202
    verified = _verify(EMAIL, _last_code(mailoutbox))
    assert verified.status_code == 200 and verified.json()["existing_sso_account"] is True
    response = _complete(verified.json()["ticket"])
    assert response.status_code == 200, response.content
    assert keycloak.passwords == [("kc-old", STRONG)] and keycloak.logouts == ["kc-old"]
    assert keycloak.confirmed == ["kc-old"]
    user = User.objects.get(email=EMAIL)
    assert user.keycloak_sub == "kc-old" == user.keycloak_id and user.activated_at is not None


@pytest.mark.parametrize("case", ["disabled", "other_email", "linked_elsewhere", "absent"])
def test_existing_kaccess_account_is_never_taken_over_when_unsafe(shield, mailoutbox, keycloak, monkeypatch, case,
                                                                  sub_a):
    _conflict(monkeypatch)
    if case == "disabled":
        keycloak.by_email[EMAIL] = {"id": "kc-old", "email": EMAIL, "enabled": False}
    elif case == "other_email":
        keycloak.by_email[EMAIL] = {"id": "kc-old", "email": "autre@kaydan.ci", "enabled": True}
    elif case == "linked_elsewhere":
        keycloak.by_email[EMAIL] = {"id": "kc-old", "email": EMAIL, "enabled": True}
        other = User.objects.create_user("deja.lie@kaydan.ci", None, role="requester", subsidiary=sub_a)
        User.objects.filter(pk=other.pk).update(keycloak_sub="kc-old", keycloak_id="kc-old")
    shield.add()
    response = _complete(_ticket(mailoutbox))
    assert response.status_code == 409 and response.json() == {"detail": activation.GENERIC_CONFLICT}
    assert keycloak.passwords == [] and keycloak.logouts == []
    assert User.objects.get(email=EMAIL).activated_at is None


def test_sso_conflict_raised_by_a_concurrent_kexpress_sync_is_not_a_false_409(shield, mailoutbox, keycloak,
                                                                               monkeypatch):
    """Revue (non sondé) : la synchro Keycloak que Shield programme à la création du compte peut
    créer le compte SSO juste avant `create_user_strict` (conflit) et écrire le LIEN un instant
    plus tard. L'activation attend ce lien (jamais d'adoption par email) au lieu d'un faux 409."""
    import time as time_mod

    pending = {}

    def concurrent_sync(user, *, email_verified=False):
        pending["user"] = user.pk
        raise kc.KeycloakConflict("créé par la synchro", status=409)

    real_sleep = time_mod.sleep

    def sleep(seconds):  # pendant l'attente, la synchro concurrente écrit le lien
        if pending.pop("user", None) is not None:
            User.objects.filter(email=EMAIL).update(keycloak_id="kc-sync", keycloak_sub="kc-sync")
        real_sleep(0)

    monkeypatch.setattr(kc, "create_user_strict", concurrent_sync)
    monkeypatch.setattr(time_mod, "sleep", sleep)
    shield.add()
    response = _complete(_ticket(mailoutbox))
    assert response.status_code == 200, response.content
    assert keycloak.passwords == [("kc-sync", STRONG)]  # sur le compte LIÉ, aucun second compte
    assert User.objects.get(email=EMAIL).activated_at is not None


def test_sso_keycloak_id_found_by_email_is_not_a_link(shield, mailoutbox, keycloak, sub_a):
    """Un `keycloak_id` adopté par une ancienne synchro par email (sans `keycloak_sub`) ne suffit
    pas : aucun mot de passe n'est posé sur ce compte SSO."""
    emp = shield.add()
    user = User.objects.create_user(EMAIL, None, role=RoleChoices.REQUESTER, subsidiary=sub_a)
    User.objects.filter(pk=user.pk).update(keycloak_id="kc-etranger")
    emp.user = User.objects.get(pk=user.pk)
    response = _complete(_ticket(mailoutbox))
    assert response.status_code == 409
    assert keycloak.passwords == [] and keycloak.created == []


def test_sso_keycloak_unavailable_is_a_503_and_the_ticket_survives(shield, mailoutbox, keycloak, monkeypatch):
    def down(user, *, email_verified=False):
        raise kc.KeycloakAdminError("injoignable")

    monkeypatch.setattr(kc, "create_user_strict", down)
    shield.add()
    ticket = _ticket(mailoutbox)
    assert _complete(ticket).status_code == 503
    monkeypatch.setattr(kc, "create_user_strict", lambda user, *, email_verified=False: "kc-0002")
    assert _complete(ticket).status_code == 200


def test_sso_without_admin_api_refuses_activation(shield, mailoutbox, settings):
    settings.OIDC_ENABLED = True
    settings.KEYCLOAK_ADMIN_ENABLED = False
    shield.add()
    ticket = _ticket(mailoutbox)
    assert _complete(ticket).status_code == 503
    assert not User.objects.filter(email=EMAIL).exists()


def test_sso_activation_never_reenables_a_disabled_keycloak_account(shield, mailoutbox, keycloak, sub_a):
    """Revue n° 14 : un compte Keycloak désactivé (verrou anti-force brute permanent, décision
    d'un administrateur) n'est jamais réactivé par l'activation, et aucun mot de passe n'y est
    posé."""
    emp = shield.add()
    user = User.objects.create_user(EMAIL, None, role=RoleChoices.REQUESTER, subsidiary=sub_a)
    User.objects.filter(pk=user.pk).update(keycloak_id="kc-lie", keycloak_sub="kc-lie")
    emp.user = User.objects.get(pk=user.pk)
    keycloak.existing["kc-lie"] = {"id": "kc-lie", "enabled": False}
    response = _complete(_ticket(mailoutbox))
    assert response.status_code == 409 and response.json() == {"detail": activation.GENERIC_CONFLICT}
    assert keycloak.passwords == [] and keycloak.confirmed == []
    assert User.objects.get(pk=user.pk).activated_at is None


def test_keycloak_confirm_email_never_touches_enabled(monkeypatch):
    calls = []

    def api(method, path, body=None, params=None, _retry=True):
        calls.append((method, body))
        return 200, dict(state), {}

    monkeypatch.setattr(kc, "_api", api)
    state = {"id": "kc-1", "enabled": True, "requiredActions": ["VERIFY_EMAIL", "CONFIGURE_TOTP"]}
    kc.confirm_email("kc-1")
    put = [body for method, body in calls if method == "PUT"][0]
    assert "enabled" not in put and put["emailVerified"] is True and put["requiredActions"] == ["CONFIGURE_TOTP"]
    state = {"id": "kc-1", "enabled": False}
    calls.clear()
    with pytest.raises(kc.KeycloakAccountDisabled):
        kc.confirm_email("kc-1")
    assert [m for m, _ in calls] == ["GET"]  # aucune écriture


def test_migration_backfills_activated_at_for_accounts_already_opened(sub_a):
    """Comptes ouverts avant la migration (connexion SSO faite, mot de passe local choisi) :
    marqués activés — une demande d'activation ne réinitialise pas leur mot de passe SSO."""
    import importlib

    from django.apps import apps as registry

    migration = importlib.import_module("apps.accounts.migrations.0008_auth_otp_devices")
    sso = User.objects.create_user("deja.sso@kaydan.ci", None, role=RoleChoices.REQUESTER, subsidiary=sub_a)
    User.objects.filter(pk=sso.pk).update(keycloak_sub="kc-deja")
    local = User.objects.create_user("deja.local@kaydan.ci", STRONG, role=RoleChoices.REQUESTER, subsidiary=sub_a)
    fresh = User.objects.create_user("jamais@kaydan.ci", None, role=RoleChoices.REQUESTER, subsidiary=sub_a)
    migration.backfill_activated_at(registry, None)
    assert User.objects.get(pk=sso.pk).activated_at is not None
    assert User.objects.get(pk=local.pk).activated_at is not None
    assert User.objects.get(pk=fresh.pk).activated_at is None


def test_codes_are_never_written_to_logs_when_no_real_email_backend(shield, settings, capsys, mailoutbox):
    """Revue n° 1 : hors DEBUG, le backend console écrirait les codes dans les journaux du
    conteneur ; aucun code ne part, la réponse publique reste identique, l'erreur est journalisée
    sans code, et le contrôle de déploiement le signale."""
    from django.core.checks import run_checks

    settings.DEBUG = False
    settings.EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    shield.add()
    response = _start(EMAIL)
    assert response.status_code == 202 and response.json() == _start_body()
    assert not CODE_RE.search(capsys.readouterr().out)
    assert EmailOTP.objects.count() == 0  # aucun code n'est même émis
    errors = [m for m in run_checks(include_deployment_checks=True) if m.id == "accounts.E010"]
    assert errors and errors[0].level >= 40
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    assert not [m for m in run_checks(include_deployment_checks=True) if m.id == "accounts.E010"]
    # Envoi réparé : le code part tout de suite (aucun code fantôme ni délai de renvoi hérité).
    assert _start(EMAIL).status_code == 202 and len(mailoutbox) == 1 and CODE_RE.search(mailoutbox[0].body)
