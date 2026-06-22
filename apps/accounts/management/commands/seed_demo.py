"""Crée un jeu de données de démonstration (idempotent).

1 entreprise, 2 filiales, un utilisateur par rôle, quelques véhicules et chauffeurs.
Mot de passe commun à tous les comptes de démo : « demo1234 ».
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.accounts.models import User
from apps.core.enums import (
    FuelType,
    RoleChoices,
    VehicleStatus,
    VehicleType,
)
from apps.drivers.models import Driver
from apps.organizations.models import Company, Department, Subsidiary
from apps.vehicles.models import Vehicle

DEMO_PASSWORD = "demo1234"


class Command(BaseCommand):
    help = "Peuple la base avec des données de démonstration."

    @transaction.atomic
    def handle(self, *args, **options):
        company, _ = Company.objects.get_or_create(
            name="Kaydan Groupe",
            defaults={"legal_name": "Kaydan Groupe SA", "is_active": True},
        )

        sub_a, _ = Subsidiary.objects.get_or_create(
            code="ABJ", defaults={"company": company, "name": "Kaydan Abidjan", "city": "Abidjan"}
        )
        sub_b, _ = Subsidiary.objects.get_or_create(
            code="DKR", defaults={"company": company, "name": "Kaydan Dakar", "city": "Dakar"}
        )

        dept_a, _ = Department.objects.get_or_create(subsidiary=sub_a, name="Direction Générale")
        Department.objects.get_or_create(subsidiary=sub_b, name="Logistique")

        # --- Utilisateurs : un par rôle ---
        users_spec = [
            ("super@kaydan.test", RoleChoices.SUPER_ADMIN, None, True, True),
            ("admin@kaydan.test", RoleChoices.COMPANY_ADMIN, None, False, True),
            ("admin.abj@kaydan.test", RoleChoices.SUBSIDIARY_ADMIN, sub_a, False, True),
            ("admin.dkr@kaydan.test", RoleChoices.SUBSIDIARY_ADMIN, sub_b, False, True),
            ("flotte.abj@kaydan.test", RoleChoices.FLEET_MANAGER, sub_a, False, False),
            ("resp.abj@kaydan.test", RoleChoices.DEPARTMENT_MANAGER, sub_a, False, False),
            ("employe.abj@kaydan.test", RoleChoices.REQUESTER, sub_a, False, False),
            ("employe.dkr@kaydan.test", RoleChoices.REQUESTER, sub_b, False, False),
            ("chauffeur.abj@kaydan.test", RoleChoices.DRIVER, sub_a, False, False),
            ("finance@kaydan.test", RoleChoices.FINANCE, None, False, False),
            ("audit@kaydan.test", RoleChoices.AUDITOR, None, False, False),
        ]
        created_count = 0
        for email, role, sub, is_super, is_staff in users_spec:
            user, created = User.objects.get_or_create(
                email=email,
                defaults={
                    "role": role,
                    "subsidiary": sub,
                    "first_name": email.split("@")[0].split(".")[0].capitalize(),
                    "last_name": role.label,
                    "is_superuser": is_super,
                    "is_staff": is_staff,
                },
            )
            if created:
                user.set_password(DEMO_PASSWORD)
                user.save()
                created_count += 1

        # Assigne le service au demandeur d'Abidjan
        User.objects.filter(email="employe.abj@kaydan.test").update(department=dept_a)

        # --- Véhicules ---
        vehicles_spec = [
            (sub_a, "AA-123-BC", "Toyota", "Hilux", VehicleType.PICKUP, 5, FuelType.DIESEL, VehicleStatus.AVAILABLE),
            (sub_a, "AA-456-DE", "Renault", "Clio", VehicleType.SEDAN, 5, FuelType.GASOLINE, VehicleStatus.AVAILABLE),
            (sub_a, "AA-789-FG", "Toyota", "Coaster", VehicleType.BUS, 22, FuelType.DIESEL, VehicleStatus.MAINTENANCE),
            (sub_b, "DK-111-AA", "Hyundai", "Tucson", VehicleType.SUV, 5, FuelType.DIESEL, VehicleStatus.AVAILABLE),
            (sub_b, "DK-222-BB", "Peugeot", "Partner", VehicleType.VAN, 3, FuelType.DIESEL, VehicleStatus.RESERVED),
        ]
        for sub, reg, brand, model, vtype, cap, fuel, status in vehicles_spec:
            Vehicle.objects.get_or_create(
                registration=reg,
                defaults={
                    "subsidiary": sub, "brand": brand, "model": model,
                    "vehicle_type": vtype, "capacity": cap, "fuel_type": fuel,
                    "status": status, "mileage": 25000,
                },
            )

        # --- Chauffeurs ---
        # Le compte chauffeur d'Abidjan possède déjà un profil Driver auto-créé par le
        # signal (rôle = chauffeur) : on l'enrichit plutôt que d'en créer un doublon.
        drv_abj = Driver.objects.filter(user__email="chauffeur.abj@kaydan.test").first()
        if drv_abj:
            drv_abj.first_name = "Koffi"
            drv_abj.last_name = "Yao"
            drv_abj.license_number = drv_abj.license_number or "ABJ-DR-001"
            drv_abj.license_category = drv_abj.license_category or "B"
            drv_abj.phone = drv_abj.phone or "+225 0700000000"
            drv_abj.save()
        # Dakar n'a pas de compte chauffeur dédié : profil créé manuellement.
        Driver.objects.get_or_create(
            subsidiary=sub_b, license_number="DKR-DR-001",
            defaults={"first_name": "Moussa", "last_name": "Diop", "license_category": "D",
                      "phone": "+221 770000000", "is_available": True},
        )

        self._seed_operations(sub_a, sub_b)

        self.stdout.write(self.style.SUCCESS(
            f"Démo prête : 1 entreprise, 2 filiales, {len(users_spec)} comptes "
            f"({created_count} créés), {len(vehicles_spec)} véhicules, 2 chauffeurs."
        ))
        self.stdout.write(self.style.WARNING(f"Mot de passe de tous les comptes de démo : {DEMO_PASSWORD}"))

    def _seed_operations(self, sub_a, sub_b):
        """Positions GPS, maintenance et carburant de démonstration (idempotent)."""
        from datetime import date, timedelta
        from decimal import Decimal

        from django.utils import timezone

        from apps.expenses.models import FuelLog
        from apps.maintenance.models import MaintenanceRecord, MaintenanceType
        from apps.tracking.models import VehicleLocation

        # Centres approximatifs des filiales (Abidjan / Dakar)
        centers = {sub_a.id: (5.345, -4.024), sub_b.id: (14.716, -17.467)}

        now = timezone.now()
        vehicles = list(Vehicle.objects.all())
        for i, v in enumerate(vehicles):
            base = centers.get(v.subsidiary_id, (5.345, -4.024))
            # Décalage déterministe pour étaler les marqueurs
            lat = Decimal(str(round(base[0] + ((i % 5) - 2) * 0.012, 6)))
            lng = Decimal(str(round(base[1] + ((i % 3) - 1) * 0.012, 6)))
            VehicleLocation.objects.update_or_create(
                vehicle=v,
                defaults={
                    "latitude": lat, "longitude": lng,
                    "speed_kmh": Decimal("0"),  # position de stationnement : aucune vitesse fictive
                    "heading": Decimal(str((i * 45) % 360)),
                    "recorded_at": now - timedelta(minutes=i * 3),
                },
            )

        # Types de maintenance
        vidange, _ = MaintenanceType.objects.get_or_create(
            name="Vidange", defaults={"interval_km": 10000, "interval_days": 180}
        )
        pneus, _ = MaintenanceType.objects.get_or_create(
            name="Pneumatiques", defaults={"interval_km": 40000}
        )

        # Course en cours (démo temps réel) : véhicule en course + chauffeur assigné
        from apps.reservations.models import Reservation
        from apps.trips.models import Trip

        veh = Vehicle.objects.filter(subsidiary=sub_a, registration="AA-123-BC").first()
        drv = Driver.objects.filter(subsidiary=sub_a).first()
        emp = User.objects.filter(email="employe.abj@kaydan.test").first()
        if veh and drv and emp:
            res, _ = Reservation.objects.get_or_create(
                subsidiary=sub_a, requester=emp, purpose="Course live (démo)",
                defaults={
                    "created_by": emp, "trip_date": date.today(),
                    "departure_time": now - timedelta(hours=1),
                    "estimated_return": now + timedelta(hours=2),
                    "destination": "Cocody, Abidjan", "passengers": 2,
                    "needs_driver": True, "status": "in_progress",
                    "vehicle": veh, "driver": drv,
                },
            )
            trip, _ = Trip.objects.get_or_create(
                reservation=res,
                leg="outbound",
                defaults={
                    "subsidiary": sub_a, "requester": emp, "vehicle": veh, "driver": drv,
                    "destination": "Cocody, Abidjan", "status": "in_progress",
                    "actual_departure": now - timedelta(hours=1),
                    "start_mileage": veh.mileage, "created_by": emp,
                },
            )
            if veh.status != "on_trip":
                veh.status = "on_trip"
                veh.save(update_fields=["status"])

            # Itinéraire prévu de la course (origine → étape → destination)
            from apps.tracking.models import TripRoute, TripWaypoint

            route, created_route = TripRoute.objects.get_or_create(
                trip=trip,
                defaults={
                    "origin_label": "Siège — Plateau", "origin_lat": Decimal("5.320000"), "origin_lng": Decimal("-4.022000"),
                    "destination_label": "Cocody", "destination_lat": Decimal("5.359000"), "destination_lng": Decimal("-3.987000"),
                    "planned_distance_km": Decimal("8.5"), "planned_duration_min": 25,
                },
            )
            if created_route:
                TripWaypoint.objects.create(
                    route=route, order=1, label="Pont HKB",
                    latitude=Decimal("5.336000"), longitude=Decimal("-4.005000"),
                )

        # Quelques interventions + pleins sur les 2 premiers véhicules de chaque filiale
        for v in vehicles[:4]:
            MaintenanceRecord.objects.get_or_create(
                vehicle=v, maintenance_type=vidange, scheduled_date=date.today() - timedelta(days=10),
                defaults={
                    "subsidiary": v.subsidiary, "status": "completed",
                    "performed_date": date.today() - timedelta(days=9),
                    "mileage": v.mileage, "cost": Decimal("85000"), "provider": "Garage Central",
                },
            )
            for k in range(2):
                FuelLog.objects.get_or_create(
                    vehicle=v, date=date.today() - timedelta(days=k * 7 + 2),
                    defaults={
                        "subsidiary": v.subsidiary,
                        "liters": Decimal("45.00"),
                        "amount": Decimal("38250"),
                        "price_per_liter": Decimal("850"),
                        "mileage": v.mileage - k * 300,
                    },
                )

        # Documents véhicule, échéances de maintenance, dépenses, permis (→ alertes & dépenses)
        from apps.expenses.models import Expense
        from apps.maintenance.models import MaintenanceSchedule
        from apps.vehicles.models import VehicleDocument

        for i, v in enumerate(vehicles):
            VehicleDocument.objects.get_or_create(
                vehicle=v, doc_type="insurance",
                defaults={"number": f"ASS-{v.registration}", "issue_date": date.today() - timedelta(days=300),
                          "expiry_date": date.today() + timedelta(days=12 + i * 5)},
            )
            VehicleDocument.objects.get_or_create(
                vehicle=v, doc_type="technical_inspection",
                defaults={"number": f"VT-{v.registration}", "issue_date": date.today() - timedelta(days=150),
                          "expiry_date": date.today() + timedelta(days=20 + i * 9)},
            )
        # Une échéance de maintenance proche sur le 1er véhicule
        if vehicles:
            MaintenanceSchedule.objects.get_or_create(
                vehicle=vehicles[0], maintenance_type=vidange,
                defaults={"due_date": date.today() + timedelta(days=8), "due_mileage": vehicles[0].mileage + 1000, "is_active": True},
            )
        # Permis chauffeur proche expiration
        d2 = Driver.objects.filter(subsidiary=sub_b).first()
        if d2 and not d2.license_expiry:
            d2.license_expiry = date.today() + timedelta(days=18)
            d2.save(update_fields=["license_expiry"])
        # Dépenses diverses
        for i, v in enumerate(vehicles[:3]):
            Expense.objects.get_or_create(
                vehicle=v, label="Péage autoroute", category="toll",
                defaults={"subsidiary": v.subsidiary, "amount": Decimal("2500"), "date": date.today() - timedelta(days=i + 1)},
            )
            Expense.objects.get_or_create(
                vehicle=v, label="Lavage", category="other",
                defaults={"subsidiary": v.subsidiary, "amount": Decimal("5000"), "date": date.today() - timedelta(days=i + 3)},
            )

        # Zone de géofencing (mission Plateau) — le trajet vers Cocody en sort.
        from apps.tracking.models import GeofenceZone

        GeofenceZone.objects.get_or_create(
            subsidiary=sub_a, name="Zone mission Plateau",
            defaults={
                "zone_type": "mission",
                "polygon": [[5.300, -4.045], [5.300, -4.000], [5.340, -4.000], [5.340, -4.045]],
                "is_active": True,
            },
        )

        # Incidents de démonstration (course + chauffeur)
        from apps.drivers.models import DriverIncident
        from apps.trips.models import Trip, TripIncident

        a_trip = Trip.objects.filter(subsidiary=sub_a).first()
        if a_trip:
            TripIncident.objects.get_or_create(
                trip=a_trip, description="Crevaison pneu avant droit sur le boulevard.",
                defaults={"occurred_at": now - timedelta(days=2), "severity": "moderate"},
            )
        a_drv = Driver.objects.filter(subsidiary=sub_a).first()
        if a_drv:
            DriverIncident.objects.get_or_create(
                driver=a_drv, description="Léger retard de prise de service.",
                defaults={"occurred_at": now - timedelta(days=5), "severity": "minor"},
            )
