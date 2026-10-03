"""Aperçus des emails K-Express : chaque gabarit et chaque variante, en .html et .txt.

    python manage.py render_email_previews --out /chemin/vers/dossier

Données FACTICES uniquement (aucune lecture de la base, aucun envoi) : caractères spéciaux,
balises à échapper, prénom absent, lien absent, montant à masquer. Le logo, embarqué par
Content-ID dans un vrai email (`cid:kx-logo`), est copié à côté des aperçus pour qu'un
navigateur l'affiche. `index.html` liste tous les aperçus.
"""
from __future__ import annotations

import html
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand

HOSTILE = "<script>alert('kx')</script>"


def _previews():
    from apps.core.emails import catalog, frontend_base
    from apps.notifications.services import email_kind
    from apps.notifications.visibility import AMOUNT, MASK

    reservation = ("Réservation n° 7F3A9C21\n"
                   "Demandeur : Aïcha Koné & Fils\n"
                   "Filiale : Kaydan Express — Abidjan\n"
                   "Départ : Plateau, tour « Ivoire » — le 03/10/2026 08:30\n"
                   "Destination : Aéroport FHB — retour estimé le 03/10/2026 12:00\n"
                   "Statut actuel : Validée\n"
                   "Prochaine action attendue : affectation d’un véhicule par le gestionnaire")
    amount_message = ("Plein déclaré pour le véhicule 1234-AB-01\n"
                      "Montant : 45 000 XOF\n"
                      "Station : Total Riviera 3")

    def notif(name, ntype, severity, title, message, link, **extra):
        return name, catalog.notification(
            subject=f"[Kaydan Express] {title}", title=title, message=message, link=link,
            kind=email_kind(ntype, severity), type_label=extra.pop("type_label", ""), **extra)

    yield "otp_connexion", catalog.login_code(
        code="482913", minutes=10, first_name="Aïcha",
        device_label=f"Chrome sur macOS (Abidjan) & {HOSTILE}")
    yield "otp_connexion_sans_prenom_sans_appareil", catalog.login_code(code="007120", minutes=10)
    yield "otp_activation", catalog.activation_code(code="90417256", minutes=10, first_name="Françoise")
    yield "otp_activation_sans_prenom", catalog.activation_code(code="31008842", minutes=15)
    yield "compte_deja_actif", catalog.already_active(first_name="Jean-Loïc")
    yield "compte_deja_actif_sans_prenom", catalog.already_active()
    # Lien factice sur le frontend configuré : `catalog.invitation` refuse tout autre hôte.
    link = f"{frontend_base()}/auth/setup-password?uid=MTIz&token=cxyz-0123456789abcdef"
    yield "invitation_creation", catalog.invitation(link=link, hours=72, first_name="Ève")
    yield "invitation_nouveau_mot_de_passe", catalog.invitation(link=link, hours=72, first_name="Ibrahim",
                                                                renewal=True)
    yield "invitation_sans_prenom", catalog.invitation(link=link, hours=24)

    yield notif("notification_information_reservation_soumise", "reservation_submitted", "info",
                "Nouvelle demande de réservation — Aéroport FHB", reservation, "/reservations/7f3a9c21",
                first_name="Kouadio", type_label="Demande soumise")
    yield notif("notification_confirmation_chauffeur_affecte", "driver_assigned", "info",
                "Vous êtes affecté à une course — Aéroport FHB",
                "Course n° 7F3A9C21\nDate : 03/10/2026 à 08:30\nPoint de départ : Plateau\n"
                "Destination : Aéroport FHB\nPassagers : 3\nVéhicule affecté : 1234-AB-01",
                "/map", first_name="Serge", type_label="Chauffeur affecté")
    yield notif("notification_avertissement_annulation", "reservation_cancelled", "info",
                "Réservation annulée — Aéroport FHB",
                reservation.replace("Validée", "Annulée") + f"\nMotif : réunion reportée {HOSTILE}",
                "/reservations/7f3a9c21", first_name="Aïcha", type_label="Demande annulée")
    yield notif("notification_action_requise_restitution", "return_expected", "warning",
                "Restitution attendue aujourd’hui — 1234-AB-01",
                "Véhicule : 1234-AB-01 (Toyota Hilux)\nRetour prévu : 02/10/2026 18:00\n"
                "Merci de restituer le véhicule et ses clés au parc de la filiale.",
                "/trips", first_name="Serge", type_label="Retour attendu")
    yield notif("notification_action_requise_carplan", "carplan", "info",
                "Car Plan : signature de votre attribution",
                "Votre attribution Car Plan est prête.\nVéhicule : Peugeot 3008 — 5678-CD-01\n"
                "Étape : signature électronique du bénéficiaire", "/car-plan?assignment=42",
                first_name="Ève", type_label="Car Plan")
    yield notif("notification_critique_immobilisation", "vehicle_immobilized", "critical",
                "Véhicule immobilisé — 1234-AB-01",
                "Panne moteur déclarée par le chauffeur.\nLieu : Yopougon, carrefour Siporex\n"
                "Maintenance : diagnostic demandé au garage partenaire", "/maintenance",
                first_name="Kouadio", type_label="Véhicule immobilisé")
    yield notif("notification_confirmation_maintenance_terminee", "maintenance_done", "info",
                "Maintenance terminée — 1234-AB-01",
                "Le véhicule est de nouveau disponible.\nIntervention : vidange & freins", "/vehicles",
                first_name="", type_label="Maintenance terminée")
    yield notif("notification_sans_lien_sans_message", "other", "info", "Mise à jour de K-Express",
                "", "", first_name="Aïcha")
    yield notif("notification_montant_masque_demandeur", "fuel_declared", "info",
                "Plein de carburant déclaré", AMOUNT.sub(MASK, amount_message), "/fuel",
                first_name="Aïcha", type_label="Plein de carburant déclaré")
    yield "notification_modele_personnalise", catalog.notification(
        subject="KX — Rappel avant départ", title="Rappel avant départ", message="Départ dans 1 h",
        link="/trips/1", kind="info", text_body="Bonjour Aïcha Koné,\nDépart dans 1 h.\nLien : /trips/1",
        type_label="Rappel avant départ")


