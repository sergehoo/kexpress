"""P0 — Aucun mot de passe par défaut : comptes sans mot de passe utilisable + invitation.

Invariants tenus ici, chacun par un test qui échouerait si la règle était retirée :

1. l'ancien mot de passe par défaut n'existe plus dans le code applicatif (backend, réglages,
   frontend), et aucun compte créé par l'API ne s'ouvre avec lui ;
2. un compte créé par POST /api/employees/ naît SANS mot de passe utilisable ; un mot de passe
   fourni à la création est refusé (400, rien n'est créé) ; la réponse ne porte jamais le lien ;
   UNE invitation part à la SEULE adresse du titulaire ; avec le SSO, aucune invitation locale ;
   un incident d'envoi ne défait pas la création (201) et il est journalisé ;
3. /api/auth/password-setup/ : lien valide → email du titulaire, définition du mot de passe
   puis connexion ; lien à usage unique, signé (altéré / autre uid → 400), à durée limitée
   (expiré → 400), validateurs Django (faible → 400, rien ne change), compte inactif → 400 ;
   route anonyme qui ne révèle pas l'existence d'une adresse ; débit limité (scope dédié) ;
4. POST /api/employees/<id>/invite/ : l'invitation part au titulaire seul, jamais à l'appelant ;
   auditeur → 403 ; admin d'une filiale sœur → refus ;
5. l'anti-escalade tient : plafond des mots de passe (compte plus puissant), auto-promotion ;
6. seed_demo : sans DEMO_PASSWORD, comptes sans mot de passe utilisable et liens d'invitation
   affichés ; avec DEMO_PASSWORD, ce mot de passe choisi par l'opérateur ;
7. la migration de révocation rend inutilisable l'ancien mot de passe partout où il est actif,
   sans toucher aux autres comptes.

Le mot de passe historique est reconstruit à l'exécution : ce fichier ne le contient pas
littéralement (sinon le scan du point 1 n'aurait aucun sens pour un futur scan du dépôt).
"""
import importlib
import io
import logging
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from django.apps import apps as django_apps
from django.conf import settings
from django.contrib.auth.password_validation import validate_password
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db.migrations.recorder import MigrationRecorder
from django.utils import translation
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle

from apps.accounts.invitations import invitation_link, resolve
from apps.accounts.models import User
from apps.accounts.serializers import EmployeeWriteSerializer
from apps.accounts.sessions import invitation_tokens
from apps.accounts.views import PasswordSetupView
from apps.audit.models import AuditLog
from apps.core.enums import RoleChoices

pytestmark = pytest.mark.django_db

#: Ancien mot de passe par défaut, reconstruit (jamais écrit d'un bloc dans ce fichier).
FORMER_DEFAULT = "demo" + "1234"
STRONG = "Kx-Robuste-Invit-2026!"
REPO = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r"https?://\S+/auth/setup-password\?\S+")


# --- Outillage -----------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_throttle_cache():
    """Le compteur de débit vit dans le cache (LocMem, partagé par le processus) : chaque test
    repart de zéro, sinon les appels des tests précédents finiraient en 429."""
    cache.clear()
    yield
    cache.clear()


def _client(user=None):
    c = APIClient(HTTP_HOST="127.0.0.1")
    if user is not None:
        c.force_authenticate(user)
    return c


def _user(email, role, subsidiary=None, password="pw", **extra):
    return User.objects.create_user(email, password, role=role, subsidiary=subsidiary, **extra)


def _invitee(email="invite@test.io", subsidiary=None, role=RoleChoices.REQUESTER):
    """Compte tel que l'API le crée : sans mot de passe utilisable."""
    u = User.objects.create_user(email, None, role=role, subsidiary=subsidiary, first_name="Invité")
    assert not u.has_usable_password()
    return u


def _uid_token(link):
    qs = parse_qs(urlparse(link).query)
    return qs["uid"][0], qs["token"][0]


def _links_in(body):
    return LINK_RE.findall(body)


def _payload(email, role=RoleChoices.REQUESTER, **extra):
    return {"email": email, "first_name": "Nouveau", "last_name": "Compte", "role": role, **extra}


def _create(client, payload, capture):
    """POST /api/employees/ en exécutant les hooks on_commit (invitation, synchro SSO)."""
    with capture(execute=True):
        return client.post("/api/employees/", payload, format="json")


def _login(email, password):
    return _client().post("/api/auth/token/", {"email": email, "password": password}, format="json")


