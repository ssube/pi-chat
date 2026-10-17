#!/usr/bin/env python3
"""Canonical Matrix room tools and bridge-routed agent operations."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import room_history as reader
import message_agent as sender

DEFAULT_CONFIG = Path.home() / '.config/pi-chat/matrix.json'
SETTINGS = {'profile_file', 'identity_file', 'contacts_file'}


def parser():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    for name in SETTINGS:
        cli.add_argument('--' + name.replace('_', '-'), type=Path)
    commands = cli.add_subparsers(dest='operation', required=True)
    commands.add_parser('contacts')
    commands.add_parser('rooms')
    for name in ('send', 'check', 'history', 'wait', 'members', 'event', 'attach', 'upload', 'batch', 'react', 'reactions'):
        command = commands.add_parser(name)
        command.add_argument('room', nargs='?', help='explicit enabled Matrix room ID or full room alias')
        command.add_argument('--contact', help='name from the owner-only contact mapping')
        if name in ('send', 'check', 'attach', 'upload', 'batch'):
            command.add_argument('mention', nargs='?', help='optional exact Matrix user ID')
        if name == 'send':
            source = command.add_mutually_exclusive_group(required=True)
            source.add_argument('--message-file', type=Path)
            source.add_argument('--stdin', action='store_true')
        if name in ('event', 'react', 'reactions'):
            command.add_argument('--event-id', required=True)
        if name == 'react':
            command.add_argument('--key', '--emoji', required=True)
        if name in ('attach', 'upload', 'batch'):
            command.add_argument('--files', type=Path, nargs='+' if name == 'batch' else 1, required=True)
            command.add_argument('--caption', default='')
            command.add_argument('--caption-before-file', action='store_true')
            command.add_argument('--interval', '--delay', type=float, default=1)
        if name == 'reactions':
            command.add_argument('--pages', type=int, default=2)
            command.add_argument('--limit', type=int, default=20)
        if name in ('history', 'wait'):
            for flag in ('full-body', 'metadata-only', 'json'):
                command.add_argument('--' + flag, action='store_true')
            command.add_argument('--sender')
            command.add_argument('--skip-progress', action='store_true')
            if name == 'history':
                command.add_argument('--since')
                command.add_argument('--contains')
                command.add_argument('--limit', type=int, default=20)
                command.add_argument('--pages', type=int, default=2)
                command.add_argument('--watch', type=int)
            else:
                command.add_argument('--marker', required=True)
                command.add_argument('--timeout', type=int, default=60)
                command.add_argument('--interval', type=int, default=5)
    return cli


def configuration(args):
    data = {}
    if args.config.exists() or args.config.is_symlink() or args.config != DEFAULT_CONFIG:
        data = reader.private_json(args.config)
    if set(data) - SETTINGS:
        raise ValueError('Matrix settings contain paths only; unknown setting')
    for name in SETTINGS:
        if getattr(args, name):
            data[name] = str(getattr(args, name))
    if any(not isinstance(value, str) or not Path(value).is_absolute() for value in data.values()):
        raise ValueError('Matrix configuration paths must be absolute')
    if 'profile_file' not in data:
        raise ValueError('Matrix needs the owner bridge profile path')
    return data


def reactions(url, token, room, event_id, pages, limit):
    if not event_id.startswith('$') or not 1 <= pages <= 5 or not 1 <= limit <= 100:
        raise ValueError('reactions need an exact event and bounded pages/limit')
    base = '/_matrix/client/v3/rooms/' + urllib.parse.quote(room, safe='')
    target = reader.get(url, token, base + '/event/' + urllib.parse.quote(event_id, safe=''))
    if target.get('event_id') != event_id or target.get('room_id') != room:
        raise ValueError('reaction target does not match selected room/event')
    path = ('/_matrix/client/v1/rooms/' + urllib.parse.quote(room, safe='') +
            '/relations/' + urllib.parse.quote(event_id, safe='') + '/m.annotation/m.reaction')
    events, seen, tokens = [], set(), set()
    cursor = None
    for _ in range(pages):
        params = {'limit': limit, 'dir': 'b'}
        if cursor:
            params['from'] = cursor
        try:
            page = reader.get(url, token, path + '?' + urllib.parse.urlencode(params))
        except RuntimeError as error:
            if not tokens and str(error) in ('Matrix request failed (HTTP 404)', 'Matrix request failed (HTTP 405)'):
                return reaction_history(url, token, room, event_id, pages, limit)
            raise
        chunk = page.get('chunk', [])
        if not isinstance(chunk, list) or len(chunk) > limit:
            raise ValueError('invalid or oversized reaction page')
        for event in chunk:
            if not isinstance(event, dict) or not isinstance(event.get('content'), dict):
                raise ValueError('invalid reaction event')
            relation = event['content'].get('m.relates_to', {})
            if not isinstance(relation, dict):
                raise ValueError('invalid reaction relation')
            identifier = event.get('event_id')
            if (event.get('type') != 'm.reaction' or event.get('room_id', room) != room or
                    relation.get('rel_type') != 'm.annotation' or relation.get('event_id') != event_id or
                    not isinstance(identifier, str) or not identifier.startswith('$')):
                raise ValueError('reaction page contains unexpected event provenance')
            if identifier not in seen:
                events.append(event)
                seen.add(identifier)
        cursor = page.get('next_batch')
        if not cursor:
            break
        if not isinstance(cursor, str) or cursor in tokens:
            raise ValueError('reaction pagination did not advance')
        tokens.add(cursor)
    return {'events': events, 'next_batch': cursor, 'limited': bool(cursor), 'source': 'relations'}


def reaction_history(url, token, room, event_id, pages, limit):
    """Bounded fallback for servers without the relations endpoint; never a total."""
    path = '/_matrix/client/v3/rooms/' + urllib.parse.quote(room, safe='') + '/messages'
    events, seen, tokens = [], set(), set()
    cursor = None
    for _ in range(pages):
        params = {'dir': 'b', 'limit': limit}
        if cursor:
            params['from'] = cursor
        page = reader.get(url, token, path, params)
        chunk = page.get('chunk', [])
        if not isinstance(chunk, list) or len(chunk) > limit:
            raise ValueError('invalid or oversized reaction history page')
        for event in chunk:
            if not isinstance(event, dict) or event.get('room_id', room) != room:
                raise ValueError('invalid reaction history event')
            content = event.get('content', {})
            relation = content.get('m.relates_to', {}) if isinstance(content, dict) else {}
            if (event.get('type') == 'm.reaction' and isinstance(relation, dict) and
                    relation.get('rel_type') == 'm.annotation' and relation.get('event_id') == event_id):
                identifier = event.get('event_id')
                if not isinstance(identifier, str) or not identifier.startswith('$'):
                    raise ValueError('invalid reaction history identifier')
                if identifier not in seen:
                    events.append(event)
                    seen.add(identifier)
        cursor = page.get('end')
        if not cursor:
            break
        if not isinstance(cursor, str) or cursor in tokens:
            raise ValueError('reaction history pagination did not advance')
        tokens.add(cursor)
    return {'events': events, 'next_batch': cursor, 'limited': bool(cursor), 'source': 'history'}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        config = configuration(args)
        profile_path = Path(config['profile_file'])
        if args.operation == 'contacts':
            contacts = reader.private_json(Path(config.get('contacts_file', Path.home() / '.config/pi-chat/contacts.json')))
            for name in sorted(contacts):
                sender.contact_route(Path(config.get('contacts_file', Path.home() / '.config/pi-chat/contacts.json')), name)
                print(json.dumps({'contact': name}))
            return 0
        identity = Path(config['identity_file']) if 'identity_file' in config else None
        url, user, token, rooms = reader.credentials(profile_path, identity)
        if args.operation == 'rooms':
            if reader.get(url, token, '/_matrix/client/v3/account/whoami').get('user_id') != user:
                raise ValueError('Matrix token does not match the configured owner')
            joined = reader.get(url, token, '/_matrix/client/v3/joined_rooms').get('joined_rooms', [])
            print(json.dumps(sorted(rooms.intersection(joined))))
            return 0
        if bool(args.room) == bool(args.contact):
            raise ValueError('supply either an enabled room or a contact name, never both')
        peer = None
        room = args.room
        if args.contact:
            room, peer = sender.contact_route(Path(config.get('contacts_file', Path.home() / '.config/pi-chat/contacts.json')), args.contact)
        if room and room.startswith('#'):
            if ':' not in room or reader.get(url, token, '/_matrix/client/v3/account/whoami').get('user_id') != user:
                raise ValueError('alias resolution requires the actual configured owner')
            alias = '/_matrix/client/v3/directory/room/' + urllib.parse.quote(room, safe='')
            room = reader.get(url, token, alias).get('room_id')
        if not isinstance(room, str) or room not in rooms:
            raise ValueError('room is not enabled in this owner bridge profile')
        if args.operation in ('send', 'check', 'attach', 'upload', 'batch', 'react'):
            if args.contact and getattr(args, 'mention', None):
                raise ValueError('contact name already selects the exact mention')
            mention = peer or getattr(args, 'mention', None)
            # Writes always use the configured profile's own token environment,
            # independently of any read-only identity file.
            media_write = args.operation in ('attach', 'upload', 'batch', 'react')
            writer = Path(__file__).with_name('matrix_operations.py' if media_write else 'message_agent.py')
            command = [sys.executable, str(writer)]
            if media_write:
                command.append('attach' if args.operation == 'upload' else args.operation)
            command += ['--profile-file', str(profile_path), '--room-id', room]
            if not args.contact:
                command.append('--general')
            if mention:
                command += ['--to', mention]
            if media_write:
                if args.operation == 'react':
                    command += ['--event-id', args.event_id, '--key', args.key]
                else:
                    command += ['--files', *(str(path) for path in args.files),
                                '--caption', args.caption, '--interval', str(args.interval)]
                    if args.caption_before_file:
                        command.append('--caption-before-file')
                return subprocess.run(command, check=False).returncode
            if args.operation == 'check':
                return subprocess.run([*command, '--check'], check=False).returncode
            if args.message_file:
                return subprocess.run([*command, '--message-file', str(args.message_file)], check=False).returncode
            raw = sys.stdin.buffer.read(32002)
            message = raw.decode('utf-8').strip()
            if len(raw) > 32001 or not message or len(message) > 8000:
                raise ValueError('message must be bounded nonempty UTF-8 text, at most 8000 characters')
            with tempfile.TemporaryDirectory(prefix='matrix-message-') as directory:
                path = Path(directory) / 'message.txt'
                with open(path, 'x', encoding='utf-8', opener=lambda p, f: os.open(p, f, 0o600)) as stream:
                    stream.write(message)
                return subprocess.run([*command, '--message-file', str(path)], check=False).returncode
        if args.operation in ('history', 'wait'):
            command = ['--profile-file', str(profile_path), '--room-id', room]
            if identity is not None:
                command += ['--identity-file', str(identity)]
            if args.operation == 'wait':
                wait_sender = peer or args.sender
                if peer and args.sender and args.sender != peer:
                    raise ValueError('contact wait sender must match the selected contact')
                if not wait_sender:
                    raise ValueError('room wait needs an exact --sender')
                command += ['--sender', wait_sender, '--wait-for', args.marker,
                            '--timeout', str(args.timeout), '--interval', str(args.interval)]
            else:
                command += ['--limit', str(args.limit), '--pages', str(args.pages)]
                for name in ('sender', 'since', 'contains', 'watch'):
                    if getattr(args, name) is not None:
                        command += ['--' + name, str(getattr(args, name))]
            for name in ('full_body', 'metadata_only', 'json', 'skip_progress'):
                if getattr(args, name):
                    command.append('--' + name.replace('_', '-'))
            return reader.main(command)
        if reader.get(url, token, '/_matrix/client/v3/account/whoami').get('user_id') != user:
            raise ValueError('Matrix token does not match the configured owner')
        if args.operation == 'reactions':
            print(json.dumps(reactions(url, token, room, args.event_id, args.pages, args.limit), ensure_ascii=True))
            return 0
        base = '/_matrix/client/v3/rooms/' + urllib.parse.quote(room, safe='')
        path = (base + '/joined_members' if args.operation == 'members' else
                base + '/event/' + urllib.parse.quote(args.event_id, safe=''))
        print(json.dumps(reader.get(url, token, path), ensure_ascii=True))
        return 0
    except (OSError, UnicodeError, ValueError, RuntimeError):
        # Configuration/HTTP failures may carry private paths or raw data.
        print('matrix: configuration, route, identity or maintained client refused; '
              'check owner paths and approved bridge route', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
