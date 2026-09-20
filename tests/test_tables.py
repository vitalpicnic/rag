import sys
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / '.runtime_deps'), str(ROOT / '.test_deps')]
from rag.tables import build_tables, load_rows, render_chart, TableNotReady, CHOICES, SOURCE


class TableTests(unittest.TestCase):
    @unittest.skipUnless((ROOT / SOURCE).is_file(), 'Optional local banking PDF is not distributed')
    def test_real_pdf_values_roundtrip_and_png(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'tables.sqlite'
            self.assertEqual(build_tables(ROOT, db), 10)
            rows = load_rows(ROOT, db)
            sber = [r for r in rows if r[0] == 'Сбербанк']
            self.assertEqual([r[2] for r in sber], ['59357.6', '64592.7'])
            png, caption = render_chart(ROOT, db, CHOICES[0])
            self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))
            self.assertIn('01.12.2025', caption)
            self.assertIn('стр. 1', caption)
            with self.assertRaises(ValueError):
                render_chart(ROOT, db, 'прибыль 2035')

    @unittest.skipUnless((ROOT / SOURCE).is_file(), 'Optional local banking PDF is not distributed')
    def test_missing_and_modified_table_fail_closed(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / 'tables.sqlite'
            with self.assertRaises(TableNotReady):
                load_rows(ROOT, db)
            build_tables(ROOT, db)
            with sqlite3.connect(db) as con:
                con.execute("UPDATE observations SET value='999'")
            con.close()
            with self.assertRaises(TableNotReady):
                load_rows(ROOT, db)

    def test_changed_pdf_rejected(self):
        from rag.tables import SOURCE
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / SOURCE
            source.parent.mkdir(parents=True)
            source.write_bytes(b'changed')
            with self.assertRaises(TableNotReady):
                build_tables(root)


if __name__ == '__main__':
    unittest.main()