def _login_with_otp(email, password):
    """Connexion complète : mot de passe (202 + code par email sur un appareil inconnu ou pour un
    rôle à MFA renforcée — cf. tests/test_auth_login.py), puis code → session (200 + access)."""
    from django.core import mail

    first = _login(email, password)
    if first.status_code != 202:
        return first
    code = re.search(r"\b(\d{6})\b", mail.outbox[-1].body).group(1)
    return _client().post("/api/auth/token/otp/", {"challenge": first.json()["challenge"], "code": code},
                          format="json")


def _aged_token(user, hours):
    """Jeton d'invitation émis il y a `hours` heures (horodatage passé, signature valide)."""
    gen = invitation_tokens
    ts = gen._num_seconds(gen._now() - timedelta(hours=hours))
    return gen._make_token_with_timestamp(user, ts, gen.secret)


def _setup_get(uid, token, client=None):
    return (client or _client()).get("/api/auth/password-setup/", {"uid": uid, "token": token})


def _setup_post(uid, token, password, client=None):
    return (client or _client()).post("/api/auth/password-setup/",
                                      {"uid": uid, "token": token, "password": password}, format="json")


@pytest.fixture
def auditor(db):
    return _user("audit@test.io", RoleChoices.AUDITOR)


@pytest.fixture
def group_finance(db):
    """Financier GROUPE (sans filiale) : détient `pay_expense`, que l'admin entreprise n'a pas."""
    return _user("fin@test.io", RoleChoices.FINANCE)


# --- 1. L'ancien mot de passe par défaut n'existe plus ---------------------------------


#: Toute construction du littéral : d'un bloc, ou concaténé ("demo" + "1234", "demo" "1234").
_FORMER_PATTERN = re.compile(rb"demo[\s'\"`+,]*1234", re.IGNORECASE)
#: Seule exception : la migration qui RÉVOQUE l'ancienne valeur (elle doit la reconnaître).
_REVOKE_MIGRATION = REPO / "apps/accounts/migrations/0005_revoke_default_password.py"
_SKIP_DIRS = {"__pycache__", "node_modules", ".next", ".turbo", "dist", "build", "coverage"}


def _scanned_files():
    for root in (REPO / "apps", REPO / "config", REPO / "frontend" / "src"):
        assert root.is_dir(), f"répertoire attendu absent : {root}"
        for path in root.rglob("*"):
            if path.is_file() and not (_SKIP_DIRS & set(path.parts)) and path.suffix not in (".pyc", ".mo"):
                yield path


def test_former_default_password_is_absent_from_application_code():
    """Le mot de passe historique n'apparaît nulle part dans apps/, config/, frontend/src —
    ni en clair, ni reconstruit par concaténation — hormis la migration qui le révoque."""
    files = list(_scanned_files())
    assert any(p.suffix == ".py" for p in files) and any(p.suffix in (".ts", ".tsx") for p in files)
    offenders = [str(p.relative_to(REPO)) for p in files
                 if p != _REVOKE_MIGRATION and _FORMER_PATTERN.search(p.read_bytes())]
    assert offenders == []
    # Contrôle du détecteur : il reconnaît bien la forme reconstruite de la migration.
    assert _FORMER_PATTERN.search(_REVOKE_MIGRATION.read_bytes())


def test_no_api_created_account_opens_with_former_default(company_admin, sub_a, django_capture_on_commit_callbacks):
    """Aucun compte créé par l'API (tous rôles) ne s'ouvre avec l'ancien mot de passe par
    défaut ; et le fournir explicitement à la création est refusé, rien n'est créé. (Un compte
    Finance, qui paie, est créé par un super administrateur : plafond des droits attribués.)"""
    c = _client(company_admin)
    root = _client(User.objects.create_user("root-p0@test.io", "Racine-Solide-91", role=RoleChoices.SUPER_ADMIN))
    emails = []
    for i, (role, sub) in enumerate([(RoleChoices.REQUESTER, sub_a), (RoleChoices.FLEET_MANAGER, sub_a),
                                     (RoleChoices.FINANCE, sub_a), (RoleChoices.DRIVER, sub_a)]):
        email = f"api{i}@test.io"
        creator = root if role == RoleChoices.FINANCE else c
        r = _create(creator, _payload(email, role, subsidiary=str(sub.pk)), django_capture_on_commit_callbacks)
        assert r.status_code == 201, r.content
        emails.append(email)
    for email in emails:
        user = User.objects.get(email=email)
        assert not user.check_password(FORMER_DEFAULT)
        assert _login(email, FORMER_DEFAULT).status_code == 401

    r = _create(c, _payload("force@test.io", password=FORMER_DEFAULT), django_capture_on_commit_callbacks)
    assert r.status_code == 400 and "password" in r.json()
    assert not User.objects.filter(email="force@test.io").exists()
    assert _login("force@test.io", FORMER_DEFAULT).status_code == 401


