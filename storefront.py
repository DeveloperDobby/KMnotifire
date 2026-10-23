"""Storefront connection settings and protocol field names."""
from pathlib import Path
import json

_CONFIG_PATH = Path(__file__).resolve().parent / 'config' / 'monitor.json'
_SETTINGS = json.loads(_CONFIG_PATH.read_text(encoding='utf-8'))['storefront']

STOREFRONT_HOST = _SETTINGS['host']
PUBLIC_ORIGIN = f'https://{STOREFRONT_HOST}'
API_ORIGIN = f'https://api.{STOREFRONT_HOST}'
PUBLIC_HOSTS = frozenset((STOREFRONT_HOST, f'www.{STOREFRONT_HOST}'))
REQUEST_HOSTS = PUBLIC_HOSTS | frozenset((f'api.{STOREFRONT_HOST}',))
API_VERSION_HEADER = _SETTINGS['headers']['api_version']
CLIENT_TIME_HEADER = _SETTINGS['headers']['client_time']
BUILD_VERSION_HEADER = _SETTINGS['headers']['build_version']
DEVICE_ID_HEADER = _SETTINGS['headers']['device_id']
WEB_REQUEST_HEADER = _SETTINGS['headers']['web_request']
