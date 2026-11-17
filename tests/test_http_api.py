"""HTTP contract tests against mock Telegram; do not require a live account."""
import importlib
import tempfile
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('TELEGRAM_API_ID', '12345')
os.environ.setdefault('TELEGRAM_API_HASH', 'mock-secret')
os.environ.setdefault('TELETUNE_ACCESS_TOKEN', 'a' * 64)
os.environ.setdefault('TELETUNE_SIGNING_KEY', 'b' * 64)
os.environ.setdefault('TELETUNE_PUBLIC_URL', 'https://example.test')

# Reuse the Telethon stub from the existing tests when the optional package is absent.
import test_gateway as gateway_test_support
gateway_test_support.GatewayTests.setUpClass()
api = importlib.import_module('app')
from fastapi.testclient import TestClient


class MockTelegram:
    def __init__(self):
        self.document = SimpleNamespace(size=10, mime_type='audio/mpeg', attributes=[])
        self.message = SimpleNamespace(id=7, chat_id=-10042, document=self.document,
                                       file=SimpleNamespace(name='track.mp3'), media=object())
    async def get_entity(self, value):
        return object()
    async def get_messages(self, entity, ids):
        return self.message if ids == 7 else None
    async def iter_download(self, media, offset=0, chunk_size=None):
        payload = b'0123456789'
        # Emulate Telegram's offset behavior.
        for i in range(offset, len(payload), 3):
            yield payload[i:i + 3]
    async def iter_messages(self, entity, limit, offset_id):
        if not offset_id or offset_id > 7:
            yield self.message


class HttpApiTests(unittest.TestCase):
    def setUp(self):
        self.mock = MockTelegram()
        self.patch = patch.object(api, 'client', self.mock)
        self.patch.start()
        # Suppress the Telegram connection lifespan; tests provide a fake client.
        self.client = TestClient(api.app, headers={'Authorization': 'Bearer ' + 'a' * 64})

    def tearDown(self):
        self.client.close()
        self.patch.stop()

    def test_authorization_enforced(self):
        response = self.client.get('/v1/channels/-10042/tracks', headers={'Authorization': ''})
        self.assertEqual(response.status_code, 401)

    def test_scan_contract(self):
        response = self.client.get('/v1/channels/-10042/tracks?limit=10')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['tracks'][0]['id'], 'tg:-10042:7')
        self.assertEqual(payload['next_before_id'], 7)

    def test_stream_complete_partial_and_head(self):
        expires = int(time.time()) + 300
        url = api.signed_path(-10042, 7, expires)
        path = url.removeprefix('https://example.test')
        all_bytes = self.client.get(path)
        self.assertEqual(all_bytes.status_code, 200)
        self.assertEqual(all_bytes.content, b'0123456789')
        self.assertEqual(all_bytes.headers['content-length'], '10')
        portion = self.client.get(path, headers={'range': 'bytes=3-6'})
        self.assertEqual(portion.status_code, 206)
        self.assertEqual(portion.content, b'3456')
        self.assertEqual(portion.headers['content-range'], 'bytes 3-6/10')
        suffix = self.client.get(path, headers={'range': 'bytes=-3'})
        self.assertEqual(suffix.status_code, 206)
        self.assertEqual(suffix.content, b'789')
        head = self.client.head(path, headers={'range': 'bytes=3-6'})
        self.assertEqual(head.status_code, 206)
        self.assertEqual(head.content, b'')
        self.assertEqual(head.headers['content-length'], '4')
        invalid = self.client.get(path, headers={'range': 'bytes=99-'})
        self.assertEqual(invalid.status_code, 416)
        self.assertEqual(invalid.headers['content-range'], 'bytes */10')

    def test_stream_missing_song_returns_404(self):
        expires = int(time.time()) + 300
        path = api.signed_path(-10042, 999, expires).removeprefix('https://example.test')
        response = self.client.get(path)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.headers.get('cache-control'), 'private, no-store')

    def test_sensitive_metadata_never_cached(self):
        for path in ['/v1/channels/-10042/tracks?limit=10', '/v1/tracks/-10042/7/play']:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get('cache-control'), 'private, no-store')
                self.assertEqual(response.headers.get('pragma'), 'no-cache')

    def test_stream_rejects_malformed_range_without_download(self):
        expires = int(time.time()) + 300
        path = api.signed_path(-10042, 7, expires).removeprefix('https://example.test')
        for raw in ('bytes=5-4', 'bytes=0-1,3-5', 'bytes=' + '9' * 300 + '-'):
            with self.subTest(raw=raw):
                response = self.client.get(path, headers={'range': raw})
                self.assertEqual(response.status_code, 416)
                self.assertEqual(response.headers['content-range'], 'bytes */10')

    def test_tampered_stream_denied(self):
        expires = int(time.time()) + 300
        path = api.signed_path(-10042, 7, expires).removeprefix('https://example.test')
        self.assertEqual(self.client.get(path.replace('/7?', '/8?')).status_code, 403)


if __name__ == '__main__':
    unittest.main()

class UpdateRouteTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(api.app, headers={'Authorization': 'Bearer ' + 'a' * 64})

    def tearDown(self):
        self.client.close()

    def test_update_route_rejects_missing_auth(self):
        result = self.client.get('/v1/updates/latest?platform=ios', headers={'Authorization': ''})
        self.assertEqual(result.status_code, 401)

    def test_update_route_empty_manifest_and_bad_platform(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {'H2T_RELEASES_PATH': tmp + '/not-published.json'}):
                result = self.client.get('/v1/updates/latest?platform=ios')
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json(), {'available': False})
                self.assertEqual(self.client.get('/v1/updates/latest?platform=bad').status_code, 422)

    def test_update_route_valid_release(self):
        import json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'releases.json'
            file.write_text(json.dumps({'ios': {
                'version': '2.1.0', 'build': 21,
                'url': 'https://example.org/H2T-Melody.ipa', 'sha256': 'f' * 64,
                'notes': 'Release notes', 'mandatory': False,
            }}), encoding='utf-8')
            with patch.dict(os.environ, {'H2T_RELEASES_PATH': str(file)}):
                resp = self.client.get('/v1/updates/latest?platform=ios')
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()['build'], 21)
                self.assertEqual(resp.json()['version'], '2.1.0')

    def test_update_route_corrupt_manifest_returns_503(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / 'releases.json'
            file.write_text('{broken-json', encoding='utf-8')
            with patch.dict(os.environ, {'H2T_RELEASES_PATH': str(file)}):
                self.assertEqual(self.client.get('/v1/updates/latest?platform=ios').status_code, 503)
