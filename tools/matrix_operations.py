#!/usr/bin/env python3
"""Canonical bounded media/reaction writes using the maintained sender guards."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import stat
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import message_agent as sender

MAX_BYTES = 32 * 1024 * 1024
MAX_BATCH_BYTES = 64 * 1024 * 1024


def media(path: Path):
    # Open once and check the opened object; never read an unbounded device/FIFO.
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_BYTES:
            raise ValueError('media must be a regular file containing 1 to 33554432 bytes')
        raw = stream.read(MAX_BYTES + 1)
        if not raw or len(raw) > MAX_BYTES:
            raise ValueError('media changed or exceeded its byte bound')
    mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
    kind = mime.split('/')[0]
    content = {'msgtype': 'm.' + kind if kind in {'image', 'audio', 'video'} else 'm.file',
               'body': path.name, 'filename': path.name, 'info': {'mimetype': mime, 'size': len(raw)}}
    return raw, content


def unencrypted(room):
    from matrix_client import MatrixError
    path = '/_matrix/client/v3/rooms/' + urllib.parse.quote(room.room_id, safe='') + '/state/m.room.encryption'
    try:
        room.request('GET', path)
    except MatrixError as error:
        if str(error) == 'Matrix HTTP 404':
            return
        raise
    raise ValueError('canonical media/reactions support only unencrypted rooms')


def target_event(room, event_id):
    event = room.event(event_id)
    if event.get('event_id') != event_id or event.get('room_id') != room.room_id:
        raise ValueError('reaction target does not match the selected room and event')


def run(args):
    room = sender.verified_room(args.profile_file, args.room_id, args.to)
    unencrypted(room)
    if args.operation == 'react':
        if not args.event_id.startswith('$') or not args.key or len(args.key) > 128:
            raise ValueError('reaction needs an exact event ID and a bounded nonempty key')
        target_event(room, args.event_id)
        content = {'m.relates_to': {'rel_type': 'm.annotation', 'event_id': args.event_id, 'key': args.key}}
        event_id = sender.guarded_write(room, lambda: room.send_event('m.reaction', content))
        print(json.dumps({'index': 0, 'event_id': event_id}), flush=True)
        return 0
    if not 1 <= len(args.files) <= 20 or not 0 <= args.interval <= 5:
        raise ValueError('batch requires 1 to 20 files and an interval from 0 to 5 seconds')
    prepared = []
    total = 0
    # Complete bounded local validation before the first upload/write.
    for path in args.files:
        raw, content = media(path)
        total += len(raw)
        if total > MAX_BATCH_BYTES:
            raise ValueError('batch media exceeds 67108864 bytes')
        if args.caption and not getattr(args, 'caption_before_file', False):
            content['body'] = args.caption
        if args.to:
            content['m.mentions'] = {'user_ids': [args.to]}
        prepared.append((raw, content))
    if args.caption and getattr(args, 'caption_before_file', False):
        message = args.caption
        if args.to and not message.startswith(args.to):
            message = args.to + ' ' + message
        try:
            event_id = sender.guarded_write(room, lambda: room.send(message, args.to))
        except (OSError, ValueError, RuntimeError):
            print(json.dumps({'phase': 'caption', 'status': 'failed_or_uncertain', 'completed': 0,
                              'retry': 'inspect room before retry'}), flush=True)
            return 1
        print(json.dumps({'phase': 'caption', 'event_id': event_id}), flush=True)
    for index, (raw, content) in enumerate(prepared):
        # Acquire/renew before each item; a batch never outlives its safe lease
        # simply because previous uploads or 429 backoff consumed time.
        def write():
            content['url'] = room.upload(raw, content['filename'], content['info']['mimetype'])
            # Recheck/renew after upload/backoff before a reply-producing PUT.
            return sender.guarded_write(room, lambda: room.send_event('m.room.message', content))
        try:
            event_id = sender.guarded_write(room, write)
        except (OSError, ValueError, RuntimeError):
            # A failed PUT may have committed. Preserve previous receipts and
            # stop; never replay successful or uncertain batch items.
            print(json.dumps({'index': index, 'status': 'failed_or_uncertain',
                              'completed': index, 'retry': 'inspect room before retry'}), flush=True)
            return 1
        print(json.dumps({'index': index, 'event_id': event_id}), flush=True)
        if index + 1 < len(prepared):
            time.sleep(args.interval)
    return 0


def main(argv=None):
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('operation', choices=('attach', 'batch', 'react'))
    cli.add_argument('--room-id', required=True)
    cli.add_argument('--profile-file', type=Path, required=True)
    cli.add_argument('--to')
    cli.add_argument('--general', action='store_true')
    cli.add_argument('--files', type=Path, nargs='+', default=[])
    cli.add_argument('--caption', default='')
    cli.add_argument('--caption-before-file', action='store_true')
    cli.add_argument('--interval', type=float, default=1)
    cli.add_argument('--event-id', default='')
    cli.add_argument('--key', default='')
    args = cli.parse_args(argv)
    if (not args.room_id.startswith('!') or ':' not in args.room_id or
            args.to is not None and (not args.to.startswith('@') or ':' not in args.to) or
            not args.general and not args.to or len(args.caption) > 8000 or
            args.operation == 'attach' and len(args.files) != 1):
        cli.error('invalid room, mention or bounded operation arguments')
    try:
        return run(args)
    except (OSError, ValueError, RuntimeError):
        print('matrix operation refused; check owner identity, room, lease and input bounds', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
