"""Sème les zones opérationnelles de dispatching (§3) pour une ou toutes les filiales.

Idempotent : la clé est `(filiale, code)`. Relancer la commande met à jour la géométrie des
zones existantes sans créer de doublon, et sans toucher aux zones ajoutées à la main.

Les zones sont rattachées à une FILIALE (le modèle `GeofenceZone` est multi-tenant) : chaque
filiale peut donc ajuster son propre découpage, ce que le besoin prévoit explicitement
(« zones métier personnalisées »).

    python manage.py seed_operational_zones                 # toutes les filiales actives
    python manage.py seed_operational_zones --subsidiary PLT
    python manage.py seed_operational_zones --dry-run
"""
from django.core.management.base import BaseCommand, CommandError

from apps.core.enums import GeofenceType, ZoneCategory
from apps.organizations.models import Subsidiary
from apps.tracking.models import GeofenceZone

# Découpage opérationnel du Grand Abidjan et alentours.
# Zones circulaires (centre + rayon) : suffisant et robuste pour du dispatching, et bien plus
# maintenable qu'un polygone administratif exact. Une zone peut être re-dessinée ensuite via
# l'UI (le polygone prend alors le pas sur le rayon lors de la résolution).
ZONES = [
    # (code, nom, lat, lng, rayon m, catégorie)
    ("plateau", "Plateau", 5.3200, -4.0200, 2500, ZoneCategory.ADMINISTRATIVE),
    ("cocody", "Cocody", 5.3600, -3.9900, 5000, ZoneCategory.ADMINISTRATIVE),
    ("marcory", "Marcory", 5.3000, -3.9900, 3500, ZoneCategory.ADMINISTRATIVE),
    ("treichville", "Treichville", 5.2930, -4.0100, 2500, ZoneCategory.ADMINISTRATIVE),
    ("yopougon", "Yopougon", 5.3450, -4.0900, 6000, ZoneCategory.ADMINISTRATIVE),
    ("aeroport", "Aéroport Félix-Houphouët-Boigny", 5.2610, -3.9260, 3000, ZoneCategory.BUSINESS),
    ("zone-industrielle", "Zone industrielle (Vridi / Yopougon)", 5.2700, -4.0060, 4000, ZoneCategory.BUSINESS),
    ("grand-abidjan", "Grand Abidjan", 5.3400, -4.0200, 25000, ZoneCategory.ADMINISTRATIVE),
    ("agneby-tiassa", "Agnéby-Tiassa", 5.8800, -4.2200, 40000, ZoneCategory.ADMINISTRATIVE),
]


class Command(BaseCommand):
    help = "Sème les zones opérationnelles de dispatching (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--subsidiary", dest="code",
            help="Code de la filiale ciblée (par défaut : toutes les filiales actives).",
        )
        parser.add_argument(
            "--dry-run", action="store_true",
            help="N'écrit rien ; affiche ce qui serait créé ou mis à jour.",
        )

    def handle(self, *args, **options):
        subsidiaries = Subsidiary.objects.filter(is_active=True)
        if options.get("code"):
            subsidiaries = subsidiaries.filter(code=options["code"])
            if not subsidiaries.exists():
                raise CommandError(f"Aucune filiale active avec le code « {options['code']} ».")
        if not subsidiaries.exists():
            raise CommandError("Aucune filiale active : rien à semer.")

        dry = options.get("dry_run")
        created = updated = 0
        for sub in subsidiaries:
            for code, name, lat, lng, radius, category in ZONES:
                existing = GeofenceZone.objects.filter(subsidiary=sub, code=code).first()
                if dry:
                    self.stdout.write(
                        f"  [{sub.code}] {'≡ ' if existing else '+ '}{name}"
                        + (" (existe)" if existing else "")
                    )
                    continue
                zone = existing or GeofenceZone(subsidiary=sub, code=code)
                zone.name = name
                zone.zone_type = GeofenceType.OPERATIONAL
                zone.category = category
                zone.center_lat = lat
                zone.center_lng = lng
                zone.radius_m = radius
                zone.is_active = True
                zone.save()
                if existing:
                    updated += 1
                else:
                    created += 1

        if dry:
            self.stdout.write(self.style.WARNING("Simulation : aucune écriture."))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Zones opérationnelles : {created} créée(s), {updated} mise(s) à jour "
                f"sur {subsidiaries.count()} filiale(s)."
            ))
