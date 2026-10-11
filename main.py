from rag.runtime_client import RuntimeClient, RemoteChain
from uuid import uuid4
import getpass
import argparse
from pathlib import Path
from rag.analysis import MODE_LABELS

def main():
    parser = argparse.ArgumentParser(description='RAG CLI')
    parser.add_argument('--topic', default='default')
    parser.add_argument('--mode', choices=MODE_LABELS, default='text', help='Аналитический режим')
    parser.add_argument('--pptx-dir', type=Path, help='Сохранять каждый подтверждённый ссылками ответ в PPTX')
    args = parser.parse_args()
    print("=== ИИ-Ассистент: подключение к общему сервису ===")
    print("(Для выхода введите: выход, exit или stop)\n")
    
    try:
        client = RuntimeClient.from_env(getpass.getuser())
        chain = RemoteChain(client,args.topic)
    except (RuntimeError,ValueError) as exc:
        print(str(exc))
        return
    # Если папка data была пуста, chain вернет None
    if chain is None:
        return

    session_config = {"configurable": {"session_id": uuid4().hex}}

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
                if args.pptx_dir and response.get('export_token'):
                    args.pptx_dir.mkdir(parents=True, exist_ok=True)
                    path = args.pptx_dir / ('analysis-' + uuid4().hex + '.pptx')
                    path.write_bytes(client.download('exports',response['export_token']))
                    print(f'Слайд: {path}')
            else:
                print(f"ИИ: {response}")
        except Exception as e:
            print(f"Произошла ошибка: {e}")

if __name__ == "__main__":
    main()
