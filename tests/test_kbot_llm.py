"""K-BOT LLM : moteur DeepSeek (API compatible OpenAI) + dispatch fournisseur + replis."""
import json

import pytest
from django.test import override_settings

from apps.kbot import llm


class _Resp:
    """Réponse HTTP factice (context manager + .read())."""

    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self, *a):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@override_settings(
    KBOT_API_KEY="sk-test", KBOT_PROVIDER="deepseek", KBOT_MODEL="deepseek-chat",
    KBOT_BASE_URL="https://api.deepseek.com", KBOT_MAX_TOKENS=600,
)
def test_deepseek_request_and_parse(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None, context=None):
        captured["url"] = req.full_url
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data.decode())
        captured["method"] = req.get_method()
        return _Resp({"choices": [{"message": {"content": "3 véhicules disponibles."}}]})

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    out = llm.ask_llm("Combien de véhicules dispo ?", "CONTEXTE: 3 véhicules disponibles")
    assert out == "3 véhicules disponibles."
    # Endpoint compatible OpenAI + auth Bearer.
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["method"] == "POST"
    assert captured["headers"]["authorization"] == "Bearer sk-test"
    assert captured["headers"]["content-type"] == "application/json"
    # Corps : modèle + messages system/user (contexte injecté).
    assert captured["body"]["model"] == "deepseek-chat"
    assert [m["role"] for m in captured["body"]["messages"]] == ["system", "user"]
    assert "CONTEXTE: 3 véhicules disponibles" in captured["body"]["messages"][1]["content"]


@override_settings(KBOT_API_KEY="", KBOT_PROVIDER="deepseek")
def test_disabled_without_key():
    assert llm.llm_enabled() is False
    assert llm.ask_llm("x", "y") is None


@override_settings(KBOT_API_KEY="sk", KBOT_PROVIDER="deepseek")
def test_network_error_falls_back_to_none(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
    assert llm.ask_llm("x", "y") is None  # repli heuristique


@override_settings(KBOT_API_KEY="sk", KBOT_PROVIDER="deepseek")
def test_empty_choices_returns_none(monkeypatch):
    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda *a, **k: _Resp({"choices": []}))
    assert llm.ask_llm("x", "y") is None


@override_settings(KBOT_API_KEY="sk", KBOT_PROVIDER="deepseek", KBOT_BASE_URL="https://api.deepseek.com/")
def test_base_url_trailing_slash_normalized(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        llm.urllib.request, "urlopen",
        lambda req, **k: seen.setdefault("url", req.full_url) or _Resp({"choices": [{"message": {"content": "ok"}}]}),
    )
    llm.ask_llm("q", "c")
    assert seen["url"] == "https://api.deepseek.com/chat/completions"  # pas de double //


@pytest.mark.django_db
def test_engine_uses_llm_for_freeform_question(monkeypatch):
    from apps.accounts.models import User
    from apps.core.enums import RoleChoices
    from apps.kbot import engine
    from apps.organizations.models import Company, Subsidiary

    company = Company.objects.create(name="Kaydan")
    sub = Subsidiary.objects.create(company=company, name="Abidjan", code="ABJ")
    user = User.objects.create_user(email="m@k.ci", password="x", role=RoleChoices.FLEET_MANAGER, subsidiary=sub)

    monkeypatch.setattr(llm, "llm_enabled", lambda: True)
    monkeypatch.setattr(llm, "ask_llm", lambda q, c: "Analyse libre fournie par le LLM.")

    # Question hors intentions heuristiques → repli LLM (DeepSeek en prod).
    res = engine.answer_question(user, "Donne-moi une analyse stratégique de la flotte pour le trimestre")
    assert res["intent"] == "llm"
    assert res["answer"] == "Analyse libre fournie par le LLM."
