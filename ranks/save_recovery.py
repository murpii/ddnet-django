import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .graveyard import RankGraveyardError, database, rows_as_dicts, utc_now


BINLOG_TIMEOUT = 50
RESULT_LIMIT = 200


def configured_directory():
    value = getattr(settings, 'SAVE_BINLOG_DIR', None) or os.environ.get(
        'SAVE_BINLOG_DIR', '/var/lib/mysql'
    )
    directory = Path(value).resolve()
    if not directory.is_dir():
        raise RankGraveyardError('The configured MariaDB binlog directory is unavailable.')
    return directory


def configured_command():
    return getattr(settings, 'SAVE_BINLOG_COMMAND', None) or os.environ.get(
        'SAVE_BINLOG_COMMAND', 'mariadb-binlog'
    )


def list_binlogs():
    directory = configured_directory()
    names = []
    for path in directory.iterdir():
        if not re.fullmatch(r'(?:mariadb|mysql)-bin\.\d+', path.name):
            continue
        if path.is_file() and path.resolve().parent == directory:
            names.append(path.name)
    return sorted(names, key=lambda name: int(name.rsplit('.', 1)[1]))


def save_details(savegame):
    lines = savegame.splitlines()
    if not lines:
        raise RankGraveyardError('The deleted save data is malformed.')
    header = lines[0].split('\t')
    try:
        player_count = int(header[1])
    except (IndexError, ValueError) as error:
        raise RankGraveyardError('The deleted save data has no valid player count.') from error
    if player_count < 1 or len(lines) < player_count + 1:
        raise RankGraveyardError('The deleted save data has an incomplete player list.')
    player_lines = lines[1:player_count + 1]
    players = [line.split('\t', 1)[0] for line in player_lines]
    game_uuids = set()
    for line in player_lines:
        for value in line.split('\t')[1:]:
            try:
                game_uuid = str(uuid.UUID(value))
            except (ValueError, AttributeError):
                continue
            if game_uuid == value.lower():
                game_uuids.add(game_uuid)
    return players, game_uuids.pop() if len(game_uuids) == 1 else None


def payload_hash(save):
    digest = hashlib.sha256()
    for key in ('savegame', 'map_name', 'code', 'timestamp', 'server', 'ddnet7', 'save_id'):
        value = save.get(key)
        if key == 'timestamp' and value:
            value = datetime.fromisoformat(str(value))
            if timezone.is_naive(value):
                value = timezone.make_aware(value)
            value = int(value.timestamp())
        encoded = ('' if value is None else str(value)).encode('utf-8')
        digest.update(len(encoded).to_bytes(8, 'big'))
        digest.update(encoded)
    return digest.hexdigest()


def decode_value(value):
    if value == b'NULL':
        return None
    if not value.startswith(b"'"):
        return int(value.split(None, 1)[0])
    end = value.rfind(b"' /* ")
    if end < 0:
        end = value.rfind(b"'")
    if end <= 0:
        raise RankGraveyardError('A deleted save contains an unterminated value.')
    source = value[1:end]
    result = bytearray()
    index = 0
    escapes = {
        ord('0'): 0, ord('b'): 8, ord('t'): 9, ord('n'): 10,
        ord('r'): 13, ord('Z'): 26,
        ord('"'): 34, ord("'"): 39, ord('\\'): 92,
    }
    while index < len(source):
        byte = source[index]
        if byte != 92:
            result.append(byte)
            index += 1
            continue
        if index + 3 < len(source) and source[index + 1] == ord('x'):
            try:
                result.append(int(source[index + 2:index + 4], 16))
                index += 4
                continue
            except ValueError:
                pass
        index += 1
        if index >= len(source):
            result.append(92)
            break
        escaped = source[index]
        result.append(escapes.get(escaped, escaped))
        index += 1
    try:
        return result.decode('utf-8')
    except UnicodeDecodeError as error:
        raise RankGraveyardError('A deleted save contains invalid UTF-8 data.') from error


def row_value(line):
    match = re.match(rb'^###\s+@(\d+)=(.*)$', line)
    if not match:
        return None
    return int(match.group(1)), decode_value(match.group(2))


def build_save(values, deleted_at, source_file, start_position, stop_position):
    if set(values) != set(range(1, 8)):
        raise RankGraveyardError('A deleted save row does not match the record_saves schema.')
    timestamp = datetime.fromtimestamp(values[4], timezone.get_current_timezone())
    save = {
        'savegame': values[1],
        'map_name': values[2],
        'code': values[3],
        'timestamp': timestamp.isoformat(),
        'server': values[5],
        'ddnet7': bool(values[6]),
        'save_id': values[7],
        'deleted_at': deleted_at.isoformat() if deleted_at else None,
        'source_file': source_file,
        'start_position': start_position,
        'stop_position': stop_position,
    }
    try:
        save['players'], save['game_uuid'] = save_details(save['savegame'])
    except RankGraveyardError as error:
        save['players'] = []
        save['game_uuid'] = None
        save['validation_error'] = str(error)
    save['payload_hash'] = payload_hash(save)
    return save


