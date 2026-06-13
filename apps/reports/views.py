"""Endpoint d'export de rapports (CSV / Excel / PDF), scopé et audité."""
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.audit import services as audit
from apps.core.enums import AuditAction
from apps.reports.datasets import REPORT_TYPES, build_dataset
from apps.reports.exporters import to_csv, to_pdf, to_xlsx

EXT = {"csv": "csv", "xlsx": "xlsx", "pdf": "pdf"}


class ReportExportView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        rtype = request.query_params.get("type", "fleet")
        # NB: paramètre « fmt » (pas « format ») pour ne pas déclencher la
        # négociation de contenu DRF (qui renverrait 404 sur un format inconnu).
        fmt = request.query_params.get("fmt", "csv")
        sub = request.query_params.get("subsidiary")

        if rtype not in REPORT_TYPES or fmt not in EXT:
            return HttpResponse("Type ou format de rapport invalide.", status=400)

        # Filtre périodique optionnel (?start=YYYY-MM-DD&end=YYYY-MM-DD)
        from datetime import date

        start = end = None
        try:
            if request.query_params.get("start") and request.query_params.get("end"):
                start = date.fromisoformat(request.query_params["start"])
                end = date.fromisoformat(request.query_params["end"])
        except ValueError:
            return HttpResponse("Dates invalides (format attendu : YYYY-MM-DD).", status=400)

        ds = build_dataset(request.user, rtype, sub, start=start, end=end)
        if ds is None:
            return HttpResponse("Rapport introuvable.", status=404)

        today = timezone.localdate().isoformat()
        period_label = (
            f"Période : {start.strftime('%d/%m/%Y')} → {end.strftime('%d/%m/%Y')}"
            if start else f"Généré le {timezone.localdate().strftime('%d/%m/%Y')}"
        )
        if fmt == "xlsx":
            content, ctype = to_xlsx(ds)
        elif fmt == "pdf":
            content, ctype = to_pdf(ds, subtitle=period_label)
        else:
            content, ctype = to_csv(ds)

        audit.record(
            request.user, AuditAction.EXPORT, None,
            changes={"report": rtype, "format": fmt, "rows": len(ds["rows"])},
            request=request,
        )

        filename = f"kaydan_{rtype}_{today}.{EXT[fmt]}"
        resp = HttpResponse(content, content_type=ctype)
        resp["Content-Disposition"] = f'attachment; filename="{filename}"'
        return resp
