from rag.engine import setup_rag_chain
from rag.index import IndexNotReady
import argparse
from pathlib import Path
from rag.analysis import MODE_LABELS
from rag.slides import export_answer_pptx, can_export

def main():
    parser = argparse.ArgumentParser(description='RAG CLI')
    parser.add_argument('--topic', default='default')
    parser.add_argument('--mode', choices=MODE_LABELS, default='text', help='Аналитический режим')
    parser.add_argument('--pptx-dir', type=Path, help='Сохранять каждый подтверждённый ссылками ответ в PPTX')
    args = parser.parse_args()
    print("=== ИИ-Ассистент готов к работе! ===")
    print("(Для выхода введите: выход, exit или stop)\n")
    
    try:
        chain = setup_rag_chain(args.topic)
    except IndexNotReady as exc:
        print(str(exc))
        return
    # Если папка data была пуста, chain вернет None
    if chain is None:
        return

    session_config = {"configurable": {"session_id": "user_1"}}

    while True:
        user_input = input("Вы: ").strip()
        
        # Сначала проверяем команду на выход
        if user_input.lower() in ["выход", "exit", "stop", "quit"]:
            print("Выключаюсь... До связи!")
            break # Вот теперь цикл реально прервется
        
        if not user_input:
            continue

        print("Думаю...")
        try:
            # Отправляем в ИИ только если это не команда выхода
            response = chain.invoke({"input": user_input, "mode": args.mode}, config=session_config)
            
            # В новой версии с памятью ответ приходит строкой или словарем
            if isinstance(response, dict):
                print(f"ИИ: {response.get('answer', response.get('output', 'Нет ответа'))}")
                if args.pptx_dir and can_export(response):
                    args.pptx_dir.mkdir(parents=True, exist_ok=True)
                    from uuid import uuid4
                    path = args.pptx_dir / ('analysis-' + uuid4().hex + '.pptx')
                    path.write_bytes(export_answer_pptx(response))
                    print(f'Слайд: {path}')
            else:
                print(f"ИИ: {response}")
        except Exception as e:
            print(f"Произошла ошибка: {e}")

if __name__ == "__main__":
    main()
