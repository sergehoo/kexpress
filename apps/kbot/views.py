"""API K-BOT — Fleet AI Copilot : chat structuré, suggestions, contexte, historique.

Sécurité : chaque requête est scannée (anti prompt-injection), scopée au rôle (les
querysets `for_user` garantissent l'isolation par filiale), et journalisée
(KBotInteraction) — utilisateur, rôle, filiale, intention, source, confiance, temps de
réponse, tentative d'injection.
"""
from __future__ import annotations

import time

from django.utils import timezone
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit import services as audit
from apps.core.enums import AuditAction
from apps.kbot import blocks as B
from apps.kbot.engine import DEFAULT_SUGGESTIONS, answer_question, suggestions_for
from apps.kbot.models import KBotInteraction
from apps.kbot.security import REFUSAL_MESSAGE, is_blocked, scan_question


def _origin_from(data) -> tuple[float, float] | None:
    try:
        lat, lng = data.get("lat"), data.get("lng")
        if lat is not None and lng is not None:
            return (float(lat), float(lng))
    except (TypeError, ValueError):
        pass
    return None


class ChatView(APIView):
    """POST {message, context:{branch_id?, period?}, lat?, lng?} → réponse structurée.

    Contrat : {answer, answer_markdown, blocks[], intent, data, suggestions[],
    data_source, confidence}. Alias historique : /api/kbot/ask/ (champ `question`).
    """

    permission_classes = [IsAuthenticated]

    def post(self, request):
        data = request.data or {}
        question = (data.get("message") or data.get("question") or "").strip()
        context = data.get("context") or {}
        page = (context.get("page") or data.get("page") or "").strip()

        if not question:
            payload = B.respond("empty", answer="Posez-moi une question sur votre flotte.",
                                blocks=[B.paragraph("Posez-moi une question sur votre flotte.")],
                                confidence=0.0, suggestions=suggestions_for(page))
            return Response(payload)

        started = time.monotonic()
        scan = scan_question(question)
        blocked = is_blocked(scan, request.user)

        if blocked:
            payload = B.respond("refused", answer=REFUSAL_MESSAGE,
                                blocks=[B.alert("danger", REFUSAL_MESSAGE)],
                                confidence=1.0, data_source="security_guard",
                                suggestions=suggestions_for(page))
            self._log(request, question, payload, scan, started, refused=True)
            return Response(payload, status=200)

        origin = _origin_from(data)
        payload = answer_question(request.user, question, origin=origin, context=context)
        if not payload.get("suggestions"):
            payload["suggestions"] = suggestions_for(page)
        self._log(request, question, payload, scan, started, refused=False)
        return Response(payload)

    def _log(self, request, question, payload, scan, started, *, refused):
        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            KBotInteraction.objects.create(
                user=request.user,
                role=getattr(request.user, "role", "") or "",
                subsidiary_id=getattr(request.user, "subsidiary_id", None),
                question=question[:2000],
                intent=payload.get("intent", "")[:64],
                data_source=payload.get("data_source", "internal_services")[:32],
                confidence=float(payload.get("confidence", 0.0)),
                response_ms=elapsed_ms,
                injection_flagged=bool(scan.get("injection")),
                refused=refused,
            )
        except Exception:
            pass
        audit.record(
            request.user, AuditAction.ACCESS, None,
            changes={"kbot_question": question[:255], "intent": payload.get("intent"),
                     "refused": refused, "injection": bool(scan.get("injection"))},
            request=request,
        )


class SuggestionsView(APIView):
    """GET /api/kbot/suggestions/?page=dashboard|map|reservations|fleet-control|vehicles|drivers"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        page = request.query_params.get("page", "")
        return Response({"suggestions": suggestions_for(page)})


class ContextView(APIView):
    """GET /api/kbot/context/ → périmètre de l'utilisateur + compteurs clés (scopés)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from apps.analytics.scope import scoped

        user = request.user
        qs = scoped(user)
        today = timezone.localdate()
        scope = "global" if user.is_superuser else ("company" if user.has_company_scope else "subsidiary")
        return Response({
            "role": getattr(user, "role", ""),
            "role_display": user.get_role_display() if hasattr(user, "get_role_display") else "",
            "scope": scope,
            "subsidiary": user.subsidiary.name if getattr(user, "subsidiary_id", None) else None,
            "counts": {
                "available_vehicles": qs["vehicles"].filter(status="available").count(),
                "available_drivers": qs["drivers"].filter(is_available=True).count(),
                "trips_in_progress": qs["trips"].filter(status="in_progress").count(),
                "pending_reservations": qs["reservations"].filter(
                    status__in=["submitted", "pending_manager", "pending_fleet"]
                ).count(),
                "reservations_today": qs["reservations"].filter(trip_date=today).count(),
            },
            "default_suggestions": DEFAULT_SUGGESTIONS,
        })


class HistoryView(APIView):
    """GET → historique K-BOT de l'utilisateur (ses propres entrées) ; DELETE → purge."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        rows = KBotInteraction.objects.filter(user=request.user).order_by("-created_at")[:50]
        return Response({
            "results": [
                {
                    "id": str(r.id),
                    "question": r.question,
                    "intent": r.intent,
                    "confidence": r.confidence,
                    "data_source": r.data_source,
                    "refused": r.refused,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows
            ]
        })

    def delete(self, request):
        deleted, _ = KBotInteraction.objects.filter(user=request.user).delete()
        return Response({"deleted": deleted})
