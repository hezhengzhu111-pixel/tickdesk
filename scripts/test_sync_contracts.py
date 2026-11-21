from pathlib import Path
import tempfile
import unittest
from sync_contracts import sync

class ExportTests(unittest.TestCase):
    def test_drift_missing_and_unknown_files_fail_without_repair(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'contracts/v2'
            source.mkdir(parents=True)
            (source / 'commands.json').write_text('{}')
            sync(root)
            sync(root, check=True)
            target = root / 'android-app/contracts/v2/commands.json'
            target.write_text('{"drift":true}')
            with self.assertRaises(ValueError): sync(root, check=True)
            self.assertEqual(target.read_text(), '{"drift":true}')
            target.unlink()
            with self.assertRaises(ValueError): sync(root, check=True)
            sync(root)
            extra = target.with_name('unknown.json')
            extra.write_text('{}')
            with self.assertRaises(ValueError): sync(root)
            self.assertTrue(extra.exists())

if __name__ == '__main__': unittest.main()
