"""Suivi kilométrique : horodatage réel des relevés, corrections tracées, remplacement de
compteur, fréquence des relevés (politique / attribution), relevés immuables EN BASE."""
import django.db.models.deletion
import django.db.models.manager
from django.db import migrations, models


def backfill_recorded_at(apps, schema_editor):
    """Relevés existants : l'instant de saisie s'il tombe le jour du relevé, sinon midi ce jour-là."""
    from datetime import datetime, time

    from django.utils import timezone

    MileageReading = apps.get_model("carplan", "MileageReading")
    for reading in MileageReading.objects.filter(recorded_at__isnull=True).iterator():
        if timezone.localdate(reading.created_at) == reading.reading_date:
            value = reading.created_at
        else:
            value = timezone.make_aware(datetime.combine(reading.reading_date, time(12, 0)))
        MileageReading.objects.filter(pk=reading.pk).update(recorded_at=value)


LOCK = r"""
CREATE TRIGGER carplan_reading_locked BEFORE UPDATE OR DELETE ON carplan_mileagereading
  FOR EACH ROW EXECUTE FUNCTION carplan_forbid();
"""
UNLOCK = "DROP TRIGGER IF EXISTS carplan_reading_locked ON carplan_mileagereading;"


class Migration(migrations.Migration):

    dependencies = [
        ('carplan', '0003_history_locks'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='mileagereading',
            options={'base_manager_name': 'all_objects', 'ordering': ['recorded_at', 'id'], 'verbose_name': 'relevé kilométrique', 'verbose_name_plural': 'relevés kilométriques'},
        ),
        migrations.AlterModelManagers(
            name='mileagereading',
            managers=[
                ('objects', django.db.models.manager.Manager()),
                ('all_objects', django.db.models.manager.Manager()),
            ],
        ),
        migrations.AddField(
            model_name='carplanassignment',
            name='reading_frequency_days',
            field=models.PositiveSmallIntegerField(blank=True, choices=[(5, 'Tous les 5 jours'), (7, 'Toutes les semaines')], null=True, verbose_name='fréquence des relevés (jours)'),
        ),
        migrations.AddField(
            model_name='carplanpolicyversion',
            name='reading_frequency_days',
            field=models.PositiveSmallIntegerField(choices=[(5, 'Tous les 5 jours'), (7, 'Toutes les semaines')], default=7, verbose_name='fréquence des relevés (jours)'),
        ),
        migrations.AddField(
            model_name='mileagereading',
            name='anomaly',
            field=models.CharField(blank=True, max_length=255, verbose_name='relevé atypique'),
        ),
        migrations.AddField(
            model_name='mileagereading',
            name='reason',
            field=models.CharField(blank=True, max_length=500, verbose_name='motif (correction, remplacement de compteur)'),
        ),
        migrations.AddField(
            model_name='mileagereading',
            name='corrects',
            field=models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='correction', to='carplan.mileagereading', verbose_name='corrige le relevé'),
        ),
        migrations.AddField(
            model_name='mileagereading',
            name='meter_offset',
            field=models.IntegerField(default=0, verbose_name='décalage de compteur (km)'),
        ),
        migrations.AddField(
            model_name='mileagereading',
            name='previous_odometer',
            field=models.PositiveIntegerField(blank=True, null=True, verbose_name='ancien compteur (km)'),
        ),
        migrations.AddField(
            model_name='mileagereading',
            name='recorded_at',
            field=models.DateTimeField(db_index=True, null=True, verbose_name='relevé le'),
        ),
        migrations.RunPython(backfill_recorded_at, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='mileagereading',
            name='recorded_at',
            field=models.DateTimeField(db_index=True, verbose_name='relevé le'),
        ),
        migrations.RunSQL(LOCK, UNLOCK),
        migrations.AlterField(
            model_name='mileagereading',
            name='source',
            field=models.CharField(choices=[('declaration', 'Déclaration du bénéficiaire'), ('handover', 'Remise'), ('return', 'Restitution'), ('manager', 'Relevé gestionnaire'), ('meter_replacement', 'Remplacement de compteur')], default='declaration', max_length=20, verbose_name='origine'),
        ),
    ]
