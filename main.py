from rag.engine import setup_rag_chain
from rag.index import IndexNotReady
import argparse

def main():
    parser = argparse.ArgumentParser(description='RAG CLI')
    parser.add_argument('--topic', default='default')
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
            response = chain.invoke({"input": user_input}, config=session_config)
            
            # В новой версии с памятью ответ приходит строкой или словарем
            if isinstance(response, dict):
                print(f"ИИ: {response.get('answer', response.get('output', 'Нет ответа'))}")
            else:
                print(f"ИИ: {response}")
        except Exception as e:
            print(f"Произошла ошибка: {e}")

if __name__ == "__main__":
    main()