# --- 2. Création : aucun mot de passe utilisable, invitation au seul titulaire ----------


@pytest.mark.parametrize("creator", ["company_admin", "admin_a"])
def test_api_creation_gives_unusable_password_and_one_invitation_to_holder_only(
        request, creator, sub_a, mailoutbox, django_capture_on_commit_callbacks):
    """Créé par un admin entreprise ou un admin de filiale : le compte n'a pas de mot de passe
    utilisable ; UN email part à SA seule adresse (ni copie, ni copie cachée, ni l'admin) ; le
    lien qu'il porte est valide pour ce compte ; la réponse ne contient ni lien ni jeton ;
    l'envoi est tracé au journal d'audit au nom de l'admin."""
    admin = request.getfixturevalue(creator)
    r = _create(_client(admin), _payload("nouveau@test.io", subsidiary=str(sub_a.pk)),
                django_capture_on_commit_callbacks)
    assert r.status_code == 201, r.content
    user = User.objects.get(email="nouveau@test.io")
    assert user.subsidiary_id == sub_a.pk
    assert user.has_usable_password() is False
    assert _login("nouveau@test.io", "pw").status_code == 401  # aucun mot de passe ne l'ouvre

    assert len(mailoutbox) == 1
    mail = mailoutbox[0]
    assert mail.to == ["nouveau@test.io"] and mail.cc == [] and mail.bcc == []
    assert admin.email not in mail.recipients()
    links = _links_in(mail.body)
    assert len(links) == 1
    assert links[0].startswith(settings.FRONTEND_URL.rstrip("/") + "/auth/setup-password?")
    uid, token = _uid_token(links[0])
    assert resolve(uid, token) == user

    body = r.content.decode()
    assert token not in body and "setup-password" not in body
    assert "password" not in r.json() and "token" not in r.json()
    assert AuditLog.objects.filter(actor=admin, target_id=str(user.pk),
                                   changes__action="send_invitation").count() == 1


def test_creation_with_a_password_is_refused_and_creates_nothing(
        company_admin, admin_a, mailoutbox, django_capture_on_commit_callbacks):
    """Un mot de passe choisi à la création (même robuste) est refusé : 400 sur le champ
    `password`, aucun compte, aucun email, aucune trace de création — pour tout admin."""
    for admin in (company_admin, admin_a):
        r = _create(_client(admin), _payload("choisi@test.io", password=STRONG), django_capture_on_commit_callbacks)
        assert r.status_code == 400 and "password" in r.json()
    assert not User.objects.filter(email="choisi@test.io").exists()
    assert mailoutbox == []
    assert not AuditLog.objects.filter(changes__action="create_user").exists()


def test_blank_password_at_creation_is_treated_as_absent(company_admin, mailoutbox,
                                                        django_capture_on_commit_callbacks):
    """Un champ `password` vide n'est pas un mot de passe : création normale, compte sans mot
    de passe utilisable (jamais un mot de passe vide qui ouvrirait le compte)."""
    r = _create(_client(company_admin), _payload("vide@test.io", password=""), django_capture_on_commit_callbacks)
    assert r.status_code == 201, r.content
    user = User.objects.get(email="vide@test.io")
    assert user.has_usable_password() is False
    assert not user.check_password("")
    assert len(mailoutbox) == 1 and mailoutbox[0].to == ["vide@test.io"]


def test_write_serializer_create_never_sets_a_usable_password(sub_a):
    """Défense en profondeur : même si une vue laissait passer un mot de passe, le serializer
    d'écriture crée le compte SANS mot de passe utilisable."""
    ser = EmployeeWriteSerializer(data=_payload("serial@test.io", subsidiary=str(sub_a.pk), password=STRONG))
    assert ser.is_valid(), ser.errors
    user = ser.save()
    user.refresh_from_db()
    assert user.has_usable_password() is False
    assert not user.check_password(STRONG)


def test_with_sso_no_local_invitation_is_sent(settings, company_admin, mailoutbox,
                                             django_capture_on_commit_callbacks):
    """Avec le SSO (OIDC_ENABLED), Keycloak envoie l'activation : aucune invitation locale ne
    part, et le compte reste sans mot de passe local utilisable."""
    settings.OIDC_ENABLED = True
    r = _create(_client(company_admin), _payload("sso@test.io"), django_capture_on_commit_callbacks)
    assert r.status_code == 201, r.content
    assert mailoutbox == []
    assert User.objects.get(email="sso@test.io").has_usable_password() is False
    assert not AuditLog.objects.filter(changes__action="send_invitation").exists()


