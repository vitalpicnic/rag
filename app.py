import streamlit as st
import os
from uuid import uuid4
from rag.engine import setup_rag_chain, store
from rag.index import IndexNotReady
from rag.analysis import MODE_LABELS
from rag.slides import export_answer_pptx, can_export, PPTX_MIME


def clear_chat():
    st.session_state.messages = []
    st.session_state.pop('last_result', None)
    st.session_state.pop('last_pptx', None)
    prefix = st.session_state.get('chat_id', '')
    if prefix:
        for key in list(store):
            if key.startswith(prefix + ':'):
                store.pop(key, None)

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
    selected_mode = st.selectbox('Режим ответа:', list(MODE_LABELS), format_func=MODE_LABELS.get)
    
    if st.button("Очистить историю чата"):
        clear_chat()
        st.rerun()
    
    st.divider()
    st.info(f"Текущая папка: data/{selected_topic}")

# 3. Логика переключения тем
if "current_topic" not in st.session_state:
    st.session_state.current_topic = selected_topic
if 'chat_id' not in st.session_state:
    st.session_state.chat_id = uuid4().hex

if st.session_state.current_topic != selected_topic:
    st.session_state.current_topic = selected_topic
    clear_chat()
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
                config = {"configurable": {"session_id": f"{st.session_state.chat_id}:{selected_topic}:{selected_mode}"}}
                
                # Получаем ответ и источники
                full_response = st.session_state.chain.invoke({"input": prompt, "mode": selected_mode}, config=config)
                st.session_state.last_result = full_response
                st.session_state.pop('last_pptx', None)
                
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

result = st.session_state.get('last_result')
if result and can_export(result):
    if st.button('Подготовить слайд PPTX'):
        try:
            st.session_state.last_pptx = export_answer_pptx(result)
        except (ImportError, ValueError) as exc:
            st.error(f'Не удалось подготовить PPTX: {exc}')
    if st.session_state.get('last_pptx'):
        st.download_button('Скачать слайд PPTX', st.session_state.last_pptx,
                           file_name='analysis.pptx', mime=PPTX_MIME)
