"""Configuration loader."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import yaml

FLAGS = re.IGNORECASE
class ConfigError(Exception):
    pass

@dataclass(frozen=True)
class Rule:
    id: str
    regex: re.Pattern
    strong: bool = False

@dataclass(frozen=True)
class Product:
    id: str
    name: str
    group: str
    patterns: tuple[re.Pattern, ...]
    size_patterns: tuple[re.Pattern, ...]

@dataclass(frozen=True)
class Location:
    name: str
    patterns: tuple[re.Pattern, ...]

@dataclass
class Chat:
    id: str
    name: str
    chat_id: int | None = None
    username: str | None = None
    location: str | None = None
    district: str | None = None
    kind: str | None = None
    source_type: str = "legacy"
    require_location: bool = False
    auto_join: bool = False
    enabled: bool = True
    priority: int = 1
    comment: str | None = None

@dataclass(frozen=True)
class AdSettings:
    min_features: int = 2
    many_products_threshold: int = 4
    price_count_threshold: int = 2
    emoji_threshold: int = 5
    caps_ratio_threshold: float = 0.5
    caps_min_letters: int = 20

@dataclass
class AppConfig:
    chats: list[Chat]
    products: list[Product]
    context_rules: list[Rule]
    project_signal_rules: list[Rule]
    professional_actor_rules: list[Rule]
    source_types: tuple[str, ...]
    size_patterns: list[re.Pattern]
    amount_patterns: list[re.Pattern]
    intents: list[Rule]
    sell_rules: list[Rule]
    ad: AdSettings
    locations: list[Location]
    non_target_rules: list[Rule]
    potential_request_rules: list[Rule]
    request_detail_rules: list[Rule]
    ambiguous_material_rules: list[Rule]

    def enabled_chats(self) -> list[Chat]:
        return [c for c in self.chats if c.enabled]

def _load_yaml(path: Path, required: bool = True) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"Config file not found: {path}")
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def _compile(pattern: str, where: str) -> re.Pattern:
    if not isinstance(pattern, str) or not pattern:
        raise ConfigError(f"{where}: pattern must be a non-empty string")
    try:
        return re.compile(pattern, FLAGS)
    except re.error as e:
        raise ConfigError(f"{where}: invalid regex {pattern!r}: {e}") from e

def _compile_list(patterns: list[str] | None, where: str) -> tuple[re.Pattern, ...]:
    return tuple(_compile(p, f"{where}[{i}]") for i, p in enumerate(patterns or []))

def _rules(items: list[dict] | None, where: str) -> list[Rule]:
    result, seen = [], set()
    for i, item in enumerate(items or []):
        rid = str(item.get("id") or f"{where}_{i}")
        if rid in seen:
            raise ConfigError(f"{where}: duplicate rule id {rid!r}")
        seen.add(rid)
        result.append(Rule(rid, _compile(item.get("pattern"), f"{where}.{rid}"),
                           bool(item.get("strong", False))))
    return result

def load_config(config_dir: Path) -> AppConfig:
    config_dir = Path(config_dir)

    source_types = tuple(str(x) for x in
                         (_load_yaml(config_dir / "source_types.yaml").get("source_types") or []))
    if not source_types:
        raise ConfigError("source_types.yaml: source_types is empty")

    # Chat state is loaded from SQLite by the runtime, not from rule files.
    chats: list[Chat] = []

    prod_raw = _load_yaml(config_dir / "products.yaml")
    products = []
    product_ids: set[str] = set()
    for i, p in enumerate(prod_raw.get("products") or []):
        pid = p.get("id")
        if not pid:
            raise ConfigError(f"products.yaml[{i}]: 'id' is required")
        if pid in product_ids:
            raise ConfigError(f"products.yaml: duplicate product id {pid!r}")
        product_ids.add(pid)
        pats = _compile_list(p.get("patterns"), f"products.{pid}.patterns")
        if not pats:
            raise ConfigError(f"products.{pid}: 'patterns' must not be empty")
        products.append(Product(
            id=str(pid), name=str(p.get("name") or pid), group=str(p.get("group") or ""),
            patterns=pats,
            size_patterns=_compile_list(p.get("size_patterns"), f"products.{pid}.size_patterns"),
        ))
    context_rules = _rules(prod_raw.get("construction_context"), "construction_context")
    size_patterns = list(_compile_list(prod_raw.get("sizes"), "sizes"))
    amount_patterns = list(_compile_list(prod_raw.get("amounts"), "amounts"))

    intents = _rules(_load_yaml(config_dir / "intents.yaml").get("purchase_intents"),
                     "purchase_intents")
    if not intents:
        raise ConfigError("intents.yaml: purchase_intents is empty")

    signals = _load_yaml(config_dir / "project_signals.yaml")
    qualification = _load_yaml(config_dir / "qualification.yaml")
    project_signal_rules = _rules(signals.get("project_signals"), "project_signals")
    professional_actor_rules = _rules(signals.get("professional_actors"), "professional_actors")

    excl_raw = _load_yaml(config_dir / "exclusions.yaml")
    sell_rules = _rules(excl_raw.get("sell_intents"), "sell_intents")
    ad = AdSettings(**{k: v for k, v in (excl_raw.get("ad_features") or {}).items()
                       if k in AdSettings.__dataclass_fields__})

    locations = []
    location_rows = _load_yaml(config_dir / "locations.yaml").get("locations")
    if not isinstance(location_rows, list) or not location_rows:
        raise ConfigError("locations.yaml: 'locations' must be a non-empty list")
    for i, loc in enumerate(location_rows):
        if not loc.get("name"):
            raise ConfigError(f"locations.yaml[{i}]: 'name' is required")
        pats = _compile_list(loc.get("patterns"), f"locations.{loc['name']}")
        if not pats:
            raise ConfigError(f"locations.{loc['name']}: 'patterns' must not be empty")
        locations.append(Location(str(loc["name"]), pats))

    return AppConfig(
        chats=chats, products=products, context_rules=context_rules,
        project_signal_rules=project_signal_rules,
        professional_actor_rules=professional_actor_rules,
        source_types=source_types, size_patterns=size_patterns, amount_patterns=amount_patterns,
        intents=intents, sell_rules=sell_rules, ad=ad, locations=locations,
        non_target_rules=_rules(qualification.get("non_target_products"), "non_target_products"),
        potential_request_rules=_rules(qualification.get("potential_requests"), "potential_requests"),
        request_detail_rules=_rules(qualification.get("request_details"), "request_details"),
        ambiguous_material_rules=_rules(qualification.get("ambiguous_materials"), "ambiguous_materials"),
    )