def test_email_failure_does_not_break_creation_and_is_logged(
        company_admin, monkeypatch, caplog, mailoutbox, django_capture_on_commit_callbacks):
    """Un incident SMTP n'annule pas la création (201, compte présent, sans mot de passe
    utilisable) : il est journalisé (WARNING) et aucune invitation n'est tracée comme envoyée ;
    l'admin la renvoie ensuite par l'action `invite`."""
    import django.core.mail

    def _smtp_down(*args, **kwargs):
        raise ConnectionRefusedError("SMTP indisponible")

    monkeypatch.setattr(django.core.mail, "send_mail", _smtp_down)
    with caplog.at_level(logging.WARNING, logger="apps.accounts.api_views"):
        r = _create(_client(company_admin), _payload("panne@test.io"), django_capture_on_commit_callbacks)
    assert r.status_code == 201, r.content
    user = User.objects.get(email="panne@test.io")
    assert user.has_usable_password() is False
    warnings = [rec for rec in caplog.records
                if rec.name == "apps.accounts.api_views" and rec.levelno == logging.WARNING]
    assert len(warnings) == 1 and "panne@test.io" in warnings[0].getMessage()
    assert warnings[0].exc_info is not None  # la cause est conservée pour le diagnostic
    assert mailoutbox == []
    assert not AuditLog.objects.filter(target_id=str(user.pk), changes__action="send_invitation").exists()

    monkeypatch.undo()
    r = _client(company_admin).post(f"/api/employees/{user.pk}/invite/")
    assert r.status_code == 200, r.content
    assert len(mailoutbox) == 1 and mailoutbox[0].to == ["panne@test.io"]


# --- 3. /api/auth/password-setup/ ------------------------------------------------------


def test_full_invitation_flow_then_login(company_admin, mailoutbox, django_capture_on_commit_callbacks):
    """Bout en bout, en anonyme : le lien reçu est reconnu (GET → valid + email, sans rien
    consommer), le titulaire y définit SON mot de passe, puis obtient un jeton avec."""
    r = _create(_client(company_admin), _payload("flux@test.io"), django_capture_on_commit_callbacks)
    assert r.status_code == 201
    uid, token = _uid_token(_links_in(mailoutbox[0].body)[0])

    anon = _client()
    for _ in range(2):  # GET est idempotent : il ne consomme pas le lien
        g = _setup_get(uid, token, anon)
        assert g.status_code == 200 and g.json() == {"valid": True, "email": "flux@test.io"}
    assert _login("flux@test.io", STRONG).status_code == 401

    p = _setup_post(uid, token, STRONG, anon)
    assert p.status_code == 200, p.content
    user = User.objects.get(email="flux@test.io")
    assert user.has_usable_password() and user.check_password(STRONG)
    tok = _login_with_otp("flux@test.io", STRONG)
    assert tok.status_code == 200 and tok.json().get("access")
    assert AuditLog.objects.filter(actor=user, target_id=str(user.pk),
                                   changes__action="password_from_invitation").count() == 1


def test_invitation_link_is_single_use():
    """Le même lien ne sert qu'une fois : second POST → 400 (le mot de passe choisi la première
    fois reste en place), GET → 400 valid=false."""
    user = _invitee()
    uid, token = _uid_token(invitation_link(user))
    assert _setup_post(uid, token, STRONG).status_code == 200
    again = _setup_post(uid, token, "Kx-Autre-Mdp-2026?")
    assert again.status_code == 400
    user.refresh_from_db()
    assert user.check_password(STRONG) and not user.check_password("Kx-Autre-Mdp-2026?")
    g = _setup_get(uid, token)
    assert g.status_code == 400 and g.json()["valid"] is False


def test_tampered_token_or_foreign_uid_is_refused():
    """Signature vérifiée : un jeton altéré, ou le jeton d'un compte présenté avec l'uid d'un
    autre, répond 400 et ne change aucun mot de passe."""
    alice = _invitee("alice@test.io")
    bob = _invitee("bob@test.io")
    uid_a, token_a = _uid_token(invitation_link(alice))
    uid_b, _ = _uid_token(invitation_link(bob))
    tampered = token_a[:-1] + ("0" if token_a[-1] != "0" else "1")
    pwd_a, pwd_b = alice.password, bob.password

    for uid, token in ((uid_a, tampered), (uid_b, token_a), (uid_a, ""), ("", token_a), ("@@@", token_a)):
        assert _setup_get(uid, token).status_code == 400
        assert _setup_post(uid, token, STRONG).status_code == 400
    alice.refresh_from_db()
    bob.refresh_from_db()
    assert (alice.password, bob.password) == (pwd_a, pwd_b)
    # Contrôle : le lien intact d'Alice fonctionne (les refus tenaient bien à l'altération).
    assert _setup_post(uid_a, token_a, STRONG).status_code == 200


