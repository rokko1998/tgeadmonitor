"""Versioned Jev questions and deterministic delivery decisions."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

VERSION = "owner-reviewed-2026-10-21-v1"


def load_questions(config_dir: Path) -> tuple[dict, str]:
    questions = json.loads((config_dir / "jev_questions.json").read_text(encoding="utf-8"))
    if not questions or any(q.get("type") != "choice" for q in questions.values()):
        raise ValueError("Invalid Jev question configuration")
    raw = json.dumps(questions, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return questions, hashlib.sha256(raw.encode()).hexdigest()


def validate_response(data: dict, questions: dict) -> None:
    if not isinstance(data, dict) or not isinstance(data.get("model"), str):
        raise ValueError("Missing resolved model")
    answers = data.get("answers")
    if not isinstance(answers, dict):
        raise ValueError("Missing answers")
    for name, question in questions.items():
        answer = answers.get(name, {})
        if not isinstance(answer, dict):
            raise ValueError("Invalid Choice object")
        options = set(question["criteria"])
        probs = answer.get("probabilities", {})
        if answer.get("type") != "choice" or answer.get("choice") not in options:
            raise ValueError("Invalid Choice answer")
        if not isinstance(probs, dict) or set(probs) != options or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            or not 0 <= v <= 1 for v in probs.values()
        ) or abs(sum(probs.values()) - 1) > .06:
            raise ValueError("Invalid Choice probabilities")
    for name in ("input_tokens", "output_tokens"):
        n = data.get("usage", {}).get(name)
        if isinstance(n, bool) or not isinstance(n, int) or n < 0:
            raise ValueError("Invalid usage")


@dataclass(frozen=True)
class SemanticDecision:
    label: str
    reason: str
    confidence: float


def decide(answers: dict) -> SemanticDecision:
    fit = answers["business_fit"]
    label = fit["choice"]
    probability = fit["probabilities"][label]
    intent = answers["intent"]["probabilities"]
    if label == "ignore":
        return SemanticDecision("ignore", "no_useful_business_opportunity", probability)
    if label == "potential":
        return SemanticDecision("potential", "insufficient_purchase_detail", probability)
    if label == "indirect":
        # The owner explicitly keeps concrete construction-project recruitment
        # as an indirect opportunity. Employment is not a universal veto here.
        role = sum(intent.get(k, 0) for k in ("demand", "employment", "planning"))
        scope = answers["scope"]["choice"]
        operation = answers["operation"]["choice"]
        if (probability >= .75 and role >= .60 and intent.get("offer", 0) < .40
                and scope in {"metal_material", "lumber", "metal_service", "adjacent_service"}
                and operation in {"supply", "fabrication", "contractor", "staffing", "unspecified"}):
            return SemanticDecision("indirect", "construction_contact_opportunity", probability)
        return SemanticDecision("review", "uncertain_indirect_opportunity", probability)
    if label == "direct":
        scope = answers["scope"]["probabilities"]
        operation = answers["operation"]["probabilities"]
        detail = answers["detail"]["probabilities"]
        evidence = min(
            intent.get("demand", 0),
            sum(scope.get(k, 0) for k in ("metal_material", "lumber", "metal_service")),
            operation.get("supply", 0) + operation.get("fabrication", 0), probability,
        )
        if evidence >= .80 and detail.get("sourcing", 0) + detail.get("specified", 0) >= .60:
            return SemanticDecision("direct", "qualified_target_purchase_or_fabrication", evidence)
        return SemanticDecision("review", "weak_or_conflicting_direct_evidence", evidence)
    return SemanticDecision("review", "insufficient_semantic_evidence", probability)
