"""Unit tests run without a Telegram account. Requires gateway requirements installed."""
import importlib
import sys
import types
import os
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('TELEGRAM_API_ID', '12345')
os.environ.setdefault('TELEGRAM_API_HASH', 'test-api-hash')
os.environ.setdefault('TELETUNE_ACCESS_TOKEN', 'a' * 64)
os.environ.setdefault('TELETUNE_SIGNING_KEY', 'b' * 64)
os.environ.setdefault('TELETUNE_PUBLIC_URL', 'https://example.test')

class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import telethon
        except ModuleNotFoundError:
            telethon = types.ModuleType('telethon')
            telethon.__path__ = []
            telethon.TelegramClient = lambda *args, **kwargs: object()
            telethon.utils = types.SimpleNamespace(get_peer_id=lambda entity: entity.id)
            tl = types.ModuleType('telethon.tl')
            tl.__path__ = []
            tl_types = types.ModuleType('telethon.tl.types')
            class Channel: pass
            class DocumentAttributeAudio:
                def __init__(self, duration, title, performer):
                    self.duration, self.title, self.performer = duration, title, performer
            class DocumentAttributeFilename:
                def __init__(self, file_name): self.file_name = file_name
            tl_types.DocumentAttributeFilename = DocumentAttributeFilename
            tl_types.Channel = Channel
            tl_types.DocumentAttributeAudio = DocumentAttributeAudio
            sys.modules.update({'telethon': telethon, 'telethon.tl': tl,
                                'telethon.tl.types': tl_types})
        cls.app = importlib.import_module('app')

    def test_signature_valid_then_tamper_denied(self):
        expiry = int(time.time()) + 300
        url = self.app.signed_path(-100123, 88, expiry)
        sig = url.split('sig=')[1]
        self.app.validate_signature(-100123, 88, expiry, sig)
        with self.assertRaises(Exception):
            self.app.validate_signature(-100123, 89, expiry, sig)
        with self.assertRaises(Exception):
            self.app.validate_signature(-100123, 88, expiry - 10000, sig)

    def test_byte_ranges(self):
        parse = self.app.parse_byte_range
        self.assertEqual(parse(None, 100), (0, 99, False))
        self.assertEqual(parse('bytes=0-', 100), (0, 99, True))
        self.assertEqual(parse('bytes=20-40', 100), (20, 40, True))
        self.assertEqual(parse('bytes=95-900', 100), (95, 99, True))
        self.assertEqual(parse('bytes=-10', 100), (90, 99, True))
        self.assertEqual(parse('bytes=-500', 100), (0, 99, True))

    def test_reject_bad_ranges(self):
        from fastapi import HTTPException
        for value in ['bytes=-0', 'bytes=', 'bytes=200-', 'bytes=50-20',
                      'bytes=0-2,4-5', 'units=0-10',
                      'bytes=' + '9' * 5000 + '-', 'bytes=-' + '9' * 5000]:
            with self.subTest(value=value), self.assertRaises(HTTPException) as ctx:
                self.app.parse_byte_range(value, 100)
            self.assertEqual(ctx.exception.status_code, 416)
        with self.assertRaises(HTTPException):
            self.app.parse_byte_range(None, 0)

    def test_audio_track_data(self):
        from telethon.tl.types import DocumentAttributeAudio
        m = SimpleNamespace(
            id=11, chat_id=-100456,
            file=SimpleNamespace(name='song.mp3'),
            document=SimpleNamespace(size=123, mime_type='audio/mpeg',
                                     attributes=[DocumentAttributeAudio(duration=123, title='Bài hát', performer='Ca sĩ')]))
        self.assertEqual(self.app.audio_info(m)['id'], 'tg:-100456:11')
        self.assertEqual(self.app.audio_info(m)['duration_seconds'], 123)
        m.document.attributes = []
        self.assertEqual(self.app.audio_info(m)['title'], 'song')

if __name__ == '__main__':
    unittest.main()

class PlaylistSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import tempfile
        from pathlib import Path
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'playlists.json'
        from unittest.mock import patch
        self.patch = patch.object(GatewayTests.app if hasattr(GatewayTests, 'app') else importlib.import_module('app'),
                                  'SYNC_FILE', self.path)
        self.patch.start()
        self.app = importlib.import_module('app')

    async def asyncTearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    async def test_upload_download_revision_and_conflict(self):
        from fastapi import HTTPException
        blank = await self.app.get_playlists()
        self.assertEqual(blank['revision'], 0)
        song = self.app.SyncedTrack(id='tg:-1007:12', title='Sample', artist='Singer',
                                    channel_id=-1007, message_id=12)
        item = self.app.SyncedPlaylist(name='Favorites', tracks=[song])
        result = await self.app.put_playlists(self.app.PlaylistUpdate(revision=0, playlists=[item]))
        self.assertEqual(result['revision'], 1)
        self.assertEqual((await self.app.get_playlists())['playlists'][0]['tracks'][0]['id'], 'tg:-1007:12')
        with self.assertRaises(HTTPException) as ctx:
            await self.app.put_playlists(self.app.PlaylistUpdate(revision=0, playlists=[]))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual((await self.app.get_playlists())['revision'], 1)

    async def test_reject_invalid_telegram_id_and_duplicate_names(self):
        from fastapi import HTTPException
        bad = self.app.SyncedTrack(id='wrong', title='Example', artist='Singer',
                                   channel_id=-1007, message_id=12)
        with self.assertRaises(HTTPException) as ctx:
            await self.app.put_playlists(self.app.PlaylistUpdate(revision=0, playlists=[
                self.app.SyncedPlaylist(name='List', tracks=[bad])]))
        self.assertEqual(ctx.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            await self.app.put_playlists(self.app.PlaylistUpdate(revision=0, playlists=[
                self.app.SyncedPlaylist(name='Repeat', tracks=[]),
                self.app.SyncedPlaylist(name='Repeat', tracks=[])]))

def setUpModule():
    GatewayTests.setUpClass()

class AudioDocumentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = importlib.import_module('app')

    def message(self, name, mime, attrs=None):
        from types import SimpleNamespace
        return SimpleNamespace(id=9, chat_id=-1009,
            file=SimpleNamespace(name=name),
            document=SimpleNamespace(size=2048, mime_type=mime, attributes=attrs or []))

    def test_mp3_without_audio_attribute_recognized(self):
        info = self.app.audio_info(self.message('track.mp3', 'application/octet-stream'))
        self.assertEqual(info['title'], 'track')
        self.assertEqual(info['filename'], 'track.mp3')
        self.assertEqual(info['duration_seconds'], 0)
        self.assertEqual(info['mime_type'], 'audio/mpeg')

    def test_flac_mime_and_attribute_filename(self):
        from telethon.tl.types import DocumentAttributeFilename
        msg = self.message('unknown', 'audio/flac', [DocumentAttributeFilename('Song.FLAC')])
        self.assertEqual(self.app.audio_info(msg)['title'], 'Song')
        self.assertEqual(self.app.audio_info(msg)['mime_type'], 'audio/flac')

    def test_video_and_non_audio_document_rejected(self):
        self.assertIsNone(self.app.audio_info(self.message('clip.mp3', 'video/mp4')))
        self.assertIsNone(self.app.audio_info(self.message('report.pdf', 'application/pdf')))
        self.assertIsNone(self.app.audio_info(self.message('virus.mp3', 'application/x-msdownload')))

class ChannelPaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_next_cursor_and_filtered_audio(self):
        app = importlib.import_module('app')
        from types import SimpleNamespace
        messages = [SimpleNamespace(id=i, chat_id=-1001,
            file=SimpleNamespace(name=f'{i}.mp3'),
            document=SimpleNamespace(size=123, mime_type='audio/mpeg', attributes=[])) for i in (50, 49, 48)]
        class Client:
            async def get_entity(self, value): return object()
            async def iter_messages(self, entity, limit, offset_id):
                eligible = [m for m in messages if not offset_id or m.id < offset_id]
                for message in eligible[:limit]: yield message
        with patch.object(app, 'client', Client()):
            first = await app.channel_tracks(-1001, limit=2)
            self.assertEqual([x['message_id'] for x in first['tracks']], [50, 49])
            self.assertEqual(first['next_before_id'], 49)
            second = await app.channel_tracks(-1001, limit=2, before_id=49)
            self.assertEqual([x['message_id'] for x in second['tracks']], [48])

class StreamHeadersTests(unittest.IsolatedAsyncioTestCase):
    async def test_head_partial_does_not_download(self):
        from starlette.requests import Request
        app = importlib.import_module('app')
        msg = SimpleNamespace(id=10, chat_id=-1002,
            file=SimpleNamespace(name='song.mp3'),
            document=SimpleNamespace(size=1000, mime_type='audio/mpeg', attributes=[]),
            media=object())
        class Client:
            async def get_entity(self, value): return object()
            async def get_messages(self, entity, ids): return msg
            async def iter_download(self, *args, **kwargs):
                raise AssertionError('HEAD must not initiate file transfer')
                yield b''
        expiry = int(time.time()) + 300
        sig = app.signed_path(-1002, 10, expiry).split('sig=')[1]
        request = Request({'type': 'http', 'method': 'HEAD', 'path': '/v1/stream/-1002/10',
                           'headers': [(b'range', b'bytes=100-199')]})
        with patch.object(app, 'client', Client()):
            result = await app.stream(-1002, 10, expiry, sig, request)
        self.assertEqual(result.status_code, 206)
        self.assertEqual(result.headers['content-range'], 'bytes 100-199/1000')
        self.assertEqual(result.headers['content-length'], '100')
        self.assertEqual(result.headers['accept-ranges'], 'bytes')

class ScanBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_scan_limits_rejected(self):
        from fastapi import HTTPException
        app = importlib.import_module('app')
        for limit, cursor in [(0, 0), (501, 0), (200, -1)]:
            with self.subTest(limit=limit, cursor=cursor), self.assertRaises(HTTPException) as ctx:
                await app.channel_tracks(-1001, limit=limit, before_id=cursor)
            self.assertEqual(ctx.exception.status_code, 400)

    async def test_empty_audio_page_advances_cursor(self):
        app = importlib.import_module('app')
        msgs = [SimpleNamespace(id=i, chat_id=-1001, document=None) for i in (30, 29, 28)]
        class Client:
            async def get_entity(self, value): return object()
            async def iter_messages(self, entity, limit, offset_id):
                for msg in [m for m in msgs if not offset_id or m.id < offset_id][:limit]:
                    yield msg
        with patch.object(app, 'client', Client()):
            first = await app.channel_tracks(-1001, limit=2)
            self.assertEqual(first['tracks'], [])
            self.assertEqual(first['next_before_id'], 29)
            second = await app.channel_tracks(-1001, limit=2, before_id=29)
            self.assertEqual(second['next_before_id'], 28)
