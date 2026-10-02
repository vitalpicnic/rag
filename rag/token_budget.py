"""Full-payload packing. A model-specific exact counter is mandatory; no estimates."""
from dataclasses import dataclass
from math import ceil

from langchain_core.messages import SystemMessage, HumanMessage

from rag.analysis import response_instructions
from rag.evidence import select_evidence, format_context
from rag.history import complete_turns
from rag.metrics import write_event
from rag.privacy import PrivacyDenied

OUTPUT_BUDGETS = {'text': 600, 'overview': 1800, 'executive': 1800, 'expert': 3000}


class BudgetDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class TokenContract:
    model: str
    counter_id: str  # Verified tokenizer/template identity or documented count API.
    input_limit: int
    output_limit: int
    context_limit: int | None = None  # None means independent input/output limits.
    thinking_tokens: int = 0
    external: bool = False
    provider: str | None = None
    endpoint: str | None = None

    def validate(self):
        positive = (self.input_limit, self.output_limit)
        if self.context_limit is not None:
            positive += (self.context_limit,)
        if (not isinstance(self.model, str) or not self.model
                or not isinstance(self.counter_id, str) or not self.counter_id
                or any(type(n) is not int or n <= 0 for n in positive)
                or type(self.thinking_tokens) is not int or self.thinking_tokens < 0
                or type(self.external) is not bool):
            raise BudgetDenied('Unknown or invalid model token contract')


@dataclass(frozen=True)
class TokenCounter:
    contract: TokenContract
    count_tokens: object  # Callable counts full messages INCLUDING model chat framing.


@dataclass(frozen=True)
class PackedRequest:
    messages: tuple
    evidence: tuple
    history: tuple
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    effective_limit: int
    margin: int
    counter_id: str


def pack_request(system, question, history, evidence, mode, provider, lease=None, *,
                 privacy_gate=None, render=None, history_limit=800):
    """Render must be the generation template; counter must match its wire format.

    Both are trusted provider/runtime dependencies, never request-controlled.
    External counts use the same privacy lease as subsequent generation.
    """
    if not isinstance(provider, TokenCounter) or not isinstance(provider.contract, TokenContract):
        raise BudgetDenied('An exact model counter is required')
    contract = provider.contract
    contract.validate()
    if mode not in OUTPUT_BUDGETS or not isinstance(question, str) or not isinstance(system, str):
        raise BudgetDenied('Invalid request')
    if type(history_limit) is not int or not 0 <= history_limit <= 800:
        raise BudgetDenied('Invalid history budget')
    output = min(OUTPUT_BUDGETS[mode], contract.output_limit - contract.thinking_tokens)
    if output <= 0:
        raise BudgetDenied('No output budget remains after thinking reserve')
    effective = min(16384, contract.input_limit)
    if contract.context_limit is not None:
        effective = min(effective, contract.context_limit - output - contract.thinking_tokens)
    margin = max(256, ceil(0.05 * effective))
    limit = effective - margin
    if limit <= 0:
        raise BudgetDenied('Model window is too small')
    if contract.external and (privacy_gate is None or lease is None
            or contract.provider != privacy_gate.provider or contract.endpoint != privacy_gate.endpoint):
        raise BudgetDenied('External counting requires matching privacy authorization')
    if contract.external:
        privacy_gate.validate(lease)
        authorized = privacy_gate.authorize_documents(evidence, history)
        if not set(authorized.source_ids).issubset(lease.source_ids):
            raise BudgetDenied('Policy lease does not cover request sources')

    def count(messages):
        try:
            if contract.external:
                value = privacy_gate.call(lease, provider.count_tokens, tuple(messages))
            else:
                value = provider.count_tokens(tuple(messages))
        except PrivacyDenied:
            raise
        except Exception:
            raise BudgetDenied('Exact token counting unavailable') from None
        if type(value) is not int or value < 0:
            raise BudgetDenied('Invalid exact token count')
        return value

    def payload(turns, docs):
        if render is not None:
            return tuple(render(turns, docs))
        return (SystemMessage(content=system + '\n' + response_instructions(mode)
                              + '\n' + format_context(docs)),
                *turns, HumanMessage(content=question))

    if count(payload([], [])) > limit:
        raise BudgetDenied('Mandatory instructions or question exceed the model budget')
    turns = complete_turns(history)
    flatten = lambda: [message for turn in turns for message in turn]
    while turns and count(flatten()) > history_limit:
        turns.pop(0)
    candidates = select_evidence(evidence, mode, whole_units=True)
    # Sacrifice older dialogue before removing evidence, never cut a turn or number.
    while turns and count(payload(flatten(), candidates)) > limit:
        turns.pop(0)
    selected = []
    for document in candidates:
        if count(payload(flatten(), [*selected, document])) <= limit:
            selected.append(document)
    if not selected:
        raise BudgetDenied('No whole evidence unit fits the model budget')
    messages = payload(flatten(), selected)
    final_count = count(messages)
    # Recount the actual final payload, even with non-additive framing/tokenization.
    while final_count > limit and selected:
        selected.pop()
        messages = payload(flatten(), selected)
        final_count = count(messages)
    if not selected or final_count > limit:
        raise BudgetDenied('Final request exceeds the model budget')
    if privacy_gate is not None:
        privacy_gate.validate(lease)
    return PackedRequest(messages, tuple(selected), tuple(flatten()), final_count,
                         output, contract.thinking_tokens, effective, margin, contract.counter_id)


def audit_usage(packed, usage, model, request_id):
    actual = usage.get('input_tokens') if isinstance(usage, dict) else None
    valid = type(actual) is int and actual >= 0
    write_event({'schema_version': 1, 'stage': 'token_budget', 'model': model,
                 'request_id': request_id, 'preflight_input_tokens': packed.input_tokens,
                 'actual_input_tokens': actual if valid else None,
                 'input_token_delta': actual - packed.input_tokens if valid else None,
                 'output_reserve': packed.output_tokens, 'thinking_reserve': packed.thinking_tokens,
                 'effective_limit': packed.effective_limit, 'margin': packed.margin})