def test_link_expires_after_invitation_timeout():
    """Durée de vie = INVITATION_TIMEOUT_HOURS (PASSWORD_RESET_TIMEOUT) : un lien émis juste
    avant l'échéance passe, un lien émis juste après est refusé (GET et POST, rien ne change)."""
    hours = settings.INVITATION_TIMEOUT_HOURS
    assert settings.PASSWORD_RESET_TIMEOUT == hours * 3600
    user = _invitee()
    uid = _uid_token(invitation_link(user))[0]

    expired = _aged_token(user, hours + 1)
    assert _setup_get(uid, expired).status_code == 400
    assert _setup_post(uid, expired, STRONG).status_code == 400
    user.refresh_from_db()
    assert user.has_usable_password() is False

    fresh = _aged_token(user, hours - 1)
    assert _setup_get(uid, fresh).json() == {"valid": True, "email": user.email}
    assert _setup_post(uid, fresh, STRONG).status_code == 200


def test_tiny_timeout_and_time_shift_expire_the_emailed_link(settings, monkeypatch):
    """Le délai suit le réglage : PASSWORD_RESET_TIMEOUT = 60 s et 2 minutes écoulées → le
    lien (émis maintenant) est refusé ; sans le décalage d'horloge, il passait."""
    settings.PASSWORD_RESET_TIMEOUT = 60
    user = _invitee()
    uid, token = _uid_token(invitation_link(user))
    assert _setup_get(uid, token).status_code == 200

    real_now = invitation_tokens._now()
    monkeypatch.setattr(invitation_tokens, "_now", lambda: real_now + timedelta(minutes=2))
    assert _setup_get(uid, token).status_code == 400
    assert _setup_post(uid, token, STRONG).status_code == 400
    user.refresh_from_db()
    assert user.has_usable_password() is False


def test_weak_password_is_refused_with_validator_messages_and_nothing_changes():
    """« 123 » est refusé par les validateurs Django (400, messages repris dans la réponse) ;
    le compte reste sans mot de passe utilisable et le lien reste valable."""
    user = _invitee()
    uid, token = _uid_token(invitation_link(user))
    with translation.override(settings.LANGUAGE_CODE):
        with pytest.raises(ValidationError) as exc:
            validate_password("123", user)
        expected = exc.value.messages
    assert len(expected) >= 2  # trop court ET entièrement numérique

    r = _setup_post(uid, token, "123")
    assert r.status_code == 400
    for message in expected:
        assert message in r.json()["detail"]
    user.refresh_from_db()
    assert user.has_usable_password() is False
    assert _setup_get(uid, token).json()["valid"] is True
    # Un mot de passe absent ou non textuel est refusé de même.
    for bad in ("", None, 123456789):
        assert _client().post("/api/auth/password-setup/", {"uid": uid, "token": token, "password": bad},
                              format="json").status_code == 400
    user.refresh_from_db()
    assert user.has_usable_password() is False


def test_inactive_account_link_is_refused():
    """Un compte désactivé ne s'active pas par un lien émis avant sa désactivation."""
    user = _invitee()
    uid, token = _uid_token(invitation_link(user))
    User.objects.filter(pk=user.pk).update(is_active=False)
    g = _setup_get(uid, token)
    assert g.status_code == 400 and g.json()["valid"] is False and "email" not in g.json()
    assert _setup_post(uid, token, STRONG).status_code == 400
    user.refresh_from_db()
    assert user.has_usable_password() is False


