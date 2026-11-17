"""Single-owner Telegram audio gateway. Run behind HTTPS, not as an open public API."""
import hashlib
import asyncio
import json
import tempfile
from pathlib import Path
import hmac
import os
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import StreamingResponse, Response
from pydantic import BaseModel
from telethon import TelegramClient, utils
from telethon.tl.types import Channel, DocumentAttributeAudio, DocumentAttributeFilename

API_ID = int(os.environ.get('TELEGRAM_API_ID', '0'))
API_HASH = os.environ.get('TELEGRAM_API_HASH', '')
ACCESS_TOKEN = os.environ.get('TELETUNE_ACCESS_TOKEN', '')
SIGNING_KEY = os.environ.get('TELETUNE_SIGNING_KEY', '')
SESSION = os.environ.get('TELEGRAM_SESSION', '/data/teletune')
PUBLIC_URL = os.environ.get('TELETUNE_PUBLIC_URL', '').rstrip('/')
if not (API_ID and API_HASH and len(ACCESS_TOKEN) >= 32 and len(SIGNING_KEY) >= 32 and PUBLIC_URL.startswith('https://')):
    raise RuntimeError('Set TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION, TELETUNE_ACCESS_TOKEN (32+), TELETUNE_SIGNING_KEY (32+) and HTTPS TELETUNE_PUBLIC_URL')
client = TelegramClient(SESSION, API_ID, API_HASH)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError('Telegram not logged in. Run login.py first using the same persistent /data volume.')
    try:
        yield
    finally:
        await client.disconnect()

app = FastAPI(title='H2T Melody Gateway', docs_url=None, redoc_url=None, lifespan=lifespan)


@app.middleware('http')
async def prevent_sensitive_api_caching(request: Request, call_next):
    # Channel lists, metadata and signed playback URLs should not be cached by proxies.
    response = await call_next(request)
    if request.url.path.startswith('/v1/'):
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['Pragma'] = 'no-cache'
    return response


def authorized(authorization: str | None = Header(default=None)):
    if not authorization or not hmac.compare_digest(authorization, 'Bearer ' + ACCESS_TOKEN):
        raise HTTPException(status_code=401, detail='Unauthorized')


def signed_path(channel_id: int, message_id: int, expiry: int) -> str:
    path = f'/v1/stream/{channel_id}/{message_id}'
    msg = f'{path}:{expiry}'.encode()
    sig = hmac.new(SIGNING_KEY.encode(), msg, hashlib.sha256).hexdigest()
    return f'{PUBLIC_URL}{path}?expires={expiry}&sig={sig}'


def validate_signature(channel_id: int, message_id: int, expires: int, sig: str):
    if expires < time.time() or expires > time.time() + 86401:
        raise HTTPException(status_code=403, detail='Expired stream URL')
    expected = signed_path(channel_id, message_id, expires).split('&sig=')[-1]
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=403, detail='Invalid signature')


AUDIO_MIME_BY_EXTENSION = {
    '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.aac': 'audio/aac',
    '.flac': 'audio/flac', '.wav': 'audio/wav', '.ogg': 'audio/ogg',
    '.opus': 'audio/ogg', '.wma': 'audio/x-ms-wma', '.aiff': 'audio/aiff',
    '.alac': 'audio/mp4',
}
AUDIO_EXTENSIONS = frozenset(AUDIO_MIME_BY_EXTENSION)


def audio_info(message):
    """Recognize Telegram audio and audio files sent as generic documents.

    DocumentAttributeAudio is not always present when an MP3 is uploaded as a file.
    Reject videos and unrecognized arbitrary documents.
    """
    if not message or not getattr(message, 'document', None):
        return None
    document = message.document
    attributes = document.attributes or []
    audio = next((a for a in attributes if isinstance(a, DocumentAttributeAudio)), None)
    filename_attribute = next((a for a in attributes if isinstance(a, DocumentAttributeFilename)), None)
    name = (getattr(filename_attribute, 'file_name', None)
            or getattr(getattr(message, 'file', None), 'name', None)
            or f'Audio {message.id}')
    mime = (document.mime_type or 'application/octet-stream').lower().split(';', 1)[0].strip()
    extension = os.path.splitext(name)[1].lower()
    # A file explicitly marked video is never audio, even if its filename is misleading.
    if mime.startswith('video/'):
        return None
    if audio is None and not (mime.startswith('audio/') or
                              (mime in ('application/octet-stream', 'application/x-octet-stream')
                               and extension in AUDIO_EXTENSIONS)):
        return None
    return {
        'id': f'tg:{message.chat_id}:{message.id}',
        'channel_id': message.chat_id,
        'message_id': message.id,
        'title': (getattr(audio, 'title', None) if audio else None) or os.path.splitext(name)[0],
        'artist': (getattr(audio, 'performer', None) if audio else None) or 'Telegram',
        'duration_seconds': (getattr(audio, 'duration', 0) if audio else 0) or 0,
        'size_bytes': document.size,
        'mime_type': mime if mime.startswith('audio/') else AUDIO_MIME_BY_EXTENSION.get(extension, 'application/octet-stream'),
        'filename': name,
    }


