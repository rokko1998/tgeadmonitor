"""Explainable scoring."""
from __future__ import annotations
from dataclasses import dataclass, field
from src.config.loader import HardRule, Scoring
from src.processing.matcher import MatchResult

LEAD = "lead"
PROJECT_SIGNAL = "project_signal"
POTENTIAL = "potential"
IGNORE = "ignore"

@dataclass
class Verdict:
    score: int
    decision: str
    hard_rule: str | None = None
    breakdown: list[tuple[str, int, str]] = field(default_factory=list)
    threshold: int = 0

    def explain(self) -> str:
        lines = [f"score = {self.score} (lead threshold = {self.threshold})"]
        for feature, weight, evidence in self.breakdown:
            lines.append(f"{weight:+d} {feature}: {evidence}")
        if self.hard_rule:
            lines.append(f"hard rule: {self.hard_rule}")
        lines.append(f"decision: {self.decision}")
        return "\n".join(lines)

def _rule_applies(rule: HardRule, features: set[str]) -> bool:
    return all(f in features for f in rule.all) and not any(f in features for f in rule.none)

class Scorer:
    def __init__(self, scoring: Scoring):
        self.s = scoring

    def score(self, r: MatchResult) -> Verdict:
        breakdown, total = [], 0
        for feature, weight in self.s.weights.items():
            if feature not in r.features:
                continue
            evidence = "; ".join(sorted({m.text for m in r.matches if m.feature == feature})) or feature
            if feature == "location_fallback":
                evidence = f"из чата: {r.location}"
            breakdown.append((feature, weight, evidence))
            total += weight

        verdict = Verdict(total, IGNORE, breakdown=breakdown, threshold=self.s.notify_threshold)

        for rule in self.s.not_lead_rules:
            if _rule_applies(rule, r.features):
                verdict.hard_rule = f"not_lead:{rule.id}"
                return verdict

        # The operator's corpus takes priority over numeric scores and generic products.
        if "non_target_product" in r.features and "material_product" not in r.features:
            verdict.hard_rule = "not_lead:non_target_product"
            return verdict
        if "service_request" in r.features:
            verdict.hard_rule = "potential:construction_request"
            verdict.decision = POTENTIAL
            return verdict
        if "ambiguous_material" in r.features and "intent" in r.features and "product" not in r.features:
            verdict.hard_rule = "potential:ambiguous_material"
            verdict.decision = POTENTIAL
            return verdict
        if "short_request" in r.features:
            verdict.hard_rule = "potential:insufficient_details"
            verdict.decision = POTENTIAL
            return verdict
        if "product" not in r.features:
            if "project_signal" in r.features and total >= self.s.project_signal_threshold:
                verdict.decision = POTENTIAL
            return verdict

        for rule in self.s.lead_rules:
            if _rule_applies(rule, r.features):
                verdict.hard_rule = f"lead:{rule.id}"
                verdict.decision = LEAD
                return verdict

        direct_evidence = "product" in r.features and bool(
            {"intent", "question", "size", "amount"} & r.features
        )
        if total >= self.s.notify_threshold and direct_evidence:
            verdict.decision = LEAD
        elif "project_signal" in r.features and total >= self.s.project_signal_threshold:
            verdict.decision = POTENTIAL
        elif total >= self.s.potential_threshold:
            verdict.decision = POTENTIAL
        return verdict
