"""Request bodies checked rule by rule, as class-validator did, so a wrong or missing
value gets every message NestJS gave, in its order (first used by wellness).

Each rule is (field, optional, trimmed, check): IsOptional skips null and absent; a
required field runs its checks on whatever is there, absent included. Unknown fields
answer "property k should not exist" first, as forbidNonWhitelisted did.
"""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, model_validator

from app.os.common import dates, validators
from app.os.common.jsparse import _JS_SPACE


def trim(value):
    return value.strip(_JS_SPACE) if isinstance(value, str) else value


def number(minimum, maximum, places=None):
    return lambda v, f: validators.number_rules(v, f, minimum, maximum, places)


def text(maximum, minimum=None):
    return lambda v, f: validators.text_rules(v, f, maximum, minimum)


def choice(choices):
    return lambda v, f: validators.choice_rules(v, f, choices)


def iso_date(v, f):
    return [] if isinstance(v, str) and dates.is_iso_date_string(v) else [f"{f} must be a valid ISO 8601 date string"]


def hhmm(v, f):
    return [] if isinstance(v, str) and validators.HHMM.match(v) else [f"{f} must be a time like 08:30"]


def _rule_problems(rules: tuple, data: dict) -> list[str]:
    names = [r[0] for r in rules]
    problems = [f"property {k} should not exist" for k in data if k not in names]
    for field, optional, _trimmed, rule in rules:
        value = data.get(field)
        if optional and value is None:
            continue
        problems += rule(value, field)
    return problems


def nested(rules: tuple):
    """@ValidateNested() @Type(() => Child): the child's messages under "field.", as
    NestJS flattened them. An array is checked element by element."""

    def check(value, field):
        if isinstance(value, dict):
            return [f"{field}.{p}" for p in _rule_problems(rules, value)]
        if isinstance(value, list):
            problems = []
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    problems += [f"{field}.{i}.{p}" for p in _rule_problems(rules, item)]
                else:
                    problems.append(f"{field}.nested property {field} must be either object or array")
            return problems
        return [f"nested property {field} must be either object or array"]

    return check


class RuleBody(BaseModel):
    """Fields are plain values; RULES checks them before anything else runs."""

    model_config = ConfigDict(extra="forbid")
    RULES: ClassVar[tuple] = ()

    @model_validator(mode="before")
    @classmethod
    def _check(cls, data):
        if not isinstance(data, dict):
            return data
        data = {k: trim(v) if any(r[0] == k and r[2] for r in cls.RULES) else v for k, v in data.items()}
        problems = _rule_problems(cls.RULES, data)
        if problems:
            raise ValueError("\n".join(problems))
        return data

    def sent(self, field: str) -> bool:
        return field in self.model_fields_set
