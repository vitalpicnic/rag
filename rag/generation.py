"""Native Ollama chat adapter. It never downloads models or falls back to cloud."""
import json
from urllib.error import URLError, HTTPError
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OllamaChat:
    def __init__(self, settings):
        self.settings = settings
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def _post(self, path, payload):
        request = Request(self.settings.ollama_url + path,
                          data=json.dumps(payload).encode('utf-8'),
                          headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with self.opener.open(request, timeout=self.settings.timeout) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError('Response too large')
            result = json.loads(raw)
            if not isinstance(result, dict) or result.get('error'):
                raise ValueError('Invalid Ollama response')
            return result
        except HTTPError as exc:
            exc.close()
            raise RuntimeError('Ollama request failed; no cloud fallback') from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise RuntimeError('Ollama request failed; no cloud fallback') from None

    def invoke(self, prompt, config=None, *, max_output_tokens=600):
        if type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 3000:
            raise ValueError('Invalid output budget')
        info = self._post('/api/show', {'model': self.settings.model})
        if info.get('remote_host') or info.get('remote_model'):
            raise RuntimeError('Cloud-backed Ollama models are forbidden')
        messages = prompt.to_messages() if hasattr(prompt, 'to_messages') else prompt
        roles = {'human': 'user', 'ai': 'assistant', 'system': 'system'}
        payload = []
        for message in messages:
            if message.type not in roles or not isinstance(message.content, str):
                raise ValueError('Only text system/user/assistant messages are supported')
            payload.append({'role': roles[message.type], 'content': message.content})
        result = self._post('/api/chat', {
            'model': self.settings.model, 'messages': payload, 'stream': False, 'think': False,
            'keep_alive': '5m', 'options': {'temperature': 0, 'num_ctx': self.settings.context_window,
                                         'num_predict': max_output_tokens},
        })
        content = result.get('message', {}).get('content')
        if result.get('done') is not True or not isinstance(content, str):
            raise RuntimeError('Ollama returned an incomplete answer')
        usage = None
        incoming, outgoing = result.get('prompt_eval_count'), result.get('eval_count')
        if all(type(n) is int and n >= 0 for n in (incoming, outgoing)):
            usage = {'input_tokens': incoming, 'output_tokens': outgoing, 'total_tokens': incoming + outgoing}
        return AIMessage(content=content, usage_metadata=usage,
                         response_metadata={'model_name': result.get('model', self.settings.model),
                                            'provider': 'ollama', 'done_reason': result.get('done_reason')})

    def bind(self, *, max_output_tokens):
        return RunnableLambda(lambda prompt, config: self.invoke(prompt, config, max_output_tokens=max_output_tokens))


def create_generation(settings):
    if settings.llm_provider == 'ollama':
        if settings.environment == 'production':
            raise ValueError('Production requires verified tokenizer/context budget integration')
        return OllamaChat(settings)
    if settings.llm_provider == 'gemini':
        if not settings.google_key:
            raise ValueError('GOOGLE_API_KEY is required for Gemini')
        from langchain_google_genai import ChatGoogleGenerativeAI
        from rag.models import model_options
        return ChatGoogleGenerativeAI(**model_options(settings.model), google_api_key=settings.google_key,
                                      temperature=0, max_output_tokens=600, timeout=30,
                                      max_retries=2, vertexai=False)
    raise ValueError('OpenAI-compatible adapter is not implemented yet')