class Command(BaseCommand):
    help = "Écrit un aperçu (.html et .txt) de chaque email K-Express dans un dossier."

    def add_arguments(self, parser):
        parser.add_argument("--out", required=True, help="Dossier de sortie (créé au besoin).")

    def handle(self, *args, **options):
        from apps.core.emails.rendering import LOGO_CID, LOGO_FILE

        out = Path(options["out"]).expanduser()
        out.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(LOGO_FILE, out / LOGO_FILE.name)
        rows = []
        for name, rendered in _previews():
            (out / f"{name}.txt").write_text(f"Objet : {rendered.subject}\n\n{rendered.text}", encoding="utf-8")
            (out / f"{name}.html").write_text(
                (rendered.html or "").replace(f"cid:{LOGO_CID}", LOGO_FILE.name), encoding="utf-8")
            rows.append(f'<li><a href="{name}.html">{html.escape(name)}</a> &middot; '
                        f'<a href="{name}.txt">texte</a> &mdash; {html.escape(rendered.subject)}</li>')
        (out / "index.html").write_text(
            '<!DOCTYPE html><html lang="fr"><head><meta charset="utf-8"><title>Aperçus des emails K-Express'
            '</title></head><body style="font-family:system-ui,sans-serif;max-width:760px;margin:32px auto;'
            'padding:0 16px;line-height:1.7"><h1>Aperçus des emails K-Express</h1><ul>'
            + "".join(rows) + "</ul></body></html>", encoding="utf-8")
        self.stdout.write(self.style.SUCCESS(f"{len(rows)} aperçus écrits dans {out}"))
