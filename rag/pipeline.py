"""RAG orchestration with stage metrics, independently of provider initialization."""
from uuid import uuid4
import os

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompt_values import ChatPromptValue
from langchain_core.runnables import RunnableLambda, RunnablePassthrough
from langchain_core.runnables.history import RunnableWithMessageHistory

from rag.metrics import measure_call
from rag.privacy import PrivacyDenied
from rag.token_budget import BudgetDenied, pack_request, audit_usage
from rag.history import trim_history
from rag.analysis import response_instructions
from datetime import datetime, timezone
from rag.evidence import resolve_query, select_evidence, format_context, finalize_answer, REFUSAL

MAX_INPUT_CHARS = 8000


def build_history_chain(retriever, llm, qa_prompt, history_factory, summary_llm=None, history_budget=800,
                        model_name='gemini-2.5-flash', expert_llm=None, privacy_gate=None,
                        token_counter=None):
    if os.getenv('RAG_ENV', 'development') == 'production' and privacy_gate is None:
        raise PrivacyDenied('Production pipeline requires an explicit privacy gate')
    if os.getenv('RAG_ENV', 'development') == 'production' and token_counter is None:
        raise BudgetDenied('Production pipeline requires an exact model counter')
    if token_counter is not None and token_counter.contract.model != model_name:
        raise BudgetDenied('Token counter does not match the generation model')

    def retrieve(inputs, config):
        if not isinstance(inputs['input'], str) or len(inputs['input']) > MAX_INPUT_CHARS:
            raise ValueError('Вопрос должен содержать не более 8000 символов.')
        response_instructions(inputs.get('mode', 'text'))
        # This duration includes the query embedding; the retriever exposes no usage.
        retrieval_config = {**config, 'metadata': {**config.get('metadata', {}),
                                                 'retrieval_mode': inputs.get('mode', 'text')}}
        documents = measure_call(
            retriever.invoke, resolve_query(inputs['input'], inputs['chat_history']), config=retrieval_config,
            stage='retrieval', model='gemini-embedding-001',
            request_id=inputs['_request_id'],
        )
        return select_evidence(documents, inputs.get('mode', 'text'), whole_units=token_counter is not None)

    def prepare(inputs, config):
        # Internal preflight state may only be produced by this chain.
        inputs = {key: value for key, value in inputs.items()
                  if key not in ('_packed', '_policy_lease')}
        if token_counter is None or not inputs['docs']:
            return inputs
        lease = (privacy_gate.authorize_documents(inputs['docs'], inputs['chat_history'])
                 if privacy_gate is not None else None)
        def render(history, documents):
            values = {**inputs, 'chat_history': history, 'context': format_context(documents),
                      'response_mode': response_instructions(inputs.get('mode', 'text'))}
            return qa_prompt.invoke(values, config=config).to_messages()
        packed = pack_request('', inputs['input'], inputs['chat_history'], inputs['docs'],
                              inputs.get('mode', 'text'), token_counter, lease,
                              privacy_gate=privacy_gate, render=render, history_limit=history_budget)
        return {**inputs, 'docs': list(packed.evidence), '_packed': packed, '_policy_lease': lease}

    def generate_answer(inputs, config):
        if not inputs['docs']:
            return REFUSAL
        packed = inputs.get('_packed')
        lease = inputs.get('_policy_lease') if packed else (privacy_gate.authorize_documents(inputs['docs'], inputs['chat_history'])
                 if privacy_gate is not None else None)
        if packed:
            prompt = ChatPromptValue(messages=list(packed.messages))
        else:
            inputs = {**inputs, 'chat_history': trim_history(inputs['chat_history'], history_budget)}
            prompt = qa_prompt.invoke(inputs, config=config)
        selected_llm = llm
        if inputs.get('mode') in ('overview', 'executive') and summary_llm is not None:
            selected_llm = summary_llm
        if inputs.get('mode') == 'expert' and expert_llm is not None:
            selected_llm = expert_llm
        if packed:
            selected_llm = selected_llm.bind(max_output_tokens=packed.output_tokens + packed.thinking_tokens)
        # Capture AIMessage usage before StrOutputParser discards the metadata.
        def invoke(prompt, config):
            if privacy_gate is not None:
                return privacy_gate.call(lease, selected_llm.invoke, prompt, config=config)
            return selected_llm.invoke(prompt, config=config)

        response = measure_call(
            invoke, prompt, config=config,
            stage='generation', model=model_name,
            request_id=inputs['_request_id'],
        )
        if packed:
            audit_usage(packed, getattr(response, 'usage_metadata', None), model_name, inputs['_request_id'])
        answer = finalize_answer(StrOutputParser().invoke(response), inputs['docs'])
        if privacy_gate is not None:
            privacy_gate.validate(lease)
        return answer

    chain = (
        RunnablePassthrough.assign(_request_id=lambda x: x.get('request_id') or uuid4().hex)
        | RunnablePassthrough.assign(docs=RunnableLambda(retrieve))
        | RunnableLambda(prepare)
        | RunnablePassthrough.assign(
            context=lambda x: format_context(x['docs']),
            response_mode=lambda x: response_instructions(x.get('mode', 'text')),
        )
        | {
            'answer': RunnableLambda(generate_answer) | StrOutputParser(),
            'sources': lambda x: x['docs'],
            'request_id': lambda x: x['_request_id'],
            'mode': lambda x: x.get('mode', 'text'),
            'question': lambda x: x['input'],
            'generated_at': lambda x: datetime.now(timezone.utc).isoformat(),
        }
        | RunnableLambda(lambda result: {**result,
            'evidence_status': 'insufficient' if result['answer'] == REFUSAL else 'cited_unverified',
            'retrieval_calibrated': False})
    )
    return RunnableWithMessageHistory(
        chain, history_factory, input_messages_key='input',
        history_messages_key='chat_history', output_messages_key='answer',
    )
