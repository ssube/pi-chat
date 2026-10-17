"""Small Matrix client shared by the standalone sender."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any


class MatrixError(RuntimeError):
    pass


class MatrixClient:
    def __init__(self, homeserver: str, user_id: str, token: str, room_id: str):
        self.homeserver, self.user_id, self.token, self.room_id = homeserver, user_id, token, room_id
        self.before_write = None

    def request(
        self, method: str, path: str, *, data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None, timeout: int = 35,
        raw_data: bytes | None = None, content_type: str = "application/json",
    ) -> dict[str, Any]:
        url = self.homeserver.rstrip("/") + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        if data is not None and raw_data is not None:
            raise MatrixError("Matrix request cannot combine JSON and binary bodies")
        raw = raw_data if raw_data is not None else (None if data is None else json.dumps(data).encode("utf-8"))
        request = urllib.request.Request(
            url, data=raw, method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": content_type,
            },
        )
        for attempt in range(4):
            if method in {"POST", "PUT"} and self.before_write is not None:
                self.before_write()
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code == 429 and attempt < 3:
                    try:
                        payload = json.load(error)
                        milliseconds = payload.get("retry_after_ms") if isinstance(payload, dict) else None
                        delay = milliseconds / 1000 if type(milliseconds) is int and milliseconds >= 0 else 1
                    except ValueError:
                        delay = 1
                    time.sleep(min(max(delay, 0.1), 30))
                    continue
                if error.code in {500, 502, 503, 504} and method in {"GET", "PUT"} and attempt < 3:
                    time.sleep(min(2 ** attempt, 10))
                    continue
                raise MatrixError(f"Matrix HTTP {error.code}") from None
            except (urllib.error.URLError, TimeoutError):
                raise MatrixError('Matrix network/timeout failure') from None
        raise MatrixError("Matrix rate limit retry exhausted")

    def whoami(self) -> str:
        return self.request('GET', '/_matrix/client/v3/account/whoami')['user_id']

    def joined_members(self) -> dict:
        room = urllib.parse.quote(self.room_id, safe='')
        return self.request('GET', f'/_matrix/client/v3/rooms/{room}/joined_members')['joined']

    def event(self, event_id: str) -> dict:
        room = urllib.parse.quote(self.room_id, safe='')
        event = urllib.parse.quote(event_id, safe='')
        return self.request('GET', f'/_matrix/client/v3/rooms/{room}/event/{event}')

    def send_event(self, event_type: str, content: dict) -> str:
        room = urllib.parse.quote(self.room_id, safe='')
        kind = urllib.parse.quote(event_type, safe='')
        return self.request('PUT', f'/_matrix/client/v3/rooms/{room}/send/{kind}/{uuid.uuid4()}', data=content)['event_id']

    def send(self, message: str, recipient: str | None = None) -> str:
        return self.send_event('m.room.message', {'msgtype': 'm.text', 'body': message,
            'm.mentions': {'user_ids': [recipient] if recipient else [], 'room': False}})

    def upload(self, raw: bytes, filename: str, mimetype: str) -> str:
        # POST uploads are not replayed on uncertain server or network failures.
        return self.request('POST', '/_matrix/media/v3/upload', params={'filename': filename},
                            raw_data=raw, content_type=mimetype)['content_uri']
