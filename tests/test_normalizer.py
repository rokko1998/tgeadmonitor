from src.processing.normalizer import normalize


def test_spec_example():
    assert normalize("НУЖНА труба 40Х20!!! Кто продаёт???") == "нужна труба 40x20 кто продает"


def test_x_variants_and_decimal():
    assert normalize("труба 40 х 20 × 1,5") == "труба 40x20x1,5"
    assert normalize("40*20") == "40x20"


def test_dashes_and_spaces():
    assert normalize("Ростов — на  —  Дону") == "ростов - на - дону"
    assert normalize("проф. труба") == "проф труба"