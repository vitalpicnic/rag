from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class PathTests(unittest.TestCase):
    def test_canonicalization_without_links_allows_platform_aliases(self):
        from rag.paths import unlinked_path
        with tempfile.TemporaryDirectory() as tmp:
            original=Path(tmp)/'new-file'
            canonical=Path(tmp).resolve()/'canonical-name'
            with patch.object(Path,'resolve',return_value=canonical):
                self.assertEqual(unlinked_path(original),canonical)

    def test_linked_ancestor_and_junction_are_rejected_before_resolving(self):
        from rag.paths import unlinked_path
        for method in ('is_symlink','is_junction'):
            with patch.object(Path,method,return_value=True),patch.object(Path,'resolve') as resolve:
                with self.assertRaises(ValueError): unlinked_path('example/file')
                resolve.assert_not_called()

    def test_parent_traversal_is_not_silently_normalized(self):
        from rag.paths import unlinked_path
        with self.assertRaises(ValueError): unlinked_path(Path('example')/'..'/'file')
