"""Gestionnaire d'exceptions DRF du projet."""
from django.db.models import ProtectedError
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler
from rest_framework.views import set_rollback


def exception_handler(exc, context):
    """Une suppression ou une modification refusée pour préserver l'historique (coût figé,
    mois clos, enregistrement référencé) répond 409 avec son motif, et non une erreur 500.

    Quand le refus vient d'un mois clos ou d'un coût figé, la réponse porte une PROPOSITION
    d'ajustement financier (`adjustment_proposal`) : l'utilisateur n'est pas face à une
    impasse, l'interface lui propose de créer l'ajustement."""
    if isinstance(exc, ProtectedError):
        message = exc.args[0] if exc.args else "Suppression impossible : donnée référencée."
        set_rollback()  # comme DRF pour ses propres erreurs (requêtes atomiques)
        body = {"detail": str(message)}
        proposal = getattr(exc, "proposal", None)
        if proposal:
            body.update(code="adjustment_required", adjustment_proposal=proposal)
        return Response(body, status=status.HTTP_409_CONFLICT)
    return drf_exception_handler(exc, context)
