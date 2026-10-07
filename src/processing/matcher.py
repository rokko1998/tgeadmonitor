"""Regex/dictionary matcher."""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from src.config.loader import AppConfig, Chat
from src.processing.extractor import SurfaceFeatures

@dataclass(frozen=True)
class Match:
    feature: str
    rule: str
    text: str
    value: str | None = None

@dataclass
class MatchResult:
    matches: list[Match] = field(default_factory=list)
    features: set[str] = field(default_factory=set)
    products: list[str] = field(default_factory=list)
    product_ids: list[str] = field(default_factory=list)
    sizes: list[str] = field(default_factory=list)
    amounts: list[str] = field(default_factory=list)
    location: str | None = None
    location_from_chat: bool = False
    intent_texts: list[str] = field(default_factory=list)

    def add(self, feature: str, rule: str, text: str, value: str | None = None) -> None:
        m = Match(feature, rule, text.strip(), value)
        if m not in self.matches:
            self.matches.append(m)
        self.features.add(feature)

class Matcher:
    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self._delivery = re.compile(r"\bдоставк\w*")
        self._in_stock = re.compile(r"\bв\s+наличии\b")
        self._opt_roznica = re.compile(r"\bопт\w*\s+и\s+в\s+розниц\w*|\bопт\s*/\s*розниц\w*")

    @staticmethod
    def _negated(norm: str, start: int) -> bool:
        return norm[max(0, start - 3):start].endswith("не ")

    @staticmethod
    def _joined_groups(m: re.Match) -> str:
        parts = [g.replace(",", ".") for g in m.groups() if g]
        return "x".join(parts) if parts else m.group(0)

    def match(self, norm: str, orig: str, surface: SurfaceFeatures, chat: Chat | None) -> MatchResult:
        r, cfg = MatchResult(), self.cfg

        for rule in cfg.intents:
            for m in rule.regex.finditer(norm):
                if self._negated(norm, m.start()):
                    continue
                r.add("intent", rule.id, m.group(0))
                r.intent_texts.append(m.group(0))
                if rule.strong:
                    r.features.add("intent_strong")
                break

        for p in cfg.products:
            found = next((rx.search(norm) for rx in p.patterns if rx.search(norm)), None)
            if not found:
                continue
            r.add("product", p.id, found.group(0), value=p.name)
            if p.name not in r.products:
                r.products.append(p.name)
                r.product_ids.append(p.id)
            for rx in p.size_patterns:
                sm = rx.search(norm)
                if sm:
                    val = self._joined_groups(sm)
                    r.add("size", f"{p.id}_size", sm.group(0), value=val)
                    if val not in r.sizes:
                        r.sizes.append(val)

        for rx in cfg.size_patterns:
            for sm in rx.finditer(norm):
                val = self._joined_groups(sm)
                r.add("size", "size", sm.group(0), value=val)
                if val not in r.sizes:
                    r.sizes.insert(0, val)

        for rx in cfg.amount_patterns:
            for am in rx.finditer(norm):
                val = am.group(0)
                r.add("amount", "amount", val, value=val)
                if val not in r.amounts:
                    r.amounts.append(val)

        for loc in cfg.locations:
            for rx in loc.patterns:
                lm = rx.search(norm)
                if lm:
                    r.add("location", loc.name, lm.group(0), value=loc.name)
                    r.location = r.location or loc.name
                    break
        if r.location is None and chat and chat.location:
            r.location = chat.location
            r.location_from_chat = True
            r.features.add("location_fallback")

        for rule in cfg.context_rules:
            m = rule.regex.search(norm)
            if m:
                r.add("context", rule.id, m.group(0))

        for rule in cfg.project_signal_rules:
            m = rule.regex.search(norm)
            if m:
                r.add("project_signal", rule.id, m.group(0))

        for rule in cfg.professional_actor_rules:
            m = rule.regex.search(norm)
            if m:
                r.add("professional_actor", rule.id, m.group(0))

        for rule in cfg.sell_rules:
            m = rule.regex.search(norm)
            if m:
                r.add("sell", rule.id, m.group(0))

        if surface.is_question:
            r.add("question", "question", "?")
        if surface.urls:
            r.add("url", "url", surface.urls[0])
        if len(r.product_ids) >= cfg.ad.many_products_threshold:
            r.add("many_products", "many_products", ", ".join(r.products))

        flags = []
        if surface.phones: flags.append("phone")
        if surface.urls: flags.append("url")
        if surface.usernames: flags.append("username")
        if len(surface.prices) >= cfg.ad.price_count_threshold: flags.append("prices")
        if "many_products" in r.features: flags.append("many_products")
        if self._delivery.search(norm) and self._in_stock.search(norm): flags.append("delivery+stock")
        if self._opt_roznica.search(norm): flags.append("wholesale+retail")
        if surface.emoji_count >= cfg.ad.emoji_threshold: flags.append("emoji")
        if surface.letters >= cfg.ad.caps_min_letters and surface.caps_ratio >= cfg.ad.caps_ratio_threshold:
            flags.append("caps")
        if len(flags) >= cfg.ad.min_features:
            r.add("ad", "ad", "+".join(flags))
        return r
