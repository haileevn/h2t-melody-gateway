"""Validate release metadata for the opt-in mobile update flow.

No release file is downloadable through this API; only trusted HTTPS URLs
are advertised. The operator publishes artifacts and hashes separately.
"""
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

MAX_MANIFEST_BYTES = 32 * 1024
SHA256_RE = re.compile(r'^[0-9a-fA-F]{64}$')
VERSION_RE = re.compile(r'^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?$')


def latest_release(path: str, platform: str) -> dict:
    if platform not in ('android', 'ios'):
        raise ValueError('Unsupported platform')
    release_path = Path(path)
    if not release_path.exists():
        return {'available': False}
    if not release_path.is_file() or release_path.stat().st_size > MAX_MANIFEST_BYTES:
        raise ValueError('Release manifest is too large or invalid')
    with release_path.open('r', encoding='utf-8') as handle:
        root = json.load(handle)
    item = root.get(platform)
    if not item:
        return {'available': False}
    if not isinstance(item, dict):
        raise ValueError('Invalid platform release')
    version = item.get('version')
    build = item.get('build')
    url = item.get('url')
    sha = item.get('sha256')
    notes = item.get('notes', '')
    required = item.get('mandatory', False)
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise ValueError('Invalid version')
    if type(build) is not int or not (1 <= build <= 2147483647):
        raise ValueError('Invalid build number')
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError('Invalid release URL')
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise ValueError('Release URL must be HTTPS without credentials or fragment')
    if not isinstance(sha, str) or not SHA256_RE.fullmatch(sha):
        raise ValueError('Invalid SHA-256')
    if not isinstance(notes, str) or len(notes) > 4096:
        raise ValueError('Invalid release notes')
    if type(required) is not bool:
        raise ValueError('Invalid mandatory flag')
    return {'available': True, 'version': version, 'build': build, 'url': url,
            'sha256': sha.lower(), 'notes': notes, 'mandatory': required}
