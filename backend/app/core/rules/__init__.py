"""确定性强类型规则核心。"""

from app.core.rules.canonical import (
    canonical_json,
    rule_config_fingerprint,
    ruleset_fingerprint,
    validate_rule_definition,
    value_set_fingerprint,
)
from app.core.rules.evaluator import (
    aggregate_verdict,
    evaluate_rule,
    evaluate_rule_selection,
    select_duplicate_match,
)
from app.core.rules.models import (
    DuplicateMatch,
    InvoiceOccurrence,
    RowVerdict,
    RuleDefinition,
    RuleEvaluation,
    RuleKind,
    RuleOutcome,
    RuleVersion,
)
from app.core.rules.reasoning import render_reasoning
from app.core.rules.selection import select_effective_rule_version

__all__ = [
    "DuplicateMatch",
    "InvoiceOccurrence",
    "RowVerdict",
    "RuleDefinition",
    "RuleEvaluation",
    "RuleKind",
    "RuleOutcome",
    "RuleVersion",
    "aggregate_verdict",
    "canonical_json",
    "evaluate_rule",
    "evaluate_rule_selection",
    "render_reasoning",
    "rule_config_fingerprint",
    "ruleset_fingerprint",
    "select_duplicate_match",
    "select_effective_rule_version",
    "validate_rule_definition",
    "value_set_fingerprint",
]
