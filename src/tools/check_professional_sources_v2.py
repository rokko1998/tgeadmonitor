"""Structural smoke test for Professional Sources v2."""
from pathlib import Path
from tempfile import TemporaryDirectory
from src.config.loader import load_config
from src.processing.classifier import Classifier
from src.storage.database import Database

def main():
    cfg = load_config(Path("config"))
    assert cfg.project_signal_rules
    assert cfg.professional_actor_rules
    assert any(c.source_type == "subcontract" for c in cfg.enabled_chats())
    assert not any(c.enabled and c.kind == "барахолка" for c in cfg.chats)

    classifier = Classifier(cfg)
    examples = [
        "Нужна кладочная сетка 100x100 500 м2 Ростов",
        "Требуются монолитчики, армирование сеткой 200x200 на объекте Ростов-на-Дону",
        "Нужен монтаж вентиляции на коммерческий объект Ростов",
        "Ищем прораба на новый объект, начинаем монолит в Ростове",
    ]
    for text in examples:
        result = classifier.classify(text)
        print(result.verdict.decision, result.verdict.score, text)

    with TemporaryDirectory() as td:
        db = Database(Path(td) / "test.db")
        assert "source_type" in {r["name"] for r in db.conn.execute("PRAGMA table_info(sources)")}
        assert "lead_type" in {r["name"] for r in db.conn.execute("PRAGMA table_info(leads)")}
        print(db.stats(30)["messages_total"])
        db.close()
    print("OK")

if __name__ == "__main__":
    main()
