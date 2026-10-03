"""Plans d'entretien : plus haut niveau notifié par échéance (anti-rebond des alertes), un seul
plan ACTIF par véhicule et par opération, événement « intervention annulée ».

Les doublons actifs éventuels (plans créés avant la contrainte) sont désactivés avant de la
poser : le plus récemment mis à jour reste actif, aucun plan n'est supprimé."""
from django.db import migrations, models


def deactivate_duplicates(apps, schema_editor):
    Schedule = apps.get_model("maintenance", "MaintenanceSchedule")
    seen = set()
    for plan in Schedule.objects.filter(is_active=True).order_by("vehicle_id", "maintenance_type_id",
                                                                 "-updated_at", "-created_at"):
        key = (plan.vehicle_id, plan.maintenance_type_id)
        if key in seen:
            Schedule.objects.filter(pk=plan.pk).update(is_active=False)
        else:
            seen.add(key)


class Migration(migrations.Migration):

    dependencies = [
        ("maintenance", "0004_predictive_plans"),
    ]

    operations = [
        migrations.AddField(
            model_name="maintenanceschedule",
            name="alert_peak",
            field=models.CharField(blank=True, max_length=10, verbose_name="plus haut niveau notifié pour l'échéance"),
        ),
        migrations.AlterField(
            model_name="maintenanceplanevent",
            name="kind",
            field=models.CharField(choices=[("alert", "Alerte"), ("relaunch", "Relance"),
                                            ("pace", "Rythme kilométrique en hausse"), ("cleared", "Alerte levée"),
                                            ("done", "Entretien réalisé"), ("undone", "Intervention annulée"),
                                            ("meter", "Remplacement de compteur"),
                                            ("config", "Paramètres modifiés")],
                                   max_length=10, verbose_name="événement"),
        ),
        migrations.RunPython(deactivate_duplicates, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="maintenanceschedule",
            constraint=models.UniqueConstraint(condition=models.Q(("is_active", True)),
                                               fields=("vehicle", "maintenance_type"),
                                               name="uniq_active_maintenance_plan"),
        ),
    ]