def test_endpoint_is_anonymous_and_reveals_no_email():
    """Route publique (aucune authentification requise), qui ne divulgue pas l'existence d'une
    adresse : seule la paire uid + jeton valide révèle l'email du titulaire ; une recherche par
    email obtient la même réponse, que l'adresse existe ou non, sans rien modifier."""
    assert PasswordSetupView.authentication_classes == []
    user = _invitee("existe@test.io")
    pwd = user.password
    uid, token = _uid_token(invitation_link(user))
    anon = _client()

    # Lien altéré du titulaire : refus, sans son email.
    bad = _setup_get(uid, token[:-2] + "zz", anon)
    assert bad.status_code == 400 and "existe@test.io" not in bad.content.decode()

    # Sonde par email : réponses identiques pour une adresse existante et inexistante.
    probes = [anon.get("/api/auth/password-setup/", {"email": email})
              for email in ("existe@test.io", "absent@test.io")]
    assert [p.status_code for p in probes] == [400, 400]
    assert probes[0].json() == probes[1].json()
    assert all("existe@test.io" not in p.content.decode() for p in probes)
    posts = [anon.post("/api/auth/password-setup/", {"email": email, "password": STRONG}, format="json")
             for email in ("existe@test.io", "absent@test.io")]
    assert [p.status_code for p in posts] == [400, 400]
    assert posts[0].json() == posts[1].json()
    user.refresh_from_db()
    assert user.password == pwd

    ok = _setup_get(uid, token, anon)
    assert ok.status_code == 200 and ok.json()["email"] == "existe@test.io"


def test_password_setup_is_throttled_with_its_own_scope(monkeypatch):
    """Anti-force brute : la vue déclare le scope `password_setup`, défini dans les réglages ;
    une fois le quota atteint, même un lien valide reçoit 429 et rien n'est modifié."""
    rates = settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]
    assert rates.get("password_setup")
    assert PasswordSetupView.throttle_scope == "password_setup"

    monkeypatch.setattr(ScopedRateThrottle, "THROTTLE_RATES",
                        {**ScopedRateThrottle.THROTTLE_RATES, "password_setup": "3/hour"})
    user = _invitee()
    uid, token = _uid_token(invitation_link(user))
    anon = _client()
    for _ in range(3):
        assert _setup_get(uid, "1-faux", anon).status_code == 400
    assert _setup_get(uid, token, anon).status_code == 429
    assert _setup_post(uid, token, STRONG, anon).status_code == 429
    user.refresh_from_db()
    assert user.has_usable_password() is False


# --- 4. Action /invite/ ---------------------------------------------------------------


def test_admin_invite_sends_to_target_only_and_returns_no_link(company_admin, sub_a, mailoutbox):
    """(Ré)invitation par un admin : 200, un email à la seule adresse du titulaire, lien valide
    pour lui ; la réponse ne porte ni lien ni jeton."""
    target = _invitee("cible@test.io", subsidiary=sub_a)
    r = _client(company_admin).post(f"/api/employees/{target.pk}/invite/")
    assert r.status_code == 200, r.content
    assert len(mailoutbox) == 1
    mail = mailoutbox[0]
    assert mail.recipients() == ["cible@test.io"]
    uid, token = _uid_token(_links_in(mail.body)[0])
    assert resolve(uid, token) == target
    body = r.content.decode()
    assert token not in body and "setup-password" not in body and uid not in body
    assert set(r.json()) == {"detail"}


def test_subsidiary_admin_invites_own_subsidiary_only(admin_a, sub_a, sub_b, mailoutbox):
    """Un admin de filiale invite les comptes de SA filiale ; celui d'une filiale sœur est
    hors de son périmètre (403/404) et ne reçoit rien."""
    own = _invitee("propre@test.io", subsidiary=sub_a)
    foreign = _invitee("soeur@test.io", subsidiary=sub_b)
    c = _client(admin_a)
    assert c.post(f"/api/employees/{foreign.pk}/invite/").status_code in (403, 404)
    assert mailoutbox == []
    assert c.post(f"/api/employees/{own.pk}/invite/").status_code == 200
    assert [m.recipients() for m in mailoutbox] == [["propre@test.io"]]


def test_auditor_and_requester_cannot_invite(auditor, requester_a, sub_a, mailoutbox):
    """L'auditeur (lecture seule, même au périmètre entreprise) et le demandeur n'invitent
    personne : 403, aucun email."""
    target = _invitee("cible@test.io", subsidiary=sub_a)
    assert _client(auditor).post(f"/api/employees/{target.pk}/invite/").status_code == 403
    assert _client(requester_a).post(f"/api/employees/{target.pk}/invite/").status_code == 403
    assert mailoutbox == []
    assert not AuditLog.objects.filter(changes__action="send_invitation").exists()


def test_inactive_account_cannot_be_invited(company_admin, sub_a, mailoutbox):
    """Inviter un compte désactivé est refusé (400) : pas de lien pour un compte bloqué."""
    target = _invitee("bloque@test.io", subsidiary=sub_a)
    User.objects.filter(pk=target.pk).update(is_active=False)
    r = _client(company_admin).post(f"/api/employees/{target.pk}/invite/")
    assert r.status_code == 400
    assert mailoutbox == []


