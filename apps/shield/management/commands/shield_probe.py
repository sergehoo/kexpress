"""Sonde Kaydan Shield avant la première synchronisation complète.

`python manage.py shield_probe [--sample N] [--apply-sample]`

1. Authentification réelle du compte de service (jetons en mémoire, jamais affichés).
2. Lecture SEULE : filiales, départements, premiers employés — comptes, statuts, couverture des
   champs attendus, doublons d'email dans l'échantillon. Aucune donnée personnelle affichée
   (emails et noms masqués).
3. `--apply-sample` : enregistre les filiales, départements et les N premiers employés (sans
   réconciliation : aucune absence déduite ; départs différés à la prochaine synchronisation
   complète, derrière ses garde-fous). Rien n'est fusionné par email : un compte existant non lié
   devient un conflit à rapprocher dans « Synchronisation RH ».
"""
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

EXPECTED_FIELDS = ("id", "uuid", "email", "first_name", "last_name", "status", "company", "department",
                   "updated_at")


def _mask(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "—"


class Command(BaseCommand):
    help = "Teste la connexion au compte de service Shield et inspecte un échantillon (lecture seule par défaut)."

    def add_arguments(self, parser):
        parser.add_argument("--sample", type=int, default=20, help="Employés lus (1 à 200, défaut 20).")
        parser.add_argument("--apply-sample", action="store_true",
                            help="Enregistrer filiales, départements et l'échantillon d'employés.")

    def handle(self, *args, sample, apply_sample, **options):
        from django.conf import settings

        from apps.shield.client import EMPLOYEES_PATH, ShieldAuthError, ShieldClient, ShieldError

        sample = max(1, min(200, sample))
        if not settings.SHIELD_BASE_URL.startswith("https://") and not settings.DEBUG:
            raise CommandError("SHIELD_BASE_URL doit être en https:// hors développement.")
        client = ShieldClient(page_size=sample)
        out = self.stdout
        try:
            client.login()
        except ShieldAuthError as exc:
            raise CommandError(f"Authentification Shield refusée : {exc}") from None
        except ShieldError as exc:
            raise CommandError(f"Shield injoignable : {exc}") from None
        out.write(self.style.SUCCESS(f"Authentification réussie sur {client.base_url} (compte de service)."))
        try:
            companies = client.list_companies()
            departments = client.list_departments()
            page = client.fetch_page(EMPLOYEES_PATH, client.employee_params(ordering="id"))
        except ShieldError as exc:
            raise CommandError(f"Lecture Shield impossible : {exc}") from None
        employees = [e for e in page.results if isinstance(e, dict)]
        out.write(f"Filiales Shield : {len(companies)} ; départements : {len(departments)} ; "
                  f"employés : {page.count if page.count is not None else 'total non communiqué'} "
                  f"(échantillon lu : {len(employees)}).")
        self._report_companies(companies)
        missing = Counter(f for e in employees for f in EXPECTED_FIELDS if e.get(f) in (None, ""))
        if missing:
            out.write(self.style.WARNING("Champs absents dans l'échantillon : "
                                         + ", ".join(f"{k} ({v})" for k, v in missing.most_common())))
        out.write("Statuts : " + ", ".join(f"{k or '∅'} = {v}" for k, v in
                                           Counter(str(e.get("status") or "") for e in employees).items()))
        emails = Counter((e.get("email") or "").strip().lower() for e in employees if e.get("email"))
        duplicates = [m for m, n in emails.items() if n > 1]
        if duplicates:
            out.write(self.style.WARNING(f"Emails en double dans l'échantillon : "
                                         f"{', '.join(_mask(m) for m in duplicates)}"))
        for e in employees[:5]:
            out.write(f"  · id {e.get('id')} — {_mask(e.get('email') or '')} — statut {e.get('status') or '∅'}")
        if apply_sample:
            self._apply(companies, departments, employees)
        else:
            out.write("Lecture seule : rien n'a été enregistré (--apply-sample pour enregistrer l'échantillon).")

    def _report_companies(self, companies):
        from apps.shield.models import ShieldCompany

        mapped = dict(ShieldCompany.objects.filter(subsidiary__isnull=False).values_list("shield_id", "subsidiary__name"))
        for c in companies[:30]:
            if not isinstance(c, dict):
                continue
            target = mapped.get(c.get("id"))
            self.stdout.write(f"  · filiale Shield {c.get('id')} « {c.get('name') or c.get('code') or '?'} » → "
                              f"{target or 'NON RAPPROCHÉE (à confirmer dans Synchronisation RH)'}")

    def _apply(self, companies, departments, employees):
        from apps.shield import sync
        from apps.shield.models import ShieldCompany

        now = timezone.now()
        counters = {}
        with transaction.atomic():
            n_companies = sum(1 for c in companies if isinstance(c, dict) and sync._tenant_ok(c)
                              and sync._upsert_company(c, now))
            known = dict(ShieldCompany.objects.values_list("shield_id", "pk"))
            n_departments = sum(1 for d in departments if isinstance(d, dict) and sync._upsert_department(d, known, now))
            refs = sync._Refs()
            for item in employees:
                if sync._tenant_ok(item):
                    sync.upsert_employee(item, None, counters, now=now, refs=refs)
        self.stdout.write(self.style.SUCCESS(
            f"Échantillon enregistré : {n_companies} filiale(s), {n_departments} département(s), "
            f"employés {counters}. Rapprochez les filiales puis lancez « shield_sync --mode full »."))