async def get_audio_message(channel_id: int, message_id: int):
    try:
        entity = await client.get_entity(channel_id)
        msg = await client.get_messages(entity, ids=message_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail='Channel not accessible')
    if not audio_info(msg):
        raise HTTPException(status_code=404, detail='Audio not found')
    return msg


@app.get('/v1/health', dependencies=[Depends(authorized)])
async def health():
    return {'status': 'ok', 'telegram_authorized': await client.is_user_authorized()}


@app.get('/v1/channels', dependencies=[Depends(authorized)])
async def channels():
    dialogs = await client.get_dialogs(limit=500)
    return {'channels': [
        {'id': d.id, 'title': d.name, 'username': getattr(d.entity, 'username', None)}
        for d in dialogs if isinstance(d.entity, Channel)
    ]}


class ResolveRequest(BaseModel):
    link: str


@app.post('/v1/channels/resolve', dependencies=[Depends(authorized)])
async def resolve_channel(body: ResolveRequest):
    from urllib.parse import urlparse
    url = urlparse(body.link.strip())
    if url.scheme != 'https' or url.hostname not in ('t.me', 'telegram.me') or url.port or url.username or url.password:
        raise HTTPException(400, 'Invalid Telegram link')
    username = url.path.strip('/')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}', username):
        raise HTTPException(400, 'Only public Telegram channel links supported. Private channels must be joined in Telegram first.')
    try:
        entity = await client.get_entity(username)
    except (ValueError, TypeError):
        raise HTTPException(404, 'Channel not found or inaccessible')
    if not isinstance(entity, Channel):
        raise HTTPException(400, 'Not a channel')
    return {'id': utils.get_peer_id(entity), 'title': entity.title, 'username': entity.username}


@app.get('/v1/channels/{channel_id}/tracks', dependencies=[Depends(authorized)])
async def channel_tracks(channel_id: int, limit: int = 200, before_id: int = 0):
    if not 1 <= limit <= 500:
        raise HTTPException(400, 'limit must be between 1 and 500')
    if before_id < 0:
        raise HTTPException(400, 'before_id must be non-negative')
    try:
        entity = await client.get_entity(channel_id)
    except (ValueError, TypeError):
        raise HTTPException(404, 'Channel not accessible')
    results = []
    last_id = 0
    # Iterate messages rather than only audio search: Telegram may store MP3 as a document.
    async for message in client.iter_messages(entity, limit=limit, offset_id=before_id):
        last_id = message.id
        track = audio_info(message)
        if track is not None:
            results.append(track)
    return {'tracks': results, 'next_before_id': last_id if last_id else None}


@app.get('/v1/tracks/{channel_id}/{message_id}/play', dependencies=[Depends(authorized)])
async def playable(channel_id: int, message_id: int):
    info = audio_info(await get_audio_message(channel_id, message_id))
    expires = int(time.time()) + 3600
    return {'track': info, 'url': signed_path(channel_id, message_id, expires), 'expires': expires}


def parse_byte_range(raw_range: str | None, size: int) -> tuple[int, int, bool]:
    """Parse single RFC 7233 byte range. Invalid/unsatisfiable values return 416."""
    if size <= 0:
        raise HTTPException(416, 'Empty file', headers={'Content-Range': f'bytes */{size}'})
    if not raw_range:
        return 0, size - 1, False
    m = re.fullmatch(r'bytes=(\d*)-(\d*)', raw_range.strip())
    if not m or (not m.group(1) and not m.group(2)):
        raise HTTPException(416, 'Unsupported range', headers={'Content-Range': f'bytes */{size}'})
    left, right = m.groups()
    # Bound untrusted decimal input before int() conversion (Python limits digits).
    # A byte offset larger than 64-bit cannot address a Telegram document.
    if len(left) > 20 or len(right) > 20:
        raise HTTPException(416, 'Range exceeds supported offset', headers={'Content-Range': f'bytes */{size}'})
    if left:
        start = int(left)
        end = min(int(right), size - 1) if right else size - 1
    else:
        suffix = int(right)
        if suffix == 0:
            raise HTTPException(416, 'Invalid suffix range', headers={'Content-Range': f'bytes */{size}'})
        start = max(size - suffix, 0)
        end = size - 1
    if start >= size or end < start:
        raise HTTPException(416, 'Range not satisfiable', headers={'Content-Range': f'bytes */{size}'})
    return start, end, True


