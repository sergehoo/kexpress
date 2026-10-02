"""Téléchargement des fichiers protégés (cf. `apps.core.secure_files`)."""
from django.http import Http404
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core import secure_files


class SecureFileDownloadView(APIView):
    """Sert un fichier désigné par une URL signée — après avoir TOUT revérifié.

    Double verrou : l'URL signée (un fichier précis, pour un utilisateur précis, 10 min) ET la
    session de l'appelant (en-tête JWT, comme toute l'API). Une URL qui fuit — historique,
    journal de proxy, capture d'écran partagée — ne sert donc à personne d'autre que son
    destinataire. La règle d'accès est réévaluée MAINTENANT : un compte désactivé, un
    changement de filiale ou de rôle, un document remplacé depuis la délivrance ferment
    l'accès sans attendre l'expiration.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request, token):
        try:
            instance, field_name, owner = secure_files.resolve_token(token)
        except secure_files.InvalidFileToken as exc:
            if str(exc) == "expired":
                raise PermissionDenied("Lien expiré : rouvrez le document depuis l'application.")
            raise Http404("Document introuvable.")
        # Réponse identique à un jeton inconnu : ne pas confirmer qu'un document existe.
        if owner.pk != request.user.pk:
            raise Http404("Document introuvable.")
        if not secure_files.can_access(request.user, instance, field_name):
            raise PermissionDenied("Accès refusé.")
        try:
            return secure_files.file_response(getattr(instance, field_name))
        except OSError:
            raise Http404("Document introuvable.")
