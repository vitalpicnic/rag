"""Reviewed numerical observations; no model or network dependencies."""
import hashlib
from io import BytesIO
import logging
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

SOURCE = 'data/Банкинг/РИА_Рейтинг_Рейтинг_банков_по_объему_активов_на_1_декабря_2025_года.pdf'
SOURCE_HASH = 'f20803e486d52923e718f3303ab8d94483897dd938424461499c4d0f04b326b2'
# Entity, complete original PDF row, January value, December value; decimal strings.
REVIEWED = [
    ('Сбербанк', '1 1 ПАО Сбербанк (лиц. 1481) 64592.7 59357.6 8.8%', '59357.6', '64592.7'),
    ('ВТБ', '2 2 Банк ВТБ (ПАО) (лиц. 1000) 33704.1 33747.8 -0.1%', '33747.8', '33704.1'),
    ('Альфа-Банк', '4 4 АО "АЛЬФА-БАНК" 12630.1 11507.4 9.8%', '11507.4', '12630.1'),
    ('Россельхозбанк', '6 6 АО "Россельхозбанк" (лиц. 3349) 6152.6 5573.5 10.4%', '5573.5', '6152.6'),
    ('Т-Банк', '8 9 АО "ТБанк" (лиц. 2673) 5215.7 3819.8 36.5%', '3819.8', '5215.7'),
]
CHOICES = tuple('Активы: ' + item[0] for item in REVIEWED) + ('Активы: сравнение банков',)
DB_PATH = 'prepared_tables/banking.sqlite'


class TableNotReady(RuntimeError):
    pass


def expected_rows():
    return sorted((name, date, value, 'млрд руб.', 'оценка активов', SOURCE, 1, SOURCE_HASH, evidence)
                  for name, evidence, january, december in REVIEWED
                  for date, value in [('2025-01-01', january), ('2025-12-01', december)])


def verify_source(root):
    path = Path(root) / SOURCE
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != SOURCE_HASH:
        raise TableNotReady('Источник таблицы изменён или отсутствует. Нужна повторная проверка данных.')
    return path


def build_tables(root, database=None):
    from pypdf import PdfReader
    logging.getLogger('pypdf').setLevel(logging.ERROR)
    text = ' '.join(PdfReader(verify_source(root)).pages[0].extract_text().split())
    for heading in ('на 1.12.25 г.,млрд руб.', 'на 1.1.25 г.,млрд руб.'):
        if heading not in text:
            raise TableNotReady('Не подтверждены даты и единицы столбцов.')
    if any(evidence not in text for _, evidence, _, _ in REVIEWED):
        raise TableNotReady('Не подтверждена строка таблицы.')
    database = Path(database) if database else Path(root) / DB_PATH
    database.parent.mkdir(parents=True, exist_ok=True)
    temporary = database.with_name(database.name + '.' + uuid4().hex + '.tmp')
    try:
        con = sqlite3.connect(temporary)
        try:
            with con:
                con.execute('CREATE TABLE observations (entity TEXT, date TEXT, value TEXT, unit TEXT, '
                            'metric TEXT, source TEXT, page INTEGER, source_sha256 TEXT, evidence TEXT, '
                            'PRIMARY KEY(entity,date,metric))')
                con.executemany('INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?)', expected_rows())
        finally:
            con.close()
        load_rows(root, temporary)
        os.replace(temporary, database)
    finally:
        temporary.unlink(missing_ok=True)
    return len(expected_rows())


def load_rows(root, database=None):
    verify_source(root)
    database = Path(database) if database else Path(root) / DB_PATH
    try:
        con = sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            rows = con.execute('SELECT entity,date,value,unit,metric,source,page,source_sha256,evidence '
                               'FROM observations ORDER BY entity,date').fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        raise TableNotReady('Проверенные таблицы не готовы. Выполните build_tables.py.') from exc
    if rows != expected_rows():
        raise TableNotReady('Таблица не соответствует проверенным значениям. Выполните build_tables.py.')
    return rows


def render_chart(root, database, choice):
    if choice not in CHOICES:
        raise ValueError('Выберите доступную проверенную таблицу кнопкой «Активы: …».')
    rows = load_rows(root, database)
    names = [r[0] for r in REVIEWED] if choice == CHOICES[-1] else [choice.removeprefix('Активы: ')]
    os.environ.setdefault('MPLCONFIGDIR', str(Path(root) / 'prepared_tables' / '.matplotlib'))
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fig = Figure(figsize=(10, 6), dpi=140)
    FigureCanvasAgg(fig)
    ax = fig.subplots()
    for offset, date, label, color in [(-.2, '2025-01-01', '01.01.2025', '#8297b1'),
                                        (.2, '2025-12-01', '01.12.2025', '#2065a8')]:
        values = [float(next(r[2] for r in rows if r[0] == name and r[1] == date)) for name in names]
        bars = ax.bar([i + offset for i in range(len(names))], values, width=.38, label=label, color=color)
        ax.bar_label(bars, labels=[f'{v:,.1f}'.replace(',', ' ').replace('.', ',') for v in values],
                     padding=4, fontsize=8)
    ax.set_xticks(range(len(names)), names, fontsize=9)
    ax.set_ylabel('Оценка активов, млрд руб.')
    ax.set_title('Активы банков: два среза 2025 года', pad=20)
    ax.set_ylim(0, ax.get_ylim()[1] * 1.15)
    ax.legend()
    ax.spines[['top', 'right']].set_visible(False)
    ax.set_axisbelow(True)
    ax.grid(axis='y', alpha=.2)
    fig.text(.08, .03, 'Источник: РИА Рейтинг, стр. 1. Оценки на 1 января и 1 декабря; не итог года.', fontsize=9)
    fig.subplots_adjust(left=.1, right=.97, top=.85, bottom=.15)
    output = BytesIO()
    fig.savefig(output, format='png')
    caption = 'Оценка активов, млрд руб. Срезы 01.01.2025 и 01.12.2025; не итог года. РИА Рейтинг, стр. 1.'
    return output.getvalue(), caption
