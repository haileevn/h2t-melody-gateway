import json
import tempfile
import unittest
from pathlib import Path

from updates import latest_release


class UpdateManifestTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'releases.json'
        self.release = {'version': '2.1.0', 'build': 21,
                        'url': 'https://example.org/H2T-Melody-v2.1.0.ipa',
                        'sha256': 'a' * 64, 'notes': 'Update notes', 'mandatory': False}

    def publish(self, obj):
        self.path.write_text(json.dumps(obj), encoding='utf-8')

    def test_missing_manifest_has_no_update(self):
        self.assertEqual(latest_release(str(self.path), 'ios'), {'available': False})

    def test_valid_release(self):
        self.publish({'ios': self.release})
        self.assertTrue(latest_release(str(self.path), 'ios')['available'])
        self.assertEqual(latest_release(str(self.path), 'ios')['build'], 21)
        self.assertEqual(latest_release(str(self.path), 'android'), {'available': False})

    def test_http_and_credentials_rejected(self):
        for url in ('http://example.org/app.ipa', 'https://user:pass@example.org/a.ipa',
                    'https://example.org/a.ipa#abc'):
            with self.subTest(url=url):
                self.publish({'ios': {**self.release, 'url': url}})
                with self.assertRaises(ValueError): latest_release(str(self.path), 'ios')

    def test_bad_hash_and_invalid_build(self):
        for change in ({'sha256': 'x' * 64}, {'build': -2}, {'build': True}):
            with self.subTest(change=change):
                self.publish({'ios': {**self.release, **change}})
                with self.assertRaises(ValueError): latest_release(str(self.path), 'ios')

    def test_invalid_platform(self):
        with self.assertRaises(ValueError): latest_release(str(self.path), 'windows')

    def test_oversize_rejected(self):
        self.path.write_text(' ' * 33000, encoding='utf-8')
        with self.assertRaises(ValueError): latest_release(str(self.path), 'ios')


if __name__ == '__main__': unittest.main()
