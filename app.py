import streamlit as st
import os
from uuid import uuid4
from rag.runtime_client import RuntimeClient, RemoteChain
from rag.analysis import MODE_LABELS
from rag.slides import PPTX_MIME


def clear_chat():
    st.session_state.messages = []
    st.session_state.pop('last_result', None)
    st.session_state.pop('last_pptx', None)
    prefix = st.session_state.get('chat_id', '')
    if prefix:
        try:
            RuntimeClient.from_env(prefix).reset(prefix,st.session_state.get('current_topic','default'))
        except RuntimeError: st.warning('Сервис пока не подтвердил очистку истории.')
        st.session_state.chat_id = uuid4().hex
        st.session_state.pop('chain',None)

st.set_page_config(page_title="Multi-Topic RAG", page_icon="📂", layout="wide")
st.title("📂 ИИ-Поиск по темам с источниками")

# 1. Работа с папками тем
if 'chat_id' not in st.session_state: st.session_state.chat_id = uuid4().hex
try:
    client = RuntimeClient.from_env(st.session_state.chat_id)
    folders = client.topics()
except (RuntimeError,ValueError):
    st.warning('Внутренний RAG-сервис недоступен или не настроен.')
    st.stop()
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
    clear_chat()
    st.session_state.current_topic = selected_topic
    if "chain" in st.session_state:
        del st.session_state.chain

# Инициализация цепочки
if "chain" not in st.session_state:
    with st.spinner(f"Загрузка базы '{selected_topic}'..."):
        try:
            chain = RemoteChain(RuntimeClient.from_env(st.session_state.chat_id),selected_topic)
        except (RuntimeError,ValueError) as exc:
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
                config = {"configurable": {"session_id": st.session_state.chat_id}}
                
                # Получаем ответ и источники
                try:
                    full_response = st.session_state.chain.invoke({"input": prompt, "mode": selected_mode}, config=config)
                except RuntimeError:
                    st.error('Ответ сейчас недоступен. Проверьте готовность сервиса и выбранной темы.')
                    st.stop()
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
if result and result.get('export_token'):
    if st.button('Подготовить слайд PPTX'):
        try:
            st.session_state.last_pptx = RuntimeClient.from_env(st.session_state.chat_id).download('exports',result['export_token'])
        except (RuntimeError, ValueError) as exc:
            st.error(f'Не удалось подготовить PPTX: {exc}')
    if st.session_state.get('last_pptx'):
        st.download_button('Скачать слайд PPTX', st.session_state.last_pptx,
                           file_name='analysis.pptx', mime=PPTX_MIME)
