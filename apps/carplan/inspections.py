"""États des lieux Car Plan (C3) : remise et restitution, validation par les parties, PV PDF,
comparaison automatique.

Modalités de validation : le gestionnaire réalise l'état des lieux ; le BÉNÉFICIAIRE le valide
depuis son espace, le GESTIONNAIRE le valide à son tour. Dès la double validation, le
procès-verbal PDF est émis (fichier servi par le seul mécanisme d'URL signée) et l'attribution
avance : remise → ACTIVE, restitution → RESTITUÉE. Un état des lieux validé est figé.
"""
from __future__ import annotations

import io
from xml.sax.saxutils import escape

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils import timezone

from apps.carplan import services
from apps.carplan.models import CarPlanAssignment, CarPlanInspection, CarPlanInspectionPhoto, MileageReading
from apps.carplan.services import CarPlanError

A = CarPlanAssignment
CONDITION_RANK = {"good": 0, "fair": 1, "poor": 2}
MAX_PHOTO_BYTES = 8 * 1024 * 1024
PHOTO_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")


def _clean_list(value, keys) -> list:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise CarPlanError("Liste attendue.")
    out = []
    for item in value[:100]:
        if not isinstance(item, dict):
            raise CarPlanError("Éléments de liste invalides.")
        out.append({k: item.get(k) for k in keys if k in item})
    return out


@services.writes
@transaction.atomic
def create_inspection(assignment, *, actor, kind, data) -> CarPlanInspection:
    assignment = CarPlanAssignment.objects.select_for_update().get(pk=assignment.pk)
    if kind == CarPlanInspection.HANDOVER and assignment.status != A.ALLOCATED:
        raise CarPlanError("L'état des lieux de remise se fait sur une attribution dont le véhicule est attribué.")
    if kind == CarPlanInspection.RETURN and assignment.status not in (A.ACTIVE, A.SUSPENDED, A.RETURNING):
        raise CarPlanError("L'état des lieux de restitution se fait sur une attribution en cours.")
    if kind not in (CarPlanInspection.HANDOVER, CarPlanInspection.RETURN):
        raise CarPlanError("Nature d'état des lieux inconnue.")
    if assignment.inspections.filter(kind=kind, vehicle=assignment.vehicle, manager_signed_at__isnull=True).exists():
        raise CarPlanError("Un état des lieux de cette nature est déjà en cours pour ce véhicule.")
    try:
        mileage = int(data.get("mileage"))
        level = int(data.get("energy_level_pct"))
    except (TypeError, ValueError):
        raise CarPlanError("Kilométrage et niveau d'énergie (en %) sont obligatoires.")
    if mileage < 0 or not 0 <= level <= 100:
        raise CarPlanError("Kilométrage positif et niveau d'énergie entre 0 et 100 %.")
    vehicle = assignment.vehicle
    if kind == CarPlanInspection.RETURN:
        handover = _handover_of(assignment)
        last = MileageReading.objects.filter(vehicle=vehicle).order_by("-reading_date", "-odometer").first()
        floor = max(filter(None, [handover.mileage if handover else None, last.odometer if last else None,
                                  assignment.start_mileage]), default=0)
        if mileage < floor:
            raise CarPlanError(f"Le kilométrage de restitution est inférieur au dernier relevé connu ({floor} km).")
    elif vehicle.mileage and mileage + 50 < vehicle.mileage:
        raise CarPlanError("Le kilométrage relevé est inférieur au compteur connu du véhicule.")
    conditions = {f: data.get(f) for f in ("exterior_condition", "interior_condition")}
    if any(v not in CONDITION_RANK for v in conditions.values()):
        raise CarPlanError("État extérieur et intérieur : bon, correct ou dégradé.")
    tyres = data.get("tyres") or {}
    if not isinstance(tyres, dict):
        raise CarPlanError("Pneumatiques : objet attendu.")
    inspection = CarPlanInspection.objects.create(
        assignment=assignment, vehicle=vehicle, kind=kind, performed_at=timezone.now(), mileage=mileage,
        energy_level_pct=level, exterior_notes=str(data.get("exterior_notes") or "")[:2000],
        interior_notes=str(data.get("interior_notes") or "")[:2000], tyres={str(k)[:40]: str(v)[:40] for k, v in
                                                                            list(tyres.items())[:10]},
        equipment=_clean_list(data.get("equipment"), ("item", "present", "note")),
        documents=_clean_list(data.get("documents"), ("item", "handed", "note")),
        anomalies=_clean_list(data.get("anomalies"), ("zone", "description", "severity")),
        observations=str(data.get("observations") or "")[:4000], performed_by=actor, created_by=actor,
        **conditions)
    services._event(assignment, f"inspection_{kind}", actor, inspection=inspection.pk, mileage=mileage)
    return inspection


