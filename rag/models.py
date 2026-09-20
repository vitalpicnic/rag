"""Explicit model profiles and text-only Standard tariff estimates, USD."""
import os

PRICING_URL = 'https://ai.google.dev/gemini-api/docs/pricing'
PRICING_CHECKED = '2026-09-20'
PROFILES = {
    'gemini-2.5-flash': {'input': .30, 'output': 2.50, 'thinking_budget': 0},
    'gemini-2.5-flash-lite': {'input': .10, 'output': .40, 'thinking_budget': 0},
    'gemini-3.1-flash-lite': {'input': .25, 'output': 1.50, 'thinking_level': 'low'},
}
DEFAULT_MODEL = 'gemini-2.5-flash'


def model_options(model=None):
    model = model or os.getenv('RAG_MODEL', '').strip() or DEFAULT_MODEL
    if model not in PROFILES:
        raise ValueError('Unsupported RAG_MODEL: ' + model)
    return {'model': model, **{k: v for k, v in PROFILES[model].items() if k.startswith('thinking_')}}


def generation_cost(event):
    """Upper estimate ignoring cache discounts, not an invoice. Output includes thinking."""
    profile = PROFILES.get(event.get('model'))
    counts = event.get('input_tokens'), event.get('output_tokens')
    if (event.get('stage') != 'generation' or not profile or
            any(type(n) is not int or n < 0 for n in counts)):
        return None
    return (counts[0] * profile['input'] + counts[1] * profile['output']) / 1_000_000


def summarize_costs(events):
    calls = [e for e in events if e.get('stage') == 'generation']
    values = [generation_cost(e) for e in calls]
    missing = sum(v is None for v in values)
    subtotal = sum(v for v in values if v is not None)
    return {'generation_usd_upper_estimate': None if missing or not calls else round(subtotal, 8),
            'known_generation_usd_subtotal': round(subtotal, 8),
            'generation_calls': len(calls), 'unknown_usage_calls': missing,
            'scope': 'generation only; excludes embeddings, hidden retries, hosting, taxes; ignores cache discounts',
            'pricing_checked': PRICING_CHECKED, 'pricing_url': PRICING_URL}


def scenario(model, input_tokens, output_tokens, requests):
    if any(type(n) is not int or n < 0 for n in (input_tokens, output_tokens, requests)):
        raise ValueError('Scenario counts must be nonnegative integers')
    model_options(model)
    cost = generation_cost(dict(stage='generation', model=model,
                                input_tokens=input_tokens, output_tokens=output_tokens))
    return {'status': 'scenario_not_measurement', 'model': model, 'requests': requests,
            'input_tokens_per_request': input_tokens, 'output_tokens_per_request': output_tokens,
            'generation_usd': round(cost * requests, 8)}
