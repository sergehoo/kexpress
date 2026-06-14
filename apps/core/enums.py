"""Énumérations centralisées (TextChoices) pour toute la plateforme."""
from django.db import models


class RoleChoices(models.TextChoices):
    SUPER_ADMIN = "super_admin", "Super administrateur"
    COMPANY_ADMIN = "company_admin", "Administrateur entreprise"
    SUBSIDIARY_ADMIN = "subsidiary_admin", "Administrateur filiale"
    FLEET_MANAGER = "fleet_manager", "Gestionnaire de flotte"
    DEPARTMENT_MANAGER = "department_manager", "Responsable de service"
    REQUESTER = "requester", "Employé demandeur"
    DRIVER = "driver", "Chauffeur"
    FINANCE = "finance", "Comptabilité / Finance"
    AUDITOR = "auditor", "Auditeur"


#: Rôles qui voient toutes les filiales (périmètre entreprise).
COMPANY_SCOPE_ROLES = frozenset(
    {RoleChoices.SUPER_ADMIN, RoleChoices.COMPANY_ADMIN, RoleChoices.AUDITOR}
)


class VehicleStatus(models.TextChoices):
    AVAILABLE = "available", "Disponible"
    RESERVED = "reserved", "Réservé"
    ON_TRIP = "on_trip", "En course"
    MAINTENANCE = "maintenance", "En maintenance"
    OUT_OF_SERVICE = "out_of_service", "Hors service"
    UNAVAILABLE = "unavailable", "Indisponible"


class VehicleType(models.TextChoices):
    SEDAN = "sedan", "Berline"
    SUV = "suv", "SUV / 4x4"
    PICKUP = "pickup", "Pick-up"
    VAN = "van", "Utilitaire / Van"
    BUS = "bus", "Bus / Minibus"
    TRUCK = "truck", "Camion"
    MOTORCYCLE = "motorcycle", "Moto"
    OTHER = "other", "Autre"


class FuelType(models.TextChoices):
    GASOLINE = "gasoline", "Essence"
    DIESEL = "diesel", "Diesel"
    HYBRID = "hybrid", "Hybride"
    ELECTRIC = "electric", "Électrique"
    LPG = "lpg", "GPL"
    OTHER = "other", "Autre"


class VehicleDocumentType(models.TextChoices):
    INSURANCE = "insurance", "Assurance"
    TECHNICAL_INSPECTION = "technical_inspection", "Visite technique"
    REGISTRATION = "registration", "Carte grise"
    OTHER = "other", "Autre"


class DriverDocumentType(models.TextChoices):
    LICENSE = "license", "Permis de conduire"
    ID_CARD = "id_card", "Pièce d'identité"
    CONTRACT = "contract", "Contrat"
    MEDICAL = "medical", "Certificat médical"
    OTHER = "other", "Autre"


class ReservationStatus(models.TextChoices):
    DRAFT = "draft", "Brouillon"
    SUBMITTED = "submitted", "Soumise"
    PENDING_MANAGER = "pending_manager", "En attente validation responsable"
    PENDING_FLEET = "pending_fleet", "En attente validation flotte"
    APPROVED = "approved", "Validée"
    REJECTED = "rejected", "Refusée"
    CANCELLED = "cancelled", "Annulée"
    VEHICLE_ASSIGNED = "vehicle_assigned", "Véhicule affecté"
    DRIVER_ASSIGNED = "driver_assigned", "Chauffeur affecté"
    IN_PROGRESS = "in_progress", "En cours"
    COMPLETED = "completed", "Terminée"
    CLOSED = "closed", "Clôturée"


class PriorityLevel(models.TextChoices):
    LOW = "low", "Basse"
    NORMAL = "normal", "Normale"
    HIGH = "high", "Haute"
    URGENT = "urgent", "Urgente"


class ValidationLevel(models.TextChoices):
    MANAGER = "manager", "Responsable hiérarchique"
    FLEET = "fleet", "Gestionnaire de flotte"


class ValidationDecision(models.TextChoices):
    PENDING = "pending", "En attente"
    APPROVED = "approved", "Approuvée"
    REJECTED = "rejected", "Refusée"


class TripStatus(models.TextChoices):
    SCHEDULED = "scheduled", "Planifiée"
    DEPARTED = "departed", "Départ effectué"
    IN_PROGRESS = "in_progress", "En cours"
    RETURNED = "returned", "Retour effectué"
    CLOSED = "closed", "Clôturée"


class IncidentSeverity(models.TextChoices):
    MINOR = "minor", "Mineur"
    MODERATE = "moderate", "Modéré"
    MAJOR = "major", "Majeur"
    CRITICAL = "critical", "Critique"


class MaintenanceStatus(models.TextChoices):
    PLANNED = "planned", "Planifiée"
    IN_PROGRESS = "in_progress", "En cours"
    COMPLETED = "completed", "Terminée"
    CANCELLED = "cancelled", "Annulée"


