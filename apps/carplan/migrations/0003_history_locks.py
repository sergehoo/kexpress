"""Verrous d'historique Car Plan EN BASE : les signaux `pre_save` ne voient ni `QuerySet.update()`
ni le SQL direct. Erreur levée en SQLSTATE 23000 (→ `IntegrityError` côté Django)."""
from django.db import migrations

FORWARD = r"""
CREATE OR REPLACE FUNCTION carplan_forbid() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'carplan : % interdit sur % (historique verrouillé)', TG_OP, TG_TABLE_NAME
    USING ERRCODE = '23000';
END $$ LANGUAGE plpgsql;

CREATE TRIGGER carplan_event_locked BEFORE UPDATE OR DELETE ON carplan_carplanevent
  FOR EACH ROW EXECUTE FUNCTION carplan_forbid();
CREATE TRIGGER carplan_assignment_undeletable BEFORE DELETE ON carplan_carplanassignment
  FOR EACH ROW EXECUTE FUNCTION carplan_forbid();
CREATE TRIGGER carplan_inspection_undeletable BEFORE DELETE ON carplan_carplaninspection
  FOR EACH ROW EXECUTE FUNCTION carplan_forbid();

CREATE OR REPLACE FUNCTION carplan_inspection_locked() RETURNS trigger AS $$
BEGIN
  IF OLD.employee_signed_at IS NOT NULL AND OLD.manager_signed_at IS NOT NULL THEN
    IF (to_jsonb(NEW) - 'pv_pdf' - 'updated_at') IS DISTINCT FROM (to_jsonb(OLD) - 'pv_pdf' - 'updated_at')
       OR (OLD.pv_pdf <> '' AND NEW.pv_pdf IS DISTINCT FROM OLD.pv_pdf) THEN
      RAISE EXCEPTION 'carplan : état des lieux validé, non modifiable' USING ERRCODE = '23000';
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER carplan_inspection_locked BEFORE UPDATE ON carplan_carplaninspection
  FOR EACH ROW EXECUTE FUNCTION carplan_inspection_locked();

CREATE OR REPLACE FUNCTION carplan_photo_locked() RETURNS trigger AS $$
DECLARE parent uuid;
BEGIN
  parent := CASE WHEN TG_OP = 'DELETE' THEN OLD.inspection_id ELSE NEW.inspection_id END;
  IF EXISTS (SELECT 1 FROM carplan_carplaninspection WHERE id = parent AND employee_signed_at IS NOT NULL)
     OR (TG_OP = 'UPDATE' AND EXISTS (SELECT 1 FROM carplan_carplaninspection
                                      WHERE id = OLD.inspection_id AND employee_signed_at IS NOT NULL)) THEN
    RAISE EXCEPTION 'carplan : photos d''un état des lieux validé, non modifiables' USING ERRCODE = '23000';
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER carplan_photo_locked BEFORE INSERT OR UPDATE OR DELETE ON carplan_carplaninspectionphoto
  FOR EACH ROW EXECUTE FUNCTION carplan_photo_locked();

CREATE OR REPLACE FUNCTION carplan_version_locked() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'carplan : version publiée, non supprimable' USING ERRCODE = '23000';
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.status <> 'draft' THEN
    IF (to_jsonb(NEW) - 'status' - 'updated_at') IS DISTINCT FROM (to_jsonb(OLD) - 'status' - 'updated_at')
       OR NOT (NEW.status = OLD.status OR (OLD.status = 'published' AND NEW.status = 'retired')) THEN
      RAISE EXCEPTION 'carplan : version publiée, non modifiable' USING ERRCODE = '23000';
    END IF;
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER carplan_version_locked BEFORE UPDATE OR DELETE ON carplan_carplanpolicyversion
  FOR EACH ROW EXECUTE FUNCTION carplan_version_locked();

CREATE OR REPLACE FUNCTION carplan_version_categories_locked() RETURNS trigger AS $$
DECLARE version uuid;
BEGIN
  version := CASE WHEN TG_OP = 'DELETE' THEN OLD.carplanpolicyversion_id ELSE NEW.carplanpolicyversion_id END;
  IF EXISTS (SELECT 1 FROM carplan_carplanpolicyversion WHERE id = version AND status <> 'draft')
     OR (TG_OP = 'UPDATE' AND EXISTS (SELECT 1 FROM carplan_carplanpolicyversion
                                      WHERE id = OLD.carplanpolicyversion_id AND status <> 'draft')) THEN
    RAISE EXCEPTION 'carplan : catégories d''une version publiée, non modifiables' USING ERRCODE = '23000';
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER carplan_version_categories_locked
  BEFORE INSERT OR UPDATE OR DELETE ON carplan_carplanpolicyversion_eligible_categories
  FOR EACH ROW EXECUTE FUNCTION carplan_version_categories_locked();

CREATE OR REPLACE FUNCTION carplan_mode_decision_final() RETURNS trigger AS $$
BEGIN
  IF OLD.status <> 'requested' THEN
    RAISE EXCEPTION 'carplan : décision de changement de mode définitive' USING ERRCODE = '23000';
  END IF;
  RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER carplan_mode_decision_final BEFORE UPDATE OR DELETE ON carplan_vehicleusagechange
  FOR EACH ROW EXECUTE FUNCTION carplan_mode_decision_final();
"""

BACKWARD = r"""
DROP TRIGGER IF EXISTS carplan_mode_decision_final ON carplan_vehicleusagechange;
DROP TRIGGER IF EXISTS carplan_version_categories_locked ON carplan_carplanpolicyversion_eligible_categories;
DROP TRIGGER IF EXISTS carplan_version_locked ON carplan_carplanpolicyversion;
DROP TRIGGER IF EXISTS carplan_photo_locked ON carplan_carplaninspectionphoto;
DROP TRIGGER IF EXISTS carplan_inspection_locked ON carplan_carplaninspection;
DROP TRIGGER IF EXISTS carplan_inspection_undeletable ON carplan_carplaninspection;
DROP TRIGGER IF EXISTS carplan_assignment_undeletable ON carplan_carplanassignment;
DROP TRIGGER IF EXISTS carplan_event_locked ON carplan_carplanevent;
DROP FUNCTION IF EXISTS carplan_mode_decision_final();
DROP FUNCTION IF EXISTS carplan_version_categories_locked();
DROP FUNCTION IF EXISTS carplan_version_locked();
DROP FUNCTION IF EXISTS carplan_photo_locked();
DROP FUNCTION IF EXISTS carplan_inspection_locked();
DROP FUNCTION IF EXISTS carplan_forbid();
"""


class Migration(migrations.Migration):
    dependencies = [("carplan", "0002_gps_access_grants")]

    operations = [migrations.RunSQL(FORWARD, BACKWARD)]
