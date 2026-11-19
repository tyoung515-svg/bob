"""Conditional read responses, computed only after normal authorization."""
import hashlib
import json
from aiohttp import web


def conditional_json(request, value):
    body = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    etag = '"' + hashlib.sha256(body).hexdigest() + '"'
    headers = {'ETag':etag, 'Cache-Control':'private, no-cache', 'Vary':'Authorization'}
    matches = [v.strip().removeprefix('W/') for v in request.headers.get('If-None-Match', '').split(',')]
    if request.method in {'GET', 'HEAD'} and (etag in matches or '*' in matches):
        return web.Response(status=304, headers=headers)
    return web.Response(body=body, content_type='application/json', headers=headers)
