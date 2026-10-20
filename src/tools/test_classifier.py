from pathlib import Path

import pytest

from src.config.loader import Chat, load_config
from src.processing.classifier import Classifier

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@pytest.fixture(scope="module")
def clf() -> Classifier:
    return Classifier(load_config(CONFIG_DIR))


def test_main_scenario_extracts_everything(clf):
    c = clf.classify("Мужики, подскажите где в Аксае взять профильную трубу 40х20, метров 100 нужно на забор")
    assert c.is_lead
    assert "профильная труба" in c.matches.products
    assert "40x20" in c.matches.sizes
    assert any("100" in a for a in c.matches.amounts)
    assert c.matches.location == "Аксай"
    assert "sell" not in c.matches.features
    assert c.verdict.score >= 8


def test_scoring_example_from_spec(clf):
    c = clf.classify("Нужна профильная труба 40x20, метров 100, Аксай")
    assert c.verdict.score == 15


def test_hard_rule_seller(clf):
    c = clf.classify("Продам арматуру, доставка")
    assert not c.is_lead
    assert c.verdict.hard_rule == "not_lead:seller_with_product"


def test_hard_rule_buyer(clf):
    c = clf.classify("куплю арматуру")
    assert c.verdict.decision == "potential"
    assert c.verdict.hard_rule == "potential:insufficient_details"


def test_location_fallback_from_chat(clf):
    chat = Chat(id="t", name="Щепкин | соседи", chat_id=1, location="Щепкин")
    c = clf.classify("где купить арматуру?", chat)
    assert c.is_lead
    assert c.matches.location == "Щепкин"
    assert c.matches.location_from_chat


def test_phone_alone_is_not_ad(clf):
    c = clf.classify("Нужна арматура. Позвоните +79991234567")
    assert "ad" not in c.matches.features
    assert c.verdict.decision == "potential"


def test_explanation_is_present(clf):
    c = clf.classify("Нужна арматура 12, три тонны")
    text = c.verdict.explain()
    assert "+5 intent" in text and "+4 product" in text and "decision: lead" in text
