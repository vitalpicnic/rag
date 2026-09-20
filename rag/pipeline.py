"""RAG orchestration with stage metrics, independently of provider initialization."""
from uuid import uuid4

from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnableLambda, RunnablePassthrough
from langchain_core.runnables.history import RunnableWithMessageHistory

from rag.metrics import measure_call
from rag.history import trim_history
from rag.evidence import resolve_query, select_evidence, format_context, finalize_answer, REFUSAL

MAX_INPUT_CHARS = 8000


def build_history_chain(retriever, llm, qa_prompt, history_factory, summary_llm=None, history_budget=800,
                        model_name='gemini-2.5-flash'):
    def retrieve(inputs, config):
        if not isinstance(inputs['input'], str) or len(inputs['input']) > MAX_INPUT_CHARS:
            raise ValueError('Вопрос должен содержать не более 8000 символов.')
        # This duration includes the query embedding; the retriever exposes no usage.
        retrieval_config = {**config, 'metadata': {**config.get('metadata', {}),
                                                 'retrieval_mode': inputs.get('mode', 'text')}}
        documents = measure_call(
            retriever.invoke, resolve_query(inputs['input'], inputs['chat_history']), config=retrieval_config,
            stage='retrieval', model='gemini-embedding-001',
            request_id=inputs['_request_id'],
        )
        return select_evidence(documents, inputs.get('mode', 'text'))

    def generate_answer(inputs, config):
        if not inputs['docs']:
            return REFUSAL
        inputs = {**inputs, 'chat_history': trim_history(inputs['chat_history'], history_budget)}
        prompt = qa_prompt.invoke(inputs, config=config)
        selected_llm = summary_llm if inputs.get('mode') == 'overview' and summary_llm is not None else llm
        # Capture AIMessage usage before StrOutputParser discards the metadata.
        response = measure_call(
            selected_llm.invoke, prompt, config=config,
            stage='generation', model=model_name,
            request_id=inputs['_request_id'],
        )
        return finalize_answer(StrOutputParser().invoke(response), inputs['docs'])

    chain = (
        RunnablePassthrough.assign(_request_id=lambda x: x.get('request_id') or uuid4().hex)
        | RunnablePassthrough.assign(docs=RunnableLambda(retrieve))
        | RunnablePassthrough.assign(
            context=lambda x: format_context(x['docs']),
            response_mode=lambda x: ('Составь краткий обзор только найденных материалов; обозначь пробелы.'
                                     if x.get('mode') == 'overview' else 'Дай короткий ответ по существу.'),
        )
        | {
            'answer': RunnableLambda(generate_answer) | StrOutputParser(),
            'sources': lambda x: x['docs'],
            'request_id': lambda x: x['_request_id'],
        }
        | RunnableLambda(lambda result: {**result,
            'evidence_status': 'insufficient' if result['answer'] == REFUSAL else 'cited_unverified',
            'retrieval_calibrated': False})
    )
    return RunnableWithMessageHistory(
        chain, history_factory, input_messages_key='input',
        history_messages_key='chat_history', output_messages_key='answer',
    )