def parse_output(lines, source_file):
    current_position = None
    table_position = None
    deleted_at = None
    row_position = None
    values = {}
    pending = []
    delete_rows = False
    end_position = None
    for raw_line in lines:
        line = raw_line.rstrip(b'\r\n')
        position_match = re.match(rb'^# at (\d+)$', line)
        if position_match:
            current_position = int(position_match.group(1))
            continue
        if b'Table_map:' in line and b'`teeworlds`.`record_saves`' in line:
            table_position = current_position
            continue
        if b'Delete_rows:' in line and table_position is not None:
            if values:
                pending.append((values, deleted_at, row_position))
                values = {}
            delete_rows = True
            row_position = table_position
            time_match = re.match(rb'^#(\d{6})\s+(\d\d:\d\d:\d\d)', line)
            if time_match:
                deleted_at = timezone.make_aware(datetime.strptime(
                    b' '.join(time_match.groups()).decode(), '%y%m%d %H:%M:%S'
                ))
            continue
        if delete_rows and line == b'### WHERE':
            if values:
                pending.append((values, deleted_at, row_position))
                values = {}
            continue
        if delete_rows:
            item = row_value(line)
            if item:
                values[item[0]] = item[1]
                continue
            end_match = re.search(rb'end_log_pos (\d+).*Xid', line)
            if end_match:
                if values:
                    pending.append((values, deleted_at, row_position))
                    values = {}
                end_position = int(end_match.group(1))
                continue
        if line == b'COMMIT/*!*/;':
            if end_position is None:
                pending = []
            else:
                for row, row_deleted_at, row_position in pending:
                    yield build_save(
                        row, row_deleted_at, source_file, row_position, end_position
                    )
            pending = []
            values = {}
            table_position = None
            deleted_at = None
            row_position = None
            delete_rows = False
            end_position = None


def run_binlog(source_files, start_at=None, stop_at=None, start_position=None,
               stop_position=None, timeout=BINLOG_TIMEOUT):
    directory = configured_directory()
    available = set(list_binlogs())
    if not source_files or any(name not in available for name in source_files):
        raise RankGraveyardError('A selected MariaDB binlog is missing or no longer retained.')
    command = [
        configured_command(), '--base64-output=DECODE-ROWS', '--verbose', '--verbose',
        '--verify-binlog-checksum', '--database=teeworlds', '--table=record_saves',
    ]
    if start_at:
        command.append(f'--start-datetime={start_at:%Y-%m-%d %H:%M:%S}')
    if stop_at:
        command.append(f'--stop-datetime={stop_at:%Y-%m-%d %H:%M:%S}')
    if start_position is not None:
        command.append(f'--start-position={int(start_position)}')
    if stop_position is not None:
        command.append(f'--stop-position={int(stop_position)}')
    command.extend(str(directory / name) for name in source_files)
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
    except OSError as error:
        raise RankGraveyardError('The configured mariadb-binlog command could not start.') from error
    timed_out = threading.Event()
    timer = threading.Timer(timeout, lambda: (timed_out.set(), process.kill()))
    timer.start()
    try:
        yield from parse_output(process.stdout, source_files[0] if len(source_files) == 1 else '')
        return_code = process.wait()
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
    if timed_out.is_set():
        raise RankGraveyardError('The binlog search exceeded 50 seconds. Use a narrower time range.')
    if return_code:
        raise RankGraveyardError('mariadb-binlog could not read the selected log range.')


def search_deleted_saves(cleaned_data):
    results = []
    total = 0
    deadline = time.monotonic() + BINLOG_TIMEOUT
    for source_file in list_binlogs():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RankGraveyardError(
                'The binlog search exceeded 50 seconds. Use a narrower time range.'
            )
        for save in run_binlog(
            [source_file], cleaned_data['deleted_after'],
            cleaned_data['deleted_before'], timeout=remaining,
        ):
            if save.get('validation_error'):
                continue
            if cleaned_data.get('map_name') and save['map_name'] != cleaned_data['map_name']:
                continue
            if cleaned_data.get('code') and save['code'] != cleaned_data['code']:
                continue
            if cleaned_data.get('player_name') and cleaned_data['player_name'] not in save['players']:
                continue
            if (
                cleaned_data.get('game_uuid')
                and save['game_uuid'] != str(cleaned_data['game_uuid'])
            ):
                continue
            total += 1
            results.append(save)
            if len(results) > RESULT_LIMIT:
                results.pop(0)
    results.sort(key=lambda save: save['deleted_at'] or '', reverse=True)
    return results, total


def candidate_target(save):
    return {
        key: save[key]
        for key in (
            'source_file', 'start_position', 'stop_position', 'map_name',
            'code', 'payload_hash',
        )
    }


def load_candidate(target):
    candidates = list(run_binlog(
        [target['source_file']], start_position=target['start_position'],
        stop_position=target['stop_position'],
    ))
    for save in candidates:
        if (
            save['map_name'] == target['map_name']
            and save['code'] == target['code']
            and save['payload_hash'] == target['payload_hash']
        ):
            if save.get('validation_error'):
                raise RankGraveyardError(save['validation_error'])
            return save
    raise RankGraveyardError('The selected deleted save no longer exists in that binlog.')