# --- 5. Anti-escalade -------------------------------------------------------------------


def test_password_ceiling_still_holds_for_stronger_account(company_admin, group_finance, mailoutbox):
    """L'admin entreprise n'a pas `pay_expense` : il ne choisit, ne réinitialise ni n'impose
    (PATCH) le mot de passe du financier groupe (403, mot de passe intact, aucun mot de passe
    temporaire renvoyé). La seule voie est l'invitation, qui part au financier lui-même."""
    c = _client(company_admin)
    before = group_finance.password
    r = c.post(f"/api/employees/{group_finance.pk}/set-password/", {"password": STRONG}, format="json")
    assert r.status_code == 403
    r = c.post(f"/api/employees/{group_finance.pk}/reset-password/")
    assert r.status_code == 403 and "temporary_password" not in r.content.decode()
    r = c.patch(f"/api/employees/{group_finance.pk}/", {"password": STRONG}, format="json")
    assert r.status_code == 403
    group_finance.refresh_from_db()
    assert group_finance.password == before

    r = c.post(f"/api/employees/{group_finance.pk}/invite/")
    assert r.status_code == 200
    assert [m.recipients() for m in mailoutbox] == [["fin@test.io"]]
    assert company_admin.email not in mailoutbox[0].recipients()


def test_password_ceiling_control_weaker_account_is_allowed(company_admin, requester_a):
    """Contrôle du plafond : pour un compte qui n'excède pas ses droits, l'admin entreprise
    peut toujours définir le mot de passe (le refus précédent tient bien au plafond)."""
    r = _client(company_admin).post(f"/api/employees/{requester_a.pk}/set-password/", {"password": STRONG},
                                    format="json")
    assert r.status_code == 200
    requester_a.refresh_from_db()
    assert requester_a.check_password(STRONG)


def test_self_promotion_is_refused(admin_a, company_admin, sub_a):
    """Personne ne modifie ses propres droits : un admin de filiale ne se fait pas admin
    entreprise ni ne se retire sa filiale ; un admin entreprise ne se fait pas super admin."""
    r = _client(admin_a).patch(f"/api/employees/{admin_a.pk}/", {"role": RoleChoices.COMPANY_ADMIN},
                               format="json")
    assert r.status_code == 403
    r = _client(admin_a).patch(f"/api/employees/{admin_a.pk}/", {"subsidiary": None}, format="json")
    assert r.status_code == 403
    r = _client(company_admin).patch(f"/api/employees/{company_admin.pk}/", {"role": RoleChoices.SUPER_ADMIN},
                                     format="json")
    assert r.status_code == 403
    # Se faire Finance, c'est s'octroyer la validation (filiale) ou le paiement (groupe) : la
    # séparation saisie / validation / paiement tomberait. Seule la garde « ses propres droits »
    # l'empêche (le rôle Finance est attribuable à autrui).
    r = _client(admin_a).patch(f"/api/employees/{admin_a.pk}/", {"role": RoleChoices.FINANCE}, format="json")
    assert r.status_code == 403
    r = _client(company_admin).patch(f"/api/employees/{company_admin.pk}/", {"role": RoleChoices.FINANCE},
                                     format="json")
    assert r.status_code == 403
    admin_a.refresh_from_db()
    company_admin.refresh_from_db()
    assert (admin_a.role, admin_a.subsidiary_id) == (RoleChoices.SUBSIDIARY_ADMIN, sub_a.pk)
    assert company_admin.role == RoleChoices.COMPANY_ADMIN
    # Contrôle : un champ sans incidence sur les droits reste modifiable par soi-même.
    r = _client(admin_a).patch(f"/api/employees/{admin_a.pk}/", {"phone": "+225 0101010101"}, format="json")
    assert r.status_code == 200, r.content


def test_creating_a_stronger_role_is_still_refused(admin_a, company_admin, mailoutbox,
                                                   django_capture_on_commit_callbacks):
    """L'invitation n'ouvre aucune porte de rôle : l'admin de filiale ne crée pas d'admin
    entreprise, l'admin entreprise ne crée pas de super admin (403, rien créé, aucun email)."""
    r = _create(_client(admin_a), _payload("grand@test.io", RoleChoices.COMPANY_ADMIN),
                django_capture_on_commit_callbacks)
    assert r.status_code == 403
    r = _create(_client(company_admin), _payload("super@test.io", RoleChoices.SUPER_ADMIN),
                django_capture_on_commit_callbacks)
    assert r.status_code == 403
    assert not User.objects.filter(email__in=["grand@test.io", "super@test.io"]).exists()
    assert mailoutbox == []


