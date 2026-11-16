from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from otelwrench.sentry.mapping import convert_bytes
from otelwrench.sentry.output import _rename_new, write_output
from otelwrench.sentry.parser import ConversionError


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.output = self.base / "output"
        self.documents = convert_bytes(b'{"message":"synthetic"}')

    def test_missing_parent_rejected_without_creation(self):
        with self.assertRaisesRegex(ConversionError, "parent directory"):
            write_output(self.documents, self.base / "missing" / "output")
        self.assertEqual(list(self.base.iterdir()), [])

    def test_dangling_symlink_is_never_followed_or_replaced(self):
        self.output.symlink_to(self.base / "missing", target_is_directory=True)
        with self.assertRaisesRegex(ConversionError, "already exists"):
            write_output(self.documents, self.output)
        self.assertTrue(self.output.is_symlink())
        self.assertFalse((self.base / "missing").exists())

    def test_write_failure_cleans_staging_without_publishing(self):
        with patch("otelwrench.sentry.output.os.fsync", side_effect=OSError("synthetic disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                write_output(self.documents, self.output)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_rename_failure_cleans_staging_without_publishing(self):
        with patch("otelwrench.sentry.output._rename_new", side_effect=OSError("synthetic rename failure")):
            with self.assertRaisesRegex(OSError, "rename failure"):
                write_output(self.documents, self.output)
        self.assertEqual(list(self.base.iterdir()), [])

    def test_no_replace_even_if_destination_appears_during_write(self):
        def race(source, destination):
            destination.mkdir()
            (destination / "competitor").write_text("keep")
            return _rename_new(source, destination)
        with patch("otelwrench.sentry.output._rename_new", side_effect=race):
            with self.assertRaises(FileExistsError):
                write_output(self.documents, self.output)
        self.assertEqual({p.name for p in self.base.iterdir()}, {"output"})
        self.assertEqual((self.output / "competitor").read_text(), "keep")
        self.assertEqual({p.name for p in self.output.iterdir()}, {"competitor"})

    def test_unsupported_publication_platform_has_clear_error_and_no_output(self):
        with patch("otelwrench.sentry.output.sys.platform", "unsupported-os"):
            with self.assertRaisesRegex(ConversionError, "requires Linux"):
                write_output(self.documents, self.output)
        self.assertEqual(list(self.base.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
