"""Manual proposal ingestion and the contract for a future model API adapter."""
import json
from typing import Protocol

PROPOSAL_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['action', 'name', 'expression', 'rationale'],
    'properties': {
        'action': {'type': 'string', 'enum': ['propose', 'stop']},
        'name': {'type': 'string', 'maxLength': 100},
        'expression': {'type': 'string', 'maxLength': 500},
        'rationale': {'type': 'string', 'maxLength': 500},
    },
}


class ProposalProvider(Protocol):
    """Implementations receive only scoped context/feedback, never raw market or holdout rows.

    A future API provider must additionally reserve/record call and token budgets before
    network I/O. No remote provider is implemented or called in this manual-first release.
    """
    def propose(self, context: dict, feedback: list) -> dict | None: ...


def parse_proposals(value):
    if isinstance(value, str):
        if len(value.encode()) > 20_000:
            raise ValueError('提案 JSON 最多 20 KB')
        value = value.strip()
        if value.startswith('```json\n') and value.endswith('```'):
            value = value[8:-3].strip()
        try:
            value = json.loads(value, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('JSON 必须使用有限数值')))
        except (json.JSONDecodeError, ValueError):
            raise ValueError('提案需要有效 JSON 数组或对象') from None
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not 1 <= len(value) <= 10:
        raise ValueError('每批需要 1 至 10 个提案')
    result = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {'action', 'name', 'expression', 'rationale'}:
            raise ValueError('每个提案必须且只能包含 action、name、expression、rationale')
        if item['action'] not in ('propose', 'stop'):
            raise ValueError('action 必须为 propose 或 stop')
        for key, limit in (('name', 100), ('expression', 500), ('rationale', 500)):
            if not isinstance(item[key], str) or len(item[key]) > limit:
                raise ValueError(f'提案 {key} 必须是最多 {limit} 字的文本')
            if (item['action'] == 'propose' or key == 'rationale') and not item[key].strip():
                raise ValueError(f'提案 {key} 不能为空')
        result.append({key: val.strip() for key, val in item.items()})
    return result


class ManualProposalProvider:
    def __init__(self, proposals):
        self.proposals = iter(proposals)

    def propose(self, context, feedback):
        return next(self.proposals, None)
