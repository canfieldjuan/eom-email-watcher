"""Connect-only Automate rule authority and matching."""

from .rules import (
    MAX_AUTOMATION_ATTACHMENTS,
    MAX_AUTOMATION_FIRES_PER_MESSAGE,
    MAX_AUTOMATION_RULES,
    AutomationFanoutLimit,
    MatchedFire,
    MatchRule,
    RuleDefinition,
    RuleValidationError,
    canonical_rule_definition,
    match_rules,
    parse_rule_definition,
)

__all__ = [
    "MAX_AUTOMATION_ATTACHMENTS",
    "MAX_AUTOMATION_FIRES_PER_MESSAGE",
    "MAX_AUTOMATION_RULES",
    "AutomationFanoutLimit",
    "MatchedFire",
    "MatchRule",
    "RuleDefinition",
    "RuleValidationError",
    "canonical_rule_definition",
    "match_rules",
    "parse_rule_definition",
]
