"""Cheap, high-recall gate before any paid semantic evaluation."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone

VERSION = "prefilter-2026-10-21-v1"
DOMAIN = re.compile(
    r"арматур|металл|стальн|сталь\b|проф(?:настил|лист|труб)|швел+ер|двутавр|"
    r"\bвгп\b|\b[аa]\s*500[сc]?\b|\b[аa]\s*400\b|\bст\s*3\b|09г2с|с255|"
    r"круг\s*\d|квадрат\s*\d|полос|шестигран|\bпвл\b|штрипс|катанк|решетчат.{0,10}настил|"
    r"рабиц|штакетник|сетк|проволок|закладн|свар|резк|гибк|пиломат|лесомат|доск|брус|"
    r"пиленн.{0,8}лес|рейк|каркас|навес|ворот|забор|стро|фундамент|монолит|"
    r"кровл|крыша|крыши|стяж|кладк|каменщик|каменьщик|блок|сантех|отоплен|"
    r"кот[её]л|котельн|труб|водопровод|канализац|электрик|вентиляц|ремонт|"
    r"подряд|тендер|штукатур|плиточник|мкд|капремонт|закуп|поставщик|"
    r"лист\b|лист[аыуое]в?\b|уголок|уголк|балк|ангар|теплиц|фанер|сосед",
    re.I,
)
DEMAND = re.compile(
    r"купл[юя]|купим|купить|покуп|нуж[а-я]*|надо|требу|ищ[уеи]|ищем|"
    r"кто\b|где\b|поч[её]м|стоимост|сколько|интересу|подскаж|посовет|"
    r"контакт|прайс|цен[аыуе]|налич|достав|привез|сдела|сварить|порез|"
    r"изготов|заказ|закуп|поставщик|возьм[её]т|возьмет|помо|пожалуйста|есть\b",
    re.I,
)
BUYER = re.compile(
    r"куплю|купим|покупаю|покупаем|где\s+купить|ищ[уе]м?\s+поставщик|"
    r"нуж[еа-я]*\s+поставщик|прошу\s+.*прайс|(?:нуж|требу).{0,80}подрядчик|"
    r"кто\s+(?:может\s+)?(?:сдела|привез|порез|свар)|подскаж|посовет",
    re.I,
)
SELLER = re.compile(r"\b(?:продам|прода[её]м|предлагаем|предоставляем|реализуем|"
                    r"изготовим|производим|поставляем|наш\s+спектр\s+услуг)\b", re.I)
HANDLING = re.compile(r"\b(?:перенести|переместить|переместите|разгрузить|выгрузить|"
                      r"погрузить|поднимите|спустите|сложить|складировать)\b", re.I)
SPEC = re.compile(r"\d\s*(?:мм|метр|м\b|т\b|тонн|кг|шт|куб|х\s*\d|x\s*\d)", re.I)
DEADLINE = re.compile(r"(?:подач[а-я\s]{0,30}заяв[а-я]*|окончание\s+при[её]ма)"
                      r"[^\n]{0,70}?(\d{2}\.\d{2}\.\d{4})", re.I)
PHONE = re.compile(r"(?<!\d)(?:\+?7|8)[\s()\-]*\d(?:[\s()\-]*\d){9}(?!\d)")
URL = re.compile(r"https?://\S+|(?<!\w)@[a-zA-Z][a-zA-Z0-9_]{3,}")


def clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return re.sub(r"[\u200b-\u200f\ufeff]", "", text)


def prefilter(text: str, now: datetime | None = None) -> str | None:
    """Return an explicit rejection reason, or None to ask Jev. No score gate."""
    t = clean(text)
    if not re.search(r"[а-яa-z]", t):
        return "no_text"
    if not DOMAIN.search(t):
        return "no_relevant_subject"
    for value in DEADLINE.findall(t):
        try:
            due = datetime.strptime(value, "%d.%m.%Y").date()
        except ValueError:
            continue
        if due < (now or datetime.now(timezone.utc)).date():
            return "expired_submission_deadline"
    buyer = BUYER.search(t)
    if SELLER.search(t) and not buyer:
        return "explicit_supplier_advertisement"
    if HANDLING.search(t) and not buyer and len(t) < 450:
        return "handling_existing_goods"
    if not DEMAND.search(t) and not SPEC.search(t):
        return "no_demand_or_specification"
    return None


def semantic_text_hash(text: str) -> str:
    # Unlike the old classifier's hash, retain punctuation, quantities and negation.
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def opportunity_key(text: str) -> str | None:
    """Deduplicate substantive project reposts, including contact-only differences."""
    t = clean(URL.sub("", PHONE.sub("", text)))
    t = re.sub(r"\[(?:телефон|phone)\]", "", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = " ".join(t.split())
    # Generic short requests by unrelated people must not be merged.
    return hashlib.sha256(t.encode()).hexdigest() if len(t) >= 100 else None