def check_image(image) -> None:
    """Photographie réelle : extension ET contenu décodé par Pillow, taille bornée."""
    from PIL import Image, UnidentifiedImageError

    name = (getattr(image, "name", "") or "").lower()
    if not name.endswith(PHOTO_EXTENSIONS):
        raise CarPlanError("Photo attendue (JPEG, PNG, WebP).")
    if getattr(image, "size", 0) > MAX_PHOTO_BYTES:
        raise CarPlanError("Photo trop lourde (8 Mo au plus).")
    try:
        image.seek(0)
        with Image.open(image) as decoded:
            decoded.verify()
            kind = (decoded.format or "").upper()
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError):
        raise CarPlanError("Le fichier n'est pas une image valide.")
    finally:
        image.seek(0)
    if kind not in ("JPEG", "PNG", "WEBP", "MPO"):
        raise CarPlanError("Format d'image non accepté.")


@services.writes
def add_photo(inspection, *, actor, image, zone="", caption="") -> CarPlanInspectionPhoto:
    inspection = CarPlanInspection.objects.get(pk=inspection.pk)
    if inspection.employee_signed_at or inspection.is_signed:
        raise CarPlanError("État des lieux déjà validé par le bénéficiaire : il ne reçoit plus de photo.")
    check_image(image)
    if inspection.photos.count() >= 40:
        raise CarPlanError("40 photos au plus par état des lieux.")
    return CarPlanInspectionPhoto.objects.create(inspection=inspection, image=image, zone=(zone or "")[:80],
                                                 caption=(caption or "")[:255], uploaded_by=actor)


@services.writes
@transaction.atomic
def sign(inspection, *, actor, as_beneficiary: bool) -> CarPlanInspection:
    """Validation par une partie. La seconde validation émet le PV et fait avancer l'attribution."""
    inspection = CarPlanInspection.objects.select_for_update().get(pk=inspection.pk)
    assignment = inspection.assignment
    now = timezone.now()
    if as_beneficiary:
        if actor.pk != assignment.beneficiary_id:
            raise CarPlanError("Seul le bénéficiaire valide l'état des lieux en son nom.")
        if inspection.employee_signed_at:
            raise CarPlanError("Déjà validé par le bénéficiaire.")
        inspection.employee_signed_at = now
    else:
        if actor.pk == assignment.beneficiary_id:
            raise CarPlanError("Le bénéficiaire ne valide pas en tant que gestionnaire.")
        if inspection.manager_signed_at:
            raise CarPlanError("Déjà validé par le gestionnaire.")
        if not inspection.employee_signed_at:
            raise CarPlanError("Le bénéficiaire valide d'abord l'état des lieux.")
        inspection.manager_signed_at, inspection.manager_signed_by = now, actor
    inspection.save(update_fields=["employee_signed_at", "manager_signed_at", "manager_signed_by", "updated_at"])
    services._event(assignment, "inspection_signed", actor, inspection=inspection.pk,
                    party="beneficiary" if as_beneficiary else "manager")
    if inspection.is_signed:
        _finalise(inspection, actor)
    return inspection


def _finalise(inspection, actor):
    MileageReading.objects.create(assignment=inspection.assignment, vehicle=inspection.vehicle,
                                  reading_date=timezone.localtime(inspection.performed_at).date(),
                                  odometer=inspection.mileage, source=inspection.kind, declared_by=actor)
    from apps.carplan.operations import raise_vehicle_mileage

    raise_vehicle_mileage(inspection.vehicle, inspection.mileage)
    inspection.pv_pdf.save(f"pv-{inspection.assignment.reference}-{inspection.kind}-{inspection.pk}.pdf",
                           ContentFile(render_pv(inspection)), save=False)
    CarPlanInspection.objects.filter(pk=inspection.pk).update(pv_pdf=inspection.pv_pdf.name)
    if inspection.kind == CarPlanInspection.HANDOVER:
        services.activate(inspection.assignment, actor=actor, handover=inspection)
    else:
        services.complete_return(inspection.assignment, actor=actor, inspection=inspection)


def _handover_of(assignment, vehicle=None):
    return (assignment.inspections.filter(kind=CarPlanInspection.HANDOVER, vehicle=vehicle or assignment.vehicle,
                                          manager_signed_at__isnull=False).order_by("-performed_at").first())


def compare(handover, ret) -> dict:
    """Écarts entre l'état des lieux de remise et celui de restitution."""
    if handover is None or ret is None:
        return {"available": False}
    gaps = []

    names = dict(CarPlanInspection.CONDITION)

    def downgrade(field, label):
        if CONDITION_RANK.get(getattr(ret, field), 0) > CONDITION_RANK.get(getattr(handover, field), 0):
            gaps.append({"kind": "condition", "label": f"{label} dégradé : {names.get(getattr(handover, field))} → "
                                                       f"{names.get(getattr(ret, field))}"})

    downgrade("exterior_condition", "État extérieur")
    downgrade("interior_condition", "État intérieur")
    known = {(str(a.get("zone", "")).lower(), str(a.get("description", "")).lower()) for a in handover.anomalies}
    for anomaly in ret.anomalies:
        if (str(anomaly.get("zone", "")).lower(), str(anomaly.get("description", "")).lower()) not in known:
            gaps.append({"kind": "new_anomaly", "label": f"Nouvelle anomalie — {anomaly.get('zone', '')} : "
                                                         f"{anomaly.get('description', '')}"})
    present_before = {e.get("item") for e in handover.equipment if e.get("present")}
    present_after = {e.get("item") for e in ret.equipment if e.get("present")}
    for item in sorted(present_before - present_after, key=str):
        gaps.append({"kind": "missing_equipment", "label": f"Équipement manquant : {item}"})
    handed_before = {d.get("item") for d in handover.documents if d.get("handed")}
    handed_after = {d.get("item") for d in ret.documents if d.get("handed")}
    for item in sorted(handed_before - handed_after, key=str):
        gaps.append({"kind": "missing_document", "label": f"Document non restitué : {item}"})
    for wheel, state in handover.tyres.items():
        if ret.tyres.get(wheel) and ret.tyres.get(wheel) != state:
            gaps.append({"kind": "tyre", "label": f"Pneumatique {wheel} : {state} → {ret.tyres.get(wheel)}"})
    return {"available": True, "km_driven": ret.mileage - handover.mileage,
            "energy_delta_pct": ret.energy_level_pct - handover.energy_level_pct, "gaps": gaps,
            "has_gaps": bool(gaps)}


