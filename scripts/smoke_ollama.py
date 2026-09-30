"""Test the Ollama adapter with a neutral prompt, never a document corpus."""
import json
import os
from langchain_core.messages import HumanMessage
from rag.config import load_settings
from rag.generation import create_generation


def main():
    try:
        settings = load_settings({**os.environ, 'RAG_PROFILE': 'LOCAL', 'RAG_LLM_PROVIDER': 'ollama',
                                  'RAG_EMBEDDING_PROVIDER': 'local'})
        response = create_generation(settings).invoke(
            [HumanMessage(content='Ответь одним числом: сколько будет 2 + 2?')], max_output_tokens=32)
        print(json.dumps({'status': 'response_received', 'model': settings.model,
                          'answer': response.content, 'usage': response.usage_metadata}, ensure_ascii=False))
        return 0
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({'status': 'failed', 'reason': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