class MaintenanceNature(models.TextChoices):
    PREVENTIVE = "preventive", "Maintenance préventive"
    CORRECTIVE = "corrective", "Maintenance corrective"
    URGENT = "urgent", "Réparation urgente"
    PERIODIC = "periodic", "Entretien périodique"
    OTHER = "other", "Autre"


class ExpenseCategory(models.TextChoices):
    FUEL = "fuel", "Carburant"
    MAINTENANCE = "maintenance", "Maintenance"
    INSURANCE = "insurance", "Assurance"
    TOLL = "toll", "Péage"
    FINE = "fine", "Amende"
    UNEXPECTED = "unexpected", "Imprévu"
    OTHER = "other", "Autre"


class AlertSeverity(models.TextChoices):
    INFO = "info", "Information"
    WARNING = "warning", "Avertissement"
    CRITICAL = "critical", "Critique"


class NotificationChannel(models.TextChoices):
    IN_APP = "in_app", "Notification interne"
    EMAIL = "email", "Email"
    SMS = "sms", "SMS"
    WHATSAPP = "whatsapp", "WhatsApp"
    PUSH = "push", "Push"


class NotificationType(models.TextChoices):
    RESERVATION_SUBMITTED = "reservation_submitted", "Demande soumise"
    RESERVATION_APPROVED = "reservation_approved", "Demande validée"
    RESERVATION_REJECTED = "reservation_rejected", "Demande refusée"
    VEHICLE_ASSIGNED = "vehicle_assigned", "Véhicule affecté"
    DRIVER_ASSIGNED = "driver_assigned", "Chauffeur affecté"
    DEPARTURE_REMINDER = "departure_reminder", "Rappel avant départ"
    RETURN_EXPECTED = "return_expected", "Retour attendu"
    RETURN_LATE = "return_late", "Retard de retour"
    INSURANCE_EXPIRING = "insurance_expiring", "Assurance proche expiration"
    INSPECTION_EXPIRING = "inspection_expiring", "Visite technique proche expiration"
    MAINTENANCE_DUE = "maintenance_due", "Maintenance à prévoir"
    TRIP_DEPARTED = "trip_departed", "Départ de course"
    TRIP_ARRIVED = "trip_arrived", "Arrivée destination"
    GEOFENCE_EXIT = "geofence_exit", "Sortie de zone"
    ROUTE_DEVIATION = "route_deviation", "Véhicule hors itinéraire"
    GPS_SIGNAL_LOST = "gps_signal_lost", "Perte de signal GPS"
    INCIDENT_REPORTED = "incident_reported", "Incident déclaré"
    REVISION_DUE = "revision_due", "Révision à prévoir"
    VEHICLE_NON_COMPLIANT = "vehicle_non_compliant", "Véhicule non conforme"
    RESERVATION_CREATED = "reservation_created", "Demande créée"
    RESERVATION_UPDATED = "reservation_updated", "Demande modifiée"
    RESERVATION_CANCELLED = "reservation_cancelled", "Demande annulée"
    TRIP_CLOSED = "trip_closed", "Course clôturée"
    EXPENSE_ADDED = "expense_added", "Dépense ajoutée"
    FUEL_DECLARED = "fuel_declared", "Plein de carburant déclaré"
    FUEL_ANOMALY = "fuel_anomaly", "Consommation carburant anormale"
    FUEL_PRICE_UPDATED = "fuel_price_updated", "Prix carburant mis à jour"
    FUEL_BUDGET_EXCEEDED = "fuel_budget_exceeded", "Budget carburant dépassé"
    MAINTENANCE_DECLARED = "maintenance_declared", "Panne / maintenance déclarée"
    MAINTENANCE_DONE = "maintenance_done", "Maintenance terminée"
    VEHICLE_IMMOBILIZED = "vehicle_immobilized", "Véhicule immobilisé"
    VEHICLE_BACK = "vehicle_back", "Véhicule remis en service"
    OTHER = "other", "Autre"


class GeofenceType(models.TextChoices):
    COMPANY = "company", "Zone entreprise"
    SUBSIDIARY = "subsidiary", "Zone filiale"
    MISSION = "mission", "Zone mission"
    FORBIDDEN = "forbidden", "Zone interdite"
    PARKING = "parking", "Zone de stationnement"


class TrackingSessionStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    PAUSED = "paused", "En pause"
    ENDED = "ended", "Terminée"


class DevicePlatform(models.TextChoices):
    ANDROID = "android", "Android"
    IOS = "ios", "iOS"
    WEB = "web", "Web"


class SyncStatus(models.TextChoices):
    PENDING = "pending", "En attente"
    SYNCED = "synced", "Synchronisé"
    CONFLICT = "conflict", "Conflit"
    FAILED = "failed", "Échec"


class AuditAction(models.TextChoices):
    CREATE = "create", "Création"
    UPDATE = "update", "Modification"
    DELETE = "delete", "Suppression"
    LOGIN = "login", "Connexion"
    LOGOUT = "logout", "Déconnexion"
    EXPORT = "export", "Export"
    ACCESS = "access", "Accès"