def comparison_for(assignment) -> dict:
    ret = (assignment.inspections.filter(kind=CarPlanInspection.RETURN).order_by("-performed_at").first())
    return compare(_handover_of(assignment, ret.vehicle if ret else None), ret)


# --- Procès-verbal PDF --------------------------------------------------------------------


def render_pv(inspection) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    a = inspection.assignment
    v = inspection.vehicle
    styles = getSampleStyleSheet()
    navy = colors.HexColor("#1D3069")
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm,
                            bottomMargin=16 * mm, title=f"PV {a.reference}")
    title = "Procès-verbal de remise" if inspection.kind == "handover" else "Procès-verbal de restitution"
    story = [Paragraph(f"<font color='#1D3069'><b>K-Express — {title}</b></font>", styles["Title"]),
             Paragraph(f"Attribution {a.reference} · {a.get_assignment_type_display()}", styles["Normal"]),
             Spacer(1, 6 * mm)]

    def table(rows):
        t = Table(rows, colWidths=[55 * mm, 115 * mm])
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                               ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#EEF1F8")),
                               ("TEXTCOLOR", (0, 0), (0, -1), navy), ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("FONTSIZE", (0, 0), (-1, -1), 9)]))
        return t

    def p(text):
        # Texte saisi ÉCHAPPÉ : aucun balisage reportlab (police, image distante…) n'est interprété.
        return Paragraph(escape(str(text or "—")).replace("\n", "<br/>"), styles["BodyText"])

    condition = dict(CarPlanInspection.CONDITION)
    story.append(table([
        ["Bénéficiaire", p(a.beneficiary.get_full_name() or a.beneficiary.email)],
        ["Véhicule", p(f"{v.registration} — {v.brand} {v.model}")],
        ["Date", p(timezone.localtime(inspection.performed_at).strftime("%d/%m/%Y %H:%M"))],
        ["Kilométrage", p(f"{inspection.mileage} km")],
        ["Carburant / batterie", p(f"{inspection.energy_level_pct} %")],
        ["État extérieur", p(f"{condition[inspection.exterior_condition]} {inspection.exterior_notes}")],
        ["État intérieur", p(f"{condition[inspection.interior_condition]} {inspection.interior_notes}")],
        ["Pneumatiques", p(", ".join(f"{k} : {val}" for k, val in inspection.tyres.items()))],
        ["Équipements", p(", ".join(f"{e.get('item')} ({'présent' if e.get('present') else 'absent'})"
                                     for e in inspection.equipment))],
        ["Documents remis", p(", ".join(f"{d.get('item')} ({'oui' if d.get('handed') else 'non'})"
                                         for d in inspection.documents))],
        ["Anomalies", p("\n".join(f"{x.get('zone', '')} : {x.get('description', '')}" for x in inspection.anomalies))],
        ["Observations", p(inspection.observations)],
        ["Photographies", p(f"{inspection.photos.count()} photo(s) jointe(s) au dossier numérique")],
    ]))
    if inspection.kind == "return":
        diff = compare(_handover_of(a, v), inspection)
        if diff.get("available"):
            story += [Spacer(1, 5 * mm), Paragraph("<b>Comparaison avec la remise</b>", styles["Heading3"]),
                      table([["Kilomètres parcourus", p(f"{diff['km_driven']} km")],
                             ["Écart d'énergie", p(f"{diff['energy_delta_pct']} %")],
                             ["Écarts constatés", p("\n".join(g["label"] for g in diff["gaps"]) or "Aucun")]])]
    signed = lambda d: timezone.localtime(d).strftime("%d/%m/%Y %H:%M") if d else "—"  # noqa: E731
    story += [Spacer(1, 6 * mm), table([
        ["Validé par le bénéficiaire", p(signed(inspection.employee_signed_at))],
        ["Validé par le gestionnaire", p(f"{signed(inspection.manager_signed_at)} — "
                                         f"{inspection.manager_signed_by.get_full_name() if inspection.manager_signed_by_id else ''}")],
    ])]
    doc.build(story)
    return buffer.getvalue()