def save_status(save, cursor=None, lock=False):
    connection = database()
    if connection.vendor != 'mysql':
        raise RankGraveyardError('Save Recovery requires the MariaDB records database.')
    if cursor is None:
        with connection.cursor() as own_cursor:
            return save_status(save, own_cursor, lock)
    cursor.execute(
        'SELECT Savegame AS savegame, Timestamp AS saved_at, Server AS server, '
        'COALESCE(DDNet7, 0) AS ddnet7, SaveId AS save_id '
        'FROM record_saves WHERE Map = %s AND Code = %s' + (' FOR UPDATE' if lock else ''),
        (save['map_name'], save['code']),
    )
    rows = rows_as_dicts(cursor)
    if not rows:
        return 'Loaded Or Missing', 'missing'
    if len(rows) != 1:
        return 'Code Conflict', 'conflict'
    row = rows[0]
    live = {
        'savegame': row['savegame'], 'map_name': save['map_name'], 'code': save['code'],
        'timestamp': row['saved_at'].isoformat(), 'server': row['server'],
        'ddnet7': bool(row['ddnet7']), 'save_id': row['save_id'],
    }
    if payload_hash(live) == save['payload_hash']:
        return 'In Live Database', 'live'
    return 'Code Conflict', 'conflict'


def live_similar_saves(save):
    connection = database()
    if connection.vendor != 'mysql':
        raise RankGraveyardError('Save Recovery requires the MariaDB records database.')
    if not save.get('game_uuid'):
        return [], False
    target_players = set(save['players'])
    player_conditions = []
    arguments = [save['map_name'], save['game_uuid']]
    for player in sorted(target_players, key=str.casefold):
        player_conditions.append('INSTR(Savegame, %s) > 0')
        arguments.append(f'\n{player}\t')
    conditions = [
        'Map = %s',
        'INSTR(Savegame, %s) > 0',
        f"({' OR '.join(player_conditions)})",
    ]
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT Savegame AS savegame, Code AS code, Timestamp AS saved_at, '
            'Server AS server, SaveId AS save_id FROM record_saves WHERE '
            + ' AND '.join(conditions)
            + ' ORDER BY Timestamp DESC LIMIT 101',
            arguments,
        )
        rows = rows_as_dicts(cursor)
    matches = []
    for row in rows:
        try:
            players, game_uuid = save_details(row['savegame'])
        except RankGraveyardError:
            continue
        matching_players = sorted(target_players.intersection(players), key=str.casefold)
        if game_uuid != save['game_uuid'] or not matching_players:
            continue
        matches.append({
            'code': row['code'],
            'saved_at': row['saved_at'],
            'server': row['server'],
            'save_id': row['save_id'],
            'game_uuid': game_uuid,
            'players': players,
            'matching_players': matching_players,
        })
    return matches[:100], len(matches) > 100


def restore_save(save, actor_id, actor_name, reason, source_action_id=None):
    connection = database()
    if connection.vendor != 'mysql':
        raise RankGraveyardError('Save Recovery requires the MariaDB records database.')
    with transaction.atomic(using='ddnet_db'):
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT COUNT(*) FROM information_schema.statistics '
                "WHERE table_schema = DATABASE() AND table_name = 'record_saves' "
                "AND non_unique = 0 GROUP BY index_name HAVING "
                "GROUP_CONCAT(column_name ORDER BY seq_in_index) = 'Map,Code'"
            )
            if cursor.fetchone() is None:
                raise RankGraveyardError(
                    'record_saves must have a unique Map and Code key before restoration.'
                )
            status, state_key = save_status(save, cursor, lock=True)
            if state_key != 'missing':
                raise RankGraveyardError(
                    'This save cannot be restored because its Map and Code are already live.'
                )
            cursor.execute(
                'INSERT INTO record_saves '
                '(Savegame, Map, Code, Timestamp, Server, DDNet7, SaveId) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s)',
                (
                    save['savegame'], save['map_name'], save['code'],
                    datetime.fromisoformat(save['timestamp']), save['server'],
                    save['ddnet7'], save['save_id'],
                ),
            )
            if cursor.rowcount != 1:
                raise RankGraveyardError('The deleted save was not restored.')
            action_id = uuid.uuid4()
            details = {'save': save}
            if source_action_id:
                details['source_action_id'] = str(source_action_id)
            cursor.execute(
                'INSERT INTO record_control_history '
                '(action_id, target_type, created_at, created_by_id, '
                'created_by_name, reason, details, summary, map_name, map_count, '
                'player_name, player_count, finish_count, team_count) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s, 0, 0)',
                (
                    str(action_id), 'save_restore', utc_now(), actor_id, actor_name,
                    reason, json.dumps(details, ensure_ascii=False),
                    f'{save["map_name"]} Save {save["code"]}', save['map_name'],
                    save['players'][0] if save['players'] else None,
                    len(save['players']),
                ),
            )
            if cursor.rowcount != 1:
                raise RankGraveyardError('The save recovery was not added to History.')
    return action_id
