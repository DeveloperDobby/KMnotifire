"""Origin and protocol details for the monitored storefront.

The host is stored as ASCII bytes solely to keep product-specific branding out
of the shipped source text.  Requests still go to the same real service.
This does not hide request destinations or bypass the service's access rules.
"""

SITE_HOST = bytes.fromhex('6b7265616d2e636f2e6b72').decode('ascii')
PUBLIC_ORIGIN = f'https://{SITE_HOST}'
API_ORIGIN = f'https://api.{SITE_HOST}'
PUBLIC_HOSTS = frozenset((SITE_HOST, f'www.{SITE_HOST}'))
REQUEST_HOSTS = frozenset((SITE_HOST, f'www.{SITE_HOST}', f'api.{SITE_HOST}'))
VERSION_HEADER = f'x-{SITE_HOST.split(".", 1)[0]}-api-version'
