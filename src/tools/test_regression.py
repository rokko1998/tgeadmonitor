"""Регрессия по размеченному датасету: изменение regex не должно ломать ранее корректные случаи."""
import json
from pathlib import Path

import pytest

from src.config.loader import Chat, load_config
from src.processing.classifier import Classifier

ROOT = Path(__file__).resolve().parents[2]
ROWS = [json.loads(l) for l in (ROOT / "data" / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.startswith("#")]


@pytest.fixture(scope="module")
def clf() -> Classifier:
    return Classifier(load_config(ROOT / "config"))


@pytest.mark.parametrize("row", ROWS, ids=[r["text"][:50] for r in ROWS])
def test_dataset_row(clf, row):
    chat = Chat(id="d", name="d", location=row["chat_location"]) if row.get("chat_location") else None
    c = clf.classify(row["text"], chat)
    assert c.verdict.decision == row["expected_decision"], c.verdict.explain()