# --- 6. seed_demo -----------------------------------------------------------------------

_DEMO_EMAILS = [
    "super@kaydan.test", "admin@kaydan.test", "admin.abj@kaydan.test", "admin.dkr@kaydan.test",
    "flotte.abj@kaydan.test", "resp.abj@kaydan.test", "employe.abj@kaydan.test", "employe.dkr@kaydan.test",
    "chauffeur.abj@kaydan.test", "finance@kaydan.test", "audit@kaydan.test",
]


def _seed():
    out = io.StringIO()
    call_command("seed_demo", stdout=out)
    return out.getvalue()


def test_seed_demo_without_demo_password_creates_unusable_accounts_and_prints_links(monkeypatch):
    """Sans DEMO_PASSWORD : chaque compte de démo naît sans mot de passe utilisable (ni l'ancien
    défaut ni aucun autre ne l'ouvre) et la commande affiche SON lien d'invitation, valide pour
    lui. Relancée avec DEMO_PASSWORD, elle ne donne pas après coup de mot de passe aux comptes
    existants."""
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    output = _seed()
    users = {u.email: u for u in User.objects.filter(email__in=_DEMO_EMAILS)}
    assert sorted(users) == sorted(_DEMO_EMAILS)
    for email, user in users.items():
        assert user.has_usable_password() is False, email
        assert not user.check_password(FORMER_DEFAULT)
        line = next(ln for ln in output.splitlines() if ln.strip().startswith(f"{email} :"))
        links = _links_in(line)
        assert len(links) == 1, line
        assert resolve(*_uid_token(links[0])) == user
    assert _login("admin@kaydan.test", FORMER_DEFAULT).status_code == 401

    monkeypatch.setenv("DEMO_PASSWORD", STRONG)
    rerun = _seed()
    assert "setup-password" not in rerun
    for user in User.objects.filter(email__in=_DEMO_EMAILS):
        assert user.has_usable_password() is False


def test_seed_demo_with_demo_password_uses_operator_choice(monkeypatch):
    """Avec DEMO_PASSWORD : les comptes de démo reçoivent CE mot de passe (choisi par
    l'opérateur, jamais codé en dur) et aucun lien d'invitation n'est affiché."""
    monkeypatch.setenv("DEMO_PASSWORD", STRONG)
    output = _seed()
    users = list(User.objects.filter(email__in=_DEMO_EMAILS))
    assert len(users) == len(_DEMO_EMAILS)
    for user in users:
        assert user.check_password(STRONG), user.email
        assert not user.check_password(FORMER_DEFAULT)
    assert "setup-password" not in output
    assert _login_with_otp("admin@kaydan.test", STRONG).status_code == 200


# --- 7. Migration de révocation ---------------------------------------------------------


def _revoke_module():
    return importlib.import_module("apps.accounts.migrations.0005_revoke_default_password")


def test_revoke_migration_neutralises_former_default_only():
    """La migration rend inutilisable l'ancien mot de passe par défaut là où il est actif, et ne
    touche ni un compte au mot de passe propre, ni un compte déjà sans mot de passe."""
    legacy = _user("ancien@test.io", RoleChoices.REQUESTER, password=FORMER_DEFAULT)
    other = _user("propre@test.io", RoleChoices.REQUESTER, password=STRONG)
    unusable = _invitee("sans@test.io")
    assert legacy.check_password(FORMER_DEFAULT)
    hashes = {"other": other.password, "unusable": unusable.password}

    _revoke_module().revoke(django_apps, None)

    legacy.refresh_from_db()
    other.refresh_from_db()
    unusable.refresh_from_db()
    assert legacy.has_usable_password() is False
    assert not legacy.check_password(FORMER_DEFAULT)
    assert _login("ancien@test.io", FORMER_DEFAULT).status_code == 401
    assert other.password == hashes["other"] and other.check_password(STRONG)
    assert unusable.password == hashes["unusable"]


def test_revoke_migration_is_wired_and_applied():
    """La révocation est une vraie opération de migration (RunPython → revoke), postérieure à
    0004, et elle est appliquée à la base."""
    from django.db import migrations

    module = _revoke_module()
    ops = module.Migration.operations
    assert any(isinstance(op, migrations.RunPython) and op.code is module.revoke for op in ops)
    assert ("accounts", "0004_user_keycloak_id_user_keycloak_sync_error_and_more") in module.Migration.dependencies
    assert MigrationRecorder.Migration.objects.filter(app="accounts", name="0005_revoke_default_password").exists()
