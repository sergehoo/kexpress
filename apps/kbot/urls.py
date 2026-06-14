from django.urls import path

from apps.kbot.views import ChatView, ContextView, HistoryView, SuggestionsView

urlpatterns = [
    path("kbot/chat/", ChatView.as_view(), name="kbot-chat"),
    # Alias historique (champ `question`) — rétrocompatibilité du panneau existant.
    path("kbot/ask/", ChatView.as_view(), name="kbot-ask"),
    path("kbot/suggestions/", SuggestionsView.as_view(), name="kbot-suggestions"),
    path("kbot/context/", ContextView.as_view(), name="kbot-context"),
    path("kbot/history/", HistoryView.as_view(), name="kbot-history"),
]