@app.api_route('/v1/stream/{channel_id}/{message_id}', methods=['GET', 'HEAD'])
async def stream(channel_id: int, message_id: int, expires: int, sig: str, request: Request):
    validate_signature(channel_id, message_id, expires, sig)
    msg = await get_audio_message(channel_id, message_id)
    size = msg.document.size
    start, end, partial = parse_byte_range(request.headers.get('range'), size)

    async def generate():
        remaining = end - start + 1
        # Telethon iter_download accepts offset; clip the final chunk for precise byte ranges.
        async for chunk in client.iter_download(msg.media, offset=start, chunk_size=256 * 1024):
            if remaining <= 0:
                break
            data = bytes(chunk[:remaining])
            remaining -= len(data)
            if data:
                yield data
            if remaining == 0:
                break

    headers = {
        'Accept-Ranges': 'bytes',
        'Content-Length': str(end - start + 1),
        'Cache-Control': 'private, no-store',
        'X-Content-Type-Options': 'nosniff',
    }
    if partial:
        headers['Content-Range'] = f'bytes {start}-{end}/{size}'
    mime_type = audio_info(msg)['mime_type']
    if request.method == 'HEAD':
        return Response(status_code=206 if partial else 200, media_type=mime_type, headers=headers)
    return StreamingResponse(generate(), status_code=206 if partial else 200,
                             media_type=mime_type, headers=headers)


# Single-owner playlist snapshot sync. Files are stored in the existing persistent /data volume.
# API uses optimistic concurrency to prevent two devices overwriting each other silently.
SYNC_FILE = Path(os.environ.get('TELETUNE_PLAYLIST_FILE', '/data/playlists.json'))
_sync_lock = asyncio.Lock()

class SyncedTrack(BaseModel):
    id: str
    title: str
    artist: str
    channel_id: int
    message_id: int

class SyncedPlaylist(BaseModel):
    name: str
    tracks: list[SyncedTrack]

class PlaylistUpdate(BaseModel):
    revision: int
    playlists: list[SyncedPlaylist]


def _read_playlists() -> dict:
    if not SYNC_FILE.is_file():
        return {'revision': 0, 'playlists': []}
    return json.loads(SYNC_FILE.read_text(encoding='utf-8'))


def _validate_playlist_snapshot(data: PlaylistUpdate) -> None:
    if len(data.playlists) > 100:
        raise HTTPException(400, 'Too many playlists')
    names = set()
    for playlist in data.playlists:
        if not 1 <= len(playlist.name.strip()) <= 70 or playlist.name in names:
            raise HTTPException(400, 'Invalid or duplicate playlist name')
        names.add(playlist.name)
        if len(playlist.tracks) > 5000:
            raise HTTPException(400, 'Too many tracks')
        seen = set()
        for track in playlist.tracks:
            if (track.id != f'tg:{track.channel_id}:{track.message_id}'
                    or len(track.title) > 500 or len(track.artist) > 500
                    or track.id in seen):
                raise HTTPException(400, 'Invalid or duplicate Telegram track')
            seen.add(track.id)


@app.get('/v1/playlists', dependencies=[Depends(authorized)])
async def get_playlists():
    async with _sync_lock:
        return _read_playlists()


@app.put('/v1/playlists', dependencies=[Depends(authorized)])
async def put_playlists(data: PlaylistUpdate):
    _validate_playlist_snapshot(data)
    async with _sync_lock:
        current = _read_playlists()
        if data.revision != current['revision']:
            raise HTTPException(409, 'Playlist revision conflict: download cloud playlists before uploading')
        updated = {'revision': current['revision'] + 1,
                   'playlists': [p.model_dump() for p in data.playlists]}
        SYNC_FILE.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replace keeps snapshots readable if the server stops during a write.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=SYNC_FILE.parent,
                                         prefix='.playlists-', delete=False) as handle:
            temp = Path(handle.name)
            try:
                json.dump(updated, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                temp.unlink(missing_ok=True)
                raise
        os.replace(temp, SYNC_FILE)
        return updated
