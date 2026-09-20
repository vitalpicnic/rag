import streamlit as st
import os
from rag.engine import setup_rag_chain, store
from rag.index import IndexNotReady

st.set_page_config(page_title="Multi-Topic RAG", page_icon="📂", layout="wide")
st.title("📂 ИИ-Поиск по темам с источниками")

# 1. Работа с папками тем
data_root = os.path.join(os.path.dirname(__file__), "data")
if not os.path.exists(data_root):
    os.makedirs(data_root)

# Список подпапок (тем)
folders = [f for f in os.listdir(data_root) if os.path.isdir(os.path.join(data_root, f))]
if not folders:
    folders = ["default"]

# 2. Боковая панель
with st.sidebar:
    st.header("Настройки")
    selected_topic = st.selectbox("Выберите тему знаний:", folders)
    
    if st.button("Очистить историю чата"):
        st.session_state.messages = []
        store.clear()
        st.rerun()
    
    st.divider()
    st.info(f"Текущая папка: data/{selected_topic}")

# 3. Логика переключения тем
if "current_topic" not in st.session_state:
    st.session_state.current_topic = selected_topic

if st.session_state.current_topic != selected_topic:
    st.session_state.current_topic = selected_topic
    st.session_state.messages = []
    store.clear()
    if "chain" in st.session_state:
        del st.session_state.chain

# Инициализация цепочки
if "chain" not in st.session_state:
    with st.spinner(f"Загрузка базы '{selected_topic}'..."):
        try:
            chain = setup_rag_chain(selected_topic)
        except IndexNotReady as exc:
            st.warning(str(exc))
            st.stop()
        if chain:
            st.session_state.chain = chain
        else:
            st.warning(f"Папка '{selected_topic}' пуста или не содержит файлов.")

# 4. Отображение чата
if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# 5. Ввод сообщения
if prompt := st.chat_input("Задайте вопрос по документам..."):
    if "chain" not in st.session_state:
        st.error("Ошибка: База данных не инициализирована. Добавьте файлы в папку темы.")
    else:
        # Добавляем вопрос пользователя
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        # Ответ ассистента
        with st.chat_message("assistant"):
            with st.spinner("Ищу информацию..."):
                config = {"configurable": {"session_id": f"session_{selected_topic}"}}
                
                # Получаем ответ и источники
                full_response = st.session_state.chain.invoke({"input": prompt}, config=config)
                
                answer = full_response["answer"]
                sources = full_response["sources"]

                st.markdown(answer)

                # Вывод источников
                if sources:
                    with st.expander("📚 Источники ответа:"):
                        source_list = []
                        for doc in sources:
                            name = os.path.basename(doc.metadata.get("source", "Документ"))
                            page = doc.metadata.get("page", None)
                            info = f"📄 {name}" + (f" (стр. {page + 1})" if page is not None else "")
                            if info not in source_list:
                                source_list.append(info)
                        
                        for s in source_list:
                            st.write(s)

                st.session_state.messages.append({"role": "assistant", "content": answer})
