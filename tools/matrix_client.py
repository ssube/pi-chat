"""Small Matrix client shared by the standalone sender."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


class MatrixError(RuntimeError):
    pass


class MatrixClient:
    def __init__(self, homeserver: str, user_id: str, token: str, room_id: str):
        self.homeserver, self.user_id, self.token, self.room_id = homeserver, user_id, token, room_id

    def request(self, method: str, path: str, data: dict | None = None) -> dict:
        request = urllib.request.Request(self.homeserver.rstrip('/') + path, method=method,
            data=None if data is None else json.dumps(data).encode(),
            headers={'Authorization': f'Bearer {self.token}', 'Content-Type': 'application/json'})
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=35) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code == 429 and attempt < 3:
                    try:
                        delay = float(json.load(error).get('retry_after_ms', 1000)) / 1000
                    except (ValueError, TypeError):
                        delay = 1
                    time.sleep(min(max(delay, .1), 30))
                    continue
                raise MatrixError(f'Matrix HTTP {error.code}') from None
            except (urllib.error.URLError, TimeoutError):
                raise MatrixError('Matrix network/timeout failure') from None
        raise MatrixError('Matrix rate limit exhausted')

    def whoami(self) -> str:
        return self.request('GET', '/_matrix/client/v3/account/whoami')['user_id']

    def joined_members(self) -> dict:
        room = urllib.parse.quote(self.room_id, safe='')
        return self.request('GET', f'/_matrix/client/v3/rooms/{room}/joined_members')['joined']

    def send(self, message: str, recipient: str) -> str:
        room = urllib.parse.quote(self.room_id, safe='')
        return self.request('PUT', f'/_matrix/client/v3/rooms/{room}/send/m.room.message/{uuid.uuid4()}',
            {'msgtype': 'm.text', 'body': message, 'm.mentions': {'user_ids': [recipient], 'room': False}})['event_id']
