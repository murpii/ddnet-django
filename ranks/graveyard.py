import json
import struct
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone

from django.core.paginator import Paginator
from django.db import connections, transaction


SEARCH_LIMIT = 100
SCAN_LIMIT = 100
PLAYER_MAP_PREVIEW_LIMIT = 100


class RankGraveyardError(Exception):
    pass


class RankNotFound(RankGraveyardError):
    pass


class RankConflict(RankGraveyardError):
    pass


class RankAmbiguous(RankGraveyardError):
    pass


def database():
    connection = connections['ddnet_db']
    if connection.vendor not in ('mysql', 'postgresql'):
        raise RankGraveyardError(f'Unsupported score database: {connection.vendor}')
    return connection


def rows_as_dicts(cursor):
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def mysql_finish_columns(alias='r'):
    checkpoints = ', '.join(f'{alias}.cp{number} AS cp{number}' for number in range(1, 26))
    return (
        f'{alias}.Map AS map_name, {alias}.Name AS player_name, '
        f'{alias}.Timestamp AS finished_at, CAST({alias}.Time AS DOUBLE) AS time_value, '
        f'{alias}.Server AS server, {checkpoints}, '
        f'{alias}.GameID AS game_id, COALESCE({alias}.DDNet7, 0) AS ddnet7'
    )


def postgres_finish_columns(alias='f'):
    return (
        f'{alias}.map_id, m.name AS map_name, {alias}.player_id, '
        f'p.name AS player_name, {alias}.time_cs AS time_value, '
        f'{alias}.finished_at, {alias}.server, {alias}.game_uuid AS game_id, '
        f'{alias}.cp_times, {alias}.ddnet7'
    )


def finish_key(row):
    if row.get('map_id') is not None:
        return (
            row['map_id'], row['player_id'], row['time_value'],
            row['finished_at'], row['server'],
        )
    return (
        row['map_name'], row['player_name'], row['time_value'],
        row['finished_at'], row['server'],
    )


def mysql_team_key(row):
    return (
        row['map_name'], row['finished_at'], row['time_value'],
        bytes(row['team_id']), row['game_id'], bool(row['ddnet7']),
    )


def token_datetime(value):
    if isinstance(value, str):
        return value
    return value.isoformat(sep=' ')


def rank_target(row):
    if row.get('map_id') is not None:
        return {
            'kind': 'rank',
            'map_id': row['map_id'],
            'player_id': row['player_id'],
            'time_value': row['time_value'],
            'finished_at': token_datetime(row['finished_at']),
            'server': row['server'],
        }
    return {
        'kind': 'rank',
        'map_name': row['map_name'],
        'player_name': row['player_name'],
        'finished_at': token_datetime(row['finished_at']),
        'time_value': row['time_value'],
        'server': row['server'],
    }


def team_target(team):
    if team.get('map_id') is not None:
        return {'kind': 'team', 'team_id': bytes(team['team_id']).hex()}
    first = team['rows'][0]
    return {
        'kind': 'team',
        'map_name': first['map_name'],
        'finished_at': token_datetime(first['finished_at']),
        'time_value': first['time_value'],
        'team_id': bytes(first['team_id']).hex(),
        'game_id': first['game_id'],
        'ddnet7': bool(first['ddnet7']),
    }


def target_key(target):
    return json.dumps(target, sort_keys=True, separators=(',', ':'))


def parse_datetime(value):
    return datetime.fromisoformat(value)


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def search_live(cleaned_data):
    if is_map_only_search(cleaned_data):
        return search_map_leaderboard_page(cleaned_data, 1)[0]
    connection = database()
    with connection.cursor() as cursor:
        if connection.vendor == 'mysql':
            return search_mysql(cursor, cleaned_data)
        return search_postgresql(cursor, cleaned_data)


def is_map_only_search(cleaned_data):
    return bool(
        cleaned_data.get('map_name')
        and not cleaned_data.get('player_name')
        and cleaned_data.get('time') is None
        and not cleaned_data.get('finished_on')
        and not cleaned_data.get('game_id')
    )


def list_map_names():
    connection = database()
    table, column = (
        ('record_maps', 'Map')
        if connection.vendor == 'mysql'
        else ('record_map', 'name')
    )
    with connection.cursor() as cursor:
        cursor.execute(f'SELECT {column} FROM {table} ORDER BY {column}')
        return [row[0] for row in cursor.fetchall()]


def list_checkpoint_player_maps(player_name):
    connection = database()
    with connection.cursor() as cursor:
        if connection.vendor == 'mysql':
            cursor.execute(
                'SELECT DISTINCT r.Map AS map_name, '
                "COALESCE(m.Server, '') AS category, m.Stars AS stars "
                'FROM record_race r LEFT JOIN record_maps m ON m.Map = r.Map '
                'WHERE r.Name = %s',
                (player_name,),
            )
        else:
            cursor.execute(
                'SELECT DISTINCT m.name AS map_name, '
                "COALESCE(m.category, '') AS category, NULL AS stars "
                'FROM record_finish f JOIN record_map m ON m.map_id = f.map_id '
                'JOIN record_player p ON p.player_id = f.player_id '
                'WHERE p.name = %s',
                (player_name,),
            )
        return sorted(rows_as_dicts(cursor), key=lambda row: (
            category_order(row['category']), row['stars'] or 0, row['map_name'].casefold(),
        ))


def category_names():
    return (
        'Novice', 'Moderate', 'Brutal', 'Insane', 'Dummy',
        'DDmaX.Easy', 'DDmaX.Next', 'DDmaX.Pro', 'DDmaX.Nut',
        'Oldschool', 'Solo', 'Race', 'Fun', 'Event',
    )


def category_order(category):
    categories = category_names()
    try:
        return categories.index(category)
    except ValueError:
        return len(categories)


def add_mysql_places(cursor, results, map_name):
    normal_results = [row for row in results if row['kind'] == 'rank']
    if normal_results:
        candidate_times = sorted({row['time_value'] for row in normal_results})
        candidates = ' UNION ALL '.join(
            ['SELECT %s AS candidate_time'] + ['SELECT %s'] * (len(candidate_times) - 1)
        )
        cursor.execute(
            'SELECT candidate_time, COUNT(best_time) + 1 AS current_place '
            f'FROM ({candidates}) candidates LEFT JOIN ('
            'SELECT MIN(Time) AS best_time FROM record_race '
            'WHERE Map = %s GROUP BY Name'
            ') leaderboard ON leaderboard.best_time < candidates.candidate_time '
            'GROUP BY candidate_time',
            [*candidate_times, map_name],
        )
        places = {
            float(row['candidate_time']): row['current_place']
            for row in rows_as_dicts(cursor)
        }
        for row in normal_results:
            row['current_place'] = places.get(row['time_value'])

    team_results = [row for row in results if row['kind'] == 'team']
    if team_results:
        conditions = []
        arguments = [map_name]
        keys = []
        for row in team_results:
            target = row['target']
            key = (
                target['map_name'], parse_datetime(target['finished_at']),
                target['time_value'], bytes.fromhex(target['team_id']),
                target['game_id'], bool(target['ddnet7']),
            )
            keys.append(key)
            conditions.append(
                '(Timestamp = %s AND ABS(CAST(Time AS DOUBLE) - %s) < 0.0001 '
                'AND ID = %s AND GameID <=> %s AND DDNet7 = %s)'
            )
            arguments.extend((key[1], key[2], key[3], key[4], key[5]))
        cursor.execute(
            'SELECT Map AS map_name, Timestamp AS finished_at, '
            'CAST(Time AS DOUBLE) AS time_value, ID AS team_id, '
            'GameID AS game_id, DDNet7 AS ddnet7, current_place FROM ('
            'SELECT Map, Timestamp, Time, ID, GameID, '
            'COALESCE(DDNet7, 0) AS DDNet7, '
            'RANK() OVER (ORDER BY Time) AS current_place '
            'FROM record_teamrace WHERE Map = %s '
            'GROUP BY Map, Timestamp, Time, ID, GameID, COALESCE(DDNet7, 0)'
            ') leaderboard WHERE ' + ' OR '.join(conditions),
            arguments,
        )
        places = {
            mysql_team_key(row): row['current_place']
            for row in rows_as_dicts(cursor)
        }
        for row, key in zip(team_results, keys):
            row.pop('team_id', None)
            row['current_place'] = places.get(key)


def add_postgresql_places(cursor, results, map_id):
    normal_results = [row for row in results if row['kind'] == 'rank']
    if normal_results:
        candidate_times = sorted({round(row['time_value'] * 100) for row in normal_results})
        candidates = ', '.join(['(%s)'] * len(candidate_times))
        cursor.execute(
            'SELECT candidate_time, COUNT(best.time_cs) + 1 AS current_place '
            f'FROM (VALUES {candidates}) AS candidates(candidate_time) '
            'LEFT JOIN record_best best ON best.map_id = %s '
            'AND best.time_cs < candidates.candidate_time '
            'GROUP BY candidate_time',
            [*candidate_times, map_id],
        )
        places = {
            row['candidate_time']: row['current_place']
            for row in rows_as_dicts(cursor)
        }
        for row in normal_results:
            row.pop('player_id')
            row['current_place'] = places.get(round(row['time_value'] * 100))

    team_results = [row for row in results if row['kind'] == 'team']
    if team_results:
        team_ids = [row.pop('team_id') for row in team_results]
        placeholders = ', '.join(['%s'] * len(team_ids))
        cursor.execute(
            'SELECT team_id, current_place FROM ('
            'SELECT team_id, RANK() OVER (ORDER BY time_cs) AS current_place '
            'FROM record_team WHERE map_id = %s'
            f') leaderboard WHERE team_id IN ({placeholders})',
            [map_id, *team_ids],
        )
        places = {bytes(row['team_id']): row['current_place'] for row in rows_as_dicts(cursor)}
        for row, team_id in zip(team_results, team_ids):
            row['current_place'] = places.get(bytes(team_id))


def checkpoint_columns(start_checkpoint, end_checkpoint):
    try:
        start_checkpoint = int(start_checkpoint)
        end_checkpoint = int(end_checkpoint)
    except (TypeError, ValueError) as error:
        raise RankGraveyardError('Invalid checkpoint selection.') from error
    if not 1 <= start_checkpoint <= end_checkpoint <= 25:
        raise RankGraveyardError('Invalid checkpoint selection.')
    return [f'cp{number}' for number in range(start_checkpoint, end_checkpoint + 1)]


def checkpoint_label(start_checkpoint, end_checkpoint):
    if start_checkpoint == end_checkpoint:
        return f'CP{start_checkpoint}'
    return f'CP{start_checkpoint} Through CP{end_checkpoint}'


def checkpoint_result(row, start_checkpoint, end_checkpoint, values, vendor, map_name):
    label = checkpoint_label(start_checkpoint, end_checkpoint)
    signature = checkpoint_signature(values, vendor)
    return {
        'map_name': map_name,
        'checkpoint_label': label,
        'signature': signature,
        'player_count': int(row['player_count']),
        'row_count': int(row['row_count']),
        'run_count': int(row['run_count']),
        'game_count': int(row['game_count']),
        'team_row_count': int(row['team_row_count']),
        'selected_row_count': int(row['selected_row_count']),
        'first_seen': row['first_seen'],
        'last_seen': row['last_seen'],
        'target': {
            'kind': 'checkpoint',
            'database_vendor': vendor,
            'map_name': map_name,
            'start_checkpoint': start_checkpoint,
            'end_checkpoint': end_checkpoint,
            'values': values,
            'player_name': row.get('selected_player') or None,
            'expected_row_count': int(row['selected_row_count']),
        },
    }


def checkpoint_signature(values, vendor):
    return ', '.join(
        f'{value / 100:.2f}' if vendor == 'postgresql' else f'{value:.2f}'
        for value in values
    )


def checkpoint_margin_value(ticks, vendor):
    return ticks * 2 if vendor == 'postgresql' else ticks / 50 + 0.0001


def checkpoint_margin_label(ticks):
    if not ticks:
        return 'Exact'
    unit = 'Tick' if ticks == 1 else 'Ticks'
    return f'{ticks} {unit} (±{ticks / 50:.2f} Seconds)'


def scan_checkpoint_clusters(cleaned_data):
    connection = database()
    with connection.cursor() as cursor:
        if cleaned_data.get('player_name'):
            player_data = dict(cleaned_data)
            if player_data.get('map_names') is None:
                player_data['map_names'] = [player_data['map_name']]
            return scan_leaderboard_checkpoints(cursor, player_data, connection.vendor)
        if connection.vendor == 'mysql':
            results = scan_mysql_checkpoints(cursor, cleaned_data)
        else:
            results = scan_postgresql_checkpoints(cursor, cleaned_data)
        return group_checkpoint_results(cursor, results, connection.vendor)


def scan_leaderboard_checkpoints(cursor, cleaned_data, vendor):
    results = []
    for map_name in cleaned_data['map_names']:
        rows = leaderboard_rows(
            cursor, vendor, map_name, cleaned_data['leaderboard_depth'],
            include_player=cleaned_data['player_name'],
        )
        results.extend(leaderboard_checkpoint_results(rows, cleaned_data, vendor, map_name))
    return results


def leaderboard_rows(
    cursor, vendor, map_name, depth, require_checkpoints=True, include_player=None
):
    rows = []
    player_names = set()
    batch_size = max(depth * 2, 200)
    offset = 0
    while len(rows) < depth:
        if vendor == 'mysql':
            cursor.execute(
                f'SELECT {mysql_finish_columns()} '
                'FROM record_race r FORCE INDEX (idx_map_time_name) '
                'WHERE r.Map = %s ORDER BY r.Time, r.Name LIMIT %s OFFSET %s',
                (map_name, batch_size, offset),
            )
        else:
            cursor.execute(
                f'SELECT {postgres_finish_columns()} '
                'FROM record_finish f JOIN record_map m ON m.map_id = f.map_id '
                'JOIN record_player p ON p.player_id = f.player_id '
                'WHERE m.name = %s '
                + ('AND f.cp_times IS NOT NULL ' if require_checkpoints else '')
                + 'ORDER BY f.time_cs, p.name LIMIT %s OFFSET %s',
                (map_name, batch_size, offset),
            )
        batch = rows_as_dicts(cursor)
        for row in batch:
            if row['player_name'] in player_names:
                continue
            player_names.add(row['player_name'])
            rows.append(row)
            if len(rows) == depth:
                break
        if len(batch) < batch_size:
            break
        offset += batch_size
    if include_player and include_player not in player_names:
        if vendor == 'mysql':
            cursor.execute(
                f'SELECT {mysql_finish_columns()} FROM record_race r '
                'WHERE r.Map = %s AND r.Name = %s ORDER BY r.Time LIMIT 1',
                (map_name, include_player),
            )
        else:
            cursor.execute(
                f'SELECT {postgres_finish_columns()} '
                'FROM record_finish f JOIN record_map m ON m.map_id = f.map_id '
                'JOIN record_player p ON p.player_id = f.player_id '
                'WHERE m.name = %s AND p.name = %s AND f.cp_times IS NOT NULL '
                'ORDER BY f.time_cs LIMIT 1',
                (map_name, include_player),
            )
        rows.extend(rows_as_dicts(cursor))
    return rows


def search_map_leaderboard_page(cleaned_data, page_number):
    connection = database()
    map_name = cleaned_data['map_name']
    selected_limit = cleaned_data['map_rank_limit']
    with connection.cursor() as cursor:
        if connection.vendor == 'mysql':
            cursor.execute(
                'SELECT COUNT(DISTINCT Name) FROM record_race WHERE Map = %s',
                (map_name,),
            )
        else:
            cursor.execute(
                'SELECT COUNT(DISTINCT f.player_id) FROM record_finish f '
                'JOIN record_map m ON m.map_id = f.map_id WHERE m.name = %s',
                (map_name,),
            )
        total = cursor.fetchone()[0]
        if selected_limit is not None:
            total = min(total, selected_limit)
        page = Paginator(range(total), SEARCH_LIMIT).get_page(page_number)
        rows = find_map_leaderboard_page(
            cursor, connection.vendor, map_name,
            page.start_index() - 1 if total else 0,
            len(page.object_list),
        )
        category, stars = find_map_category(cursor, connection.vendor, map_name)

    results = map_leaderboard_results(rows, connection.vendor, category, stars)
    return results, page


def find_map_leaderboard_page(cursor, vendor, map_name, offset, limit):
    if not limit:
        return []
    if vendor == 'mysql':
        cursor.execute(
            'SELECT page_rows.* FROM ('
            f'SELECT {mysql_finish_columns()}, page_keys.current_place, '
            'ROW_NUMBER() OVER ('
            'PARTITION BY r.Name ORDER BY r.Timestamp, r.Server'
            ') AS finish_row '
            'FROM record_race r FORCE INDEX (idx_map_name_time) JOIN ('
            'SELECT best.*, RANK() OVER (ORDER BY best_time) AS current_place '
            'FROM ('
            'SELECT Name, MIN(Time) AS best_time '
            'FROM record_race FORCE INDEX (idx_map_name_time) '
            'WHERE Map = %s GROUP BY Name'
            ') best ORDER BY best_time, Name LIMIT %s OFFSET %s'
            ') page_keys ON r.Map = %s AND r.Name = page_keys.Name '
            'AND r.Time = page_keys.best_time'
            ') page_rows WHERE finish_row = 1 ORDER BY time_value, player_name',
            (map_name, limit, offset, map_name),
        )
    else:
        cursor.execute(
            'SELECT page_rows.* FROM ('
            f'SELECT {postgres_finish_columns()}, page_keys.current_place, '
            'ROW_NUMBER() OVER ('
            'PARTITION BY f.player_id '
            'ORDER BY f.finished_at, f.server, f.game_uuid'
            ') AS finish_row '
            'FROM record_finish f JOIN record_map m ON m.map_id = f.map_id '
            'JOIN record_player p ON p.player_id = f.player_id JOIN ('
            'SELECT best.*, RANK() OVER (ORDER BY best_time) AS current_place '
            'FROM ('
            'SELECT f.player_id, MIN(f.time_cs) AS best_time '
            'FROM record_finish f JOIN record_map m ON m.map_id = f.map_id '
            'WHERE m.name = %s GROUP BY f.player_id'
            ') best ORDER BY best_time, player_id LIMIT %s OFFSET %s'
            ') page_keys ON page_keys.player_id = f.player_id '
            'AND page_keys.best_time = f.time_cs WHERE m.name = %s'
            ') page_rows WHERE finish_row = 1 ORDER BY time_value, player_name',
            (map_name, limit, offset, map_name),
        )
    return rows_as_dicts(cursor)


def find_map_category(cursor, vendor, map_name):
    if vendor == 'mysql':
        cursor.execute(
            "SELECT COALESCE(Server, ''), Stars FROM record_maps WHERE Map = %s",
            (map_name,),
        )
        return cursor.fetchone() or ('', None)
    cursor.execute(
        "SELECT COALESCE(category, '') FROM record_map WHERE name = %s",
        (map_name,),
    )
    map_row = cursor.fetchone()
    return (map_row[0] if map_row else ''), None


def map_leaderboard_results(rows, vendor, category, stars):

    results = []
    for row in rows:
        results.append({
            'kind': 'rank',
            'kind_label': 'Normal Rank',
            'impact': '1 Rank',
            'map_name': row['map_name'],
            'players': row['player_name'],
            'player_names': [row['player_name']],
            'finished_at': row['finished_at'],
            'time_value': (
                row['time_value'] / 100 if vendor == 'postgresql' else row['time_value']
            ),
            'server': row['server'],
            'category': category,
            'stars': stars,
            'game_id': row.get('game_id'),
            'has_checkpoints': has_checkpoint_values(row),
            'current_place': row['current_place'],
            'target': rank_target(row),
        })
    return results


def leaderboard_checkpoint_results(rows, cleaned_data, vendor, map_name):
    start_checkpoint = cleaned_data['start_checkpoint']
    end_checkpoint = cleaned_data['end_checkpoint']
    columns = checkpoint_columns(start_checkpoint, end_checkpoint)
    selected_player = cleaned_data['player_name']
    selected = cleaned_data.get('source_finish') or next(
        (row for row in rows if row['player_name'] == selected_player), None
    )
    if selected is None:
        return []
    values = leaderboard_checkpoint_values(selected, columns, vendor)
    if not all(values):
        return []
    margin_ticks = cleaned_data.get('checkpoint_margin', 0)
    margin = checkpoint_margin_value(margin_ticks, vendor)
    matches = []
    candidates = [row for row in rows if row['player_name'] != selected_player]
    candidates.append(selected)
    for row in candidates:
        candidate = leaderboard_checkpoint_values(row, columns, vendor)
        if all(candidate) and all(
            abs(candidate_value - selected_value) <= margin
            for candidate_value, selected_value in zip(candidate, values)
        ):
            matches.append(row)
    if len(matches) < cleaned_data['minimum_players']:
        return []
    signature = checkpoint_signature(values, vendor)
    return [{
        'map_name': map_name,
        'checkpoint_label': checkpoint_label(start_checkpoint, end_checkpoint),
        'signature': signature,
        'checkpoint_margin': checkpoint_margin_label(margin_ticks),
        'signatures': [signature],
        'cluster_count': 1,
        'associated_team_count': 0,
        'player_count': len(matches),
        'row_count': len(matches),
        'run_count': len({row['finished_at'] for row in matches}),
        'game_count': len({row['game_id'] for row in matches if row.get('game_id')}),
        'team_row_count': 0,
        'selected_row_count': 1,
        'first_seen': min(row['finished_at'] for row in matches),
        'last_seen': max(row['finished_at'] for row in matches),
        'leaderboard_sample': True,
        'target': {
            'kind': 'leaderboard_checkpoint',
            'database_vendor': vendor,
            'map_name': map_name,
            'start_checkpoint': start_checkpoint,
            'end_checkpoint': end_checkpoint,
            'values': list(values),
            'player_name': selected_player,
            'source_rank': rank_target(selected),
            'minimum_players': cleaned_data['minimum_players'],
            'leaderboard_depth': cleaned_data['leaderboard_depth'],
            'checkpoint_margin': margin_ticks,
        },
    }]


def leaderboard_checkpoint_values(row, columns, vendor):
    if vendor == 'mysql':
        return tuple(float(row[column]) for column in columns)
    packed = struct.unpack('<25i', bytes(row['cp_times']))
    return tuple(packed[int(column[2:]) - 1] for column in columns)


def scan_mysql_checkpoints(cursor, cleaned_data):
    start_checkpoint = cleaned_data['start_checkpoint']
    end_checkpoint = cleaned_data['end_checkpoint']
    columns = checkpoint_columns(start_checkpoint, end_checkpoint)
    selected = ', '.join(f'r.{column} AS {column}' for column in columns)
    grouped = ', '.join(f'r.{column}' for column in columns)
    nonzero = ' AND '.join(f'r.{column} != 0' for column in columns)
    player_name = cleaned_data.get('player_name')
    selected_count = 'SUM(r.Name = %s)' if player_name else 'COUNT(*)'
    map_names = cleaned_data.get('map_names')
    if map_names is not None and not map_names:
        return []
    if map_names is not None:
        placeholders = ', '.join(['%s'] * len(map_names))
        selected_columns = ', '.join(columns)
        selected_nonzero = ' AND '.join(f'{column} != 0' for column in columns)
        selected_join = (
            f'JOIN (SELECT Map, {selected_columns} FROM record_race '
            f'WHERE Name = %s AND Map IN ({placeholders}) AND {selected_nonzero} '
            f'GROUP BY Map, {selected_columns}) selected_matches '
            'ON selected_matches.Map = r.Map AND '
            + ' AND '.join(
                f'selected_matches.{column} = r.{column}' for column in columns
            ) + ' '
        )
        map_select = 'r.Map AS map_name, '
        where = f'r.Map IN ({placeholders}) AND {nonzero}'
        grouped = f'r.Map, {grouped}'
        arguments = [
            player_name, player_name, player_name,
            *map_names, *map_names, cleaned_data['minimum_players'],
        ]
    else:
        selected_join = ''
        map_select = ''
        where = f'r.Map = %s AND {nonzero}'
        arguments = (
            [player_name, player_name, cleaned_data['map_name'], cleaned_data['minimum_players']]
            if player_name
            else [None, cleaned_data['map_name'], cleaned_data['minimum_players']]
        )
    selected_having = ' AND selected_row_count > 0' if player_name else ''
    limit = '' if map_names is not None else f' LIMIT {SCAN_LIMIT}'
    cursor.execute(
        f'SELECT {map_select}{selected}, COUNT(*) AS row_count, '
        f'{selected_count} AS selected_row_count, '
        '%s AS selected_player, '
        'COUNT(DISTINCT r.Name) AS player_count, '
        'COUNT(DISTINCT r.Timestamp) AS run_count, '
        "COUNT(DISTINCT NULLIF(r.GameID, '')) AS game_count, "
        'SUM(EXISTS(SELECT 1 FROM record_teamrace t '
        'WHERE t.Map = r.Map AND t.Name = r.Name AND t.Timestamp = r.Timestamp '
        'AND t.Time = r.Time AND t.GameID <=> r.GameID '
        'AND COALESCE(t.DDNet7, 0) = COALESCE(r.DDNet7, 0))) AS team_row_count, '
        'MIN(r.Timestamp) AS first_seen, MAX(r.Timestamp) AS last_seen '
        f'FROM record_race r {selected_join}WHERE {where} '
        f'GROUP BY {grouped} HAVING COUNT(DISTINCT r.Name) >= %s '
        + selected_having + ' '
        f'ORDER BY player_count DESC, row_count DESC{limit}',
        arguments,
    )
    return [
        checkpoint_result(
            row, start_checkpoint, end_checkpoint, [row[column] for column in columns],
            'mysql', row['map_name'] if map_names is not None else cleaned_data['map_name'],
        )
        for row in rows_as_dicts(cursor)
    ]


def postgres_checkpoint_expression(start_checkpoint, end_checkpoint, alias='f'):
    start = (start_checkpoint - 1) * 4
    length = (end_checkpoint - start_checkpoint + 1) * 4
    return f'substring({alias}.cp_times FROM {start + 1} FOR {length})'


def postgres_checkpoint_value_expression(checkpoint, alias='f'):
    offset = (checkpoint - 1) * 4
    return ' + '.join(
        f'get_byte({alias}.cp_times, {offset + byte}) * {256 ** byte}'
        for byte in range(4)
    )


def scan_postgresql_checkpoints(cursor, cleaned_data):
    start_checkpoint = cleaned_data['start_checkpoint']
    end_checkpoint = cleaned_data['end_checkpoint']
    columns = checkpoint_columns(start_checkpoint, end_checkpoint)
    key = postgres_checkpoint_expression(start_checkpoint, end_checkpoint)
    nonzero = ' AND '.join(
        f"substring(f.cp_times FROM {(int(column[2:]) - 1) * 4 + 1} FOR 4) "
        "<> decode('00000000', 'hex')"
        for column in columns
    )
    player_name = cleaned_data.get('player_name')
    selected_count = 'COUNT(*) FILTER (WHERE p.name = %s)' if player_name else 'COUNT(*)'
    map_names = cleaned_data.get('map_names')
    if map_names is not None and not map_names:
        return []
    if map_names is not None:
        placeholders = ', '.join(['%s'] * len(map_names))
        selected_key = postgres_checkpoint_expression(
            start_checkpoint, end_checkpoint, 'selected_finish'
        )
        selected_nonzero = ' AND '.join(
            f"substring(selected_finish.cp_times FROM {(int(column[2:]) - 1) * 4 + 1} FOR 4) "
            "<> decode('00000000', 'hex')"
            for column in columns
        )
        selected_join = (
            f'JOIN (SELECT selected_finish.map_id, {selected_key} AS checkpoint_key '
            'FROM record_finish selected_finish '
            'JOIN record_player selected_player '
            'ON selected_player.player_id = selected_finish.player_id '
            'JOIN record_map selected_map ON selected_map.map_id = selected_finish.map_id '
            f'WHERE selected_player.name = %s AND selected_map.name IN ({placeholders}) '
            f'AND selected_finish.cp_times IS NOT NULL AND {selected_nonzero} '
            f'GROUP BY selected_finish.map_id, {selected_key}) selected_matches '
            f'ON selected_matches.map_id = f.map_id AND selected_matches.checkpoint_key = {key} '
        )
        map_select = 'm.name AS map_name, '
        where = f'm.name IN ({placeholders}) AND f.cp_times IS NOT NULL AND {nonzero}'
        grouped = f'f.map_id, m.name, {key}'
        arguments = [
            player_name, player_name, player_name,
            *map_names, *map_names, cleaned_data['minimum_players'], player_name,
        ]
    else:
        selected_join = ''
        map_select = ''
        where = f'm.name = %s AND f.cp_times IS NOT NULL AND {nonzero}'
        grouped = key
        arguments = (
            [player_name, player_name, cleaned_data['map_name'], cleaned_data['minimum_players'], player_name]
            if player_name
            else [None, cleaned_data['map_name'], cleaned_data['minimum_players']]
        )
    selected_having = (
        ' AND COUNT(*) FILTER (WHERE p.name = %s) > 0' if player_name else ''
    )
    limit = '' if map_names is not None else f' LIMIT {SCAN_LIMIT}'
    cursor.execute(
        f'SELECT {map_select}{key} AS checkpoint_key, COUNT(*) AS row_count, '
        f'{selected_count} AS selected_row_count, %s AS selected_player, '
        'COUNT(DISTINCT p.name) AS player_count, '
        'COUNT(DISTINCT f.finished_at) AS run_count, '
        'COUNT(DISTINCT f.game_uuid) AS game_count, '
        'SUM((EXISTS(SELECT 1 FROM record_team t '
        'JOIN record_team_player tp ON tp.team_id = t.team_id '
        'WHERE t.map_id = f.map_id AND tp.player_id = f.player_id '
        'AND t.time_cs = f.time_cs AND t.finished_at = f.finished_at '
        'AND t.game_uuid IS NOT DISTINCT FROM f.game_uuid))::int) AS team_row_count, '
        'MIN(f.finished_at) AS first_seen, MAX(f.finished_at) AS last_seen '
        f'FROM record_finish f {selected_join}JOIN record_map m ON m.map_id = f.map_id '
        'JOIN record_player p ON p.player_id = f.player_id '
        f'WHERE {where} '
        f'GROUP BY {grouped} HAVING COUNT(DISTINCT p.name) >= %s '
        + selected_having + ' '
        f'ORDER BY player_count DESC, row_count DESC{limit}',
        arguments,
    )
    results = []
    for row in rows_as_dicts(cursor):
        values = list(struct.unpack(f'<{len(columns)}i', bytes(row['checkpoint_key'])))
        results.append(checkpoint_result(
            row, start_checkpoint, end_checkpoint, values,
            'postgresql', row['map_name'] if map_names is not None else cleaned_data['map_name']
        ))
    return results


def checkpoint_components(team_sets):
    remaining = set(range(len(team_sets)))
    components = []
    while remaining:
        first = min(remaining)
        remaining.remove(first)
        component = []
        pending = [first]
        while pending:
            current = pending.pop()
            component.append(current)
            linked = [
                candidate for candidate in remaining
                if team_sets[current] & team_sets[candidate]
            ]
            for candidate in linked:
                remaining.remove(candidate)
                pending.append(candidate)
        components.append(sorted(component))
    return components


def mysql_checkpoint_group_data(cursor, results):
    columns = checkpoint_columns(
        results[0]['target']['start_checkpoint'],
        results[0]['target']['end_checkpoint'],
    )
    cluster_where = ' OR '.join(
        '(' + ' AND '.join(
            f'ABS(CAST(r.{column} AS DOUBLE) - %s) < 0.0001'
            for column in columns
        ) + ')'
        for result in results
    )
    arguments = [results[0]['target']['map_name']]
    for result in results:
        arguments.extend(result['target']['values'])
    selected = ', '.join(f'r.{column} AS {column}' for column in columns)
    cursor.execute(
        f'SELECT {mysql_finish_columns("r")}, {selected}, '
        't.Map AS linked_map, t.Timestamp AS linked_at, '
        'CAST(t.Time AS DOUBLE) AS linked_time, t.ID AS linked_team_id, '
        't.GameID AS linked_game_id, COALESCE(t.DDNet7, 0) AS linked_ddnet7 '
        'FROM record_race r LEFT JOIN record_teamrace t '
        'ON t.Map = r.Map AND t.Name = r.Name AND t.Timestamp = r.Timestamp '
        'AND t.Time = r.Time AND t.GameID <=> r.GameID '
        'AND COALESCE(t.DDNet7, 0) = COALESCE(r.DDNet7, 0) '
        f'WHERE r.Map = %s AND ({cluster_where})',
        arguments,
    )
    indexes = {
        tuple(float(value) for value in result['target']['values']): index
        for index, result in enumerate(results)
    }
    data = [{'teams': set(), 'direct': {}} for result in results]
    for row in rows_as_dicts(cursor):
        key = tuple(float(row[column]) for column in columns)
        index = indexes.get(key)
        if index is None:
            continue
        team_id = row.pop('linked_team_id')
        if team_id is not None:
            team_key = (
                row.pop('linked_map'), row.pop('linked_at'), row.pop('linked_time'),
                bytes(team_id), row.pop('linked_game_id'), bool(row.pop('linked_ddnet7')),
            )
            data[index]['teams'].add(team_key)
        else:
            row.pop('linked_map')
            row.pop('linked_at')
            row.pop('linked_time')
            row.pop('linked_game_id')
            row.pop('linked_ddnet7')
        data[index]['direct'][finish_key(row)] = row
    return data


def group_checkpoint_results(cursor, results, vendor):
    results_by_map = {}
    for result in results:
        results_by_map.setdefault(result['target']['map_name'], []).append(result)
    grouped = []
    for map_results in results_by_map.values():
        grouped.extend(group_checkpoint_map_results(cursor, map_results, vendor))
    return grouped


def group_checkpoint_map_results(cursor, results, vendor):
    if not results:
        return results
    if vendor == 'mysql':
        data = mysql_checkpoint_group_data(cursor, results)
    else:
        data = []
        for result in results:
            population_target = dict(result['target'])
            population_target['player_name'] = None
            population_target['expected_row_count'] = result['row_count']
            preview = preview_postgresql_checkpoint(cursor, population_target, False)
            teams = {target_key(team_target(team)) for team in preview['teams']}
            columns, values = checkpoint_target_values(population_target)
            direct = {}
            for row in preview['finishes']:
                packed = struct.unpack('<25i', bytes(row['cp_times']))
                actual = [packed[int(column[2:]) - 1] for column in columns]
                if actual == values:
                    direct[finish_key(row)] = row
            data.append({'teams': teams, 'direct': direct})

    grouped = []
    for indexes in checkpoint_components([item['teams'] for item in data]):
        if len(indexes) == 1:
            result = results[indexes[0]]
            result['cluster_count'] = 1
            result['signatures'] = [result['signature']]
            result['associated_team_count'] = 0
            grouped.append(result)
            continue
        members = [results[index] for index in indexes]
        direct = {}
        team_counts = {}
        for index in indexes:
            direct.update(data[index]['direct'])
            for key in data[index]['teams']:
                team_counts[key] = team_counts.get(key, 0) + 1
        first = dict(members[0])
        first.update({
            'cluster_count': len(members),
            'signatures': [member['signature'] for member in members],
            'associated_team_count': sum(count > 1 for count in team_counts.values()),
            'player_count': len({row['player_name'] for row in direct.values()}),
            'row_count': len(direct),
            'selected_row_count': sum(member['selected_row_count'] for member in members),
            'run_count': len({row['finished_at'] for row in direct.values()}),
            'game_count': len({
                row['game_id'] for row in direct.values() if row.get('game_id')
            }),
            'team_row_count': sum(member['team_row_count'] for member in members),
            'first_seen': min(member['first_seen'] for member in members),
            'last_seen': max(member['last_seen'] for member in members),
            'target': {
                'kind': 'checkpoint_group',
                'database_vendor': vendor,
                'map_name': members[0]['target']['map_name'],
                'clusters': [member['target'] for member in members],
            },
        })
        grouped.append(first)
    return grouped


def search_mysql(cursor, cleaned_data):
    def search_conditions(alias):
        conditions = []
        arguments = []
        if cleaned_data.get('map_name'):
            conditions.append(f'{alias}Map = %s')
            arguments.append(cleaned_data['map_name'])
        if cleaned_data.get('player_name'):
            conditions.append(f'{alias}Name = %s')
            arguments.append(cleaned_data['player_name'])
        if cleaned_data.get('time') is not None:
            conditions.append(f'{alias}Time BETWEEN %s AND %s')
            arguments.extend((cleaned_data['time'] - 0.005, cleaned_data['time'] + 0.005))
        if cleaned_data.get('finished_on'):
            start = datetime.combine(cleaned_data['finished_on'], time.min)
            conditions.append(f'{alias}Timestamp >= %s AND {alias}Timestamp < %s')
            arguments.extend((start, start + timedelta(days=1)))
        if cleaned_data.get('game_id'):
            conditions.append(f'{alias}GameID = %s')
            arguments.append(cleaned_data['game_id'])
        return conditions, arguments

    player_only = bool(cleaned_data.get('player_name') and not cleaned_data.get('map_name'))
    limit = '' if player_only else f' LIMIT {SEARCH_LIMIT}'
    conditions, arguments = search_conditions('r.')
    where = ' AND '.join(conditions)
    cursor.execute(
        f'SELECT {mysql_finish_columns()}, maps.Server AS category, maps.Stars AS stars '
        'FROM record_race r LEFT JOIN record_maps maps ON maps.Map = r.Map '
        f'WHERE {where} ORDER BY r.Timestamp DESC{limit}',
        arguments,
    )
    finishes = rows_as_dicts(cursor)

    team_conditions, team_arguments = search_conditions('selected.')
    cursor.execute(
        'SELECT t.Map AS map_name, t.Name AS player_name, '
        't.Timestamp AS finished_at, CAST(t.Time AS DOUBLE) AS time_value, '
        't.ID AS team_id, t.GameID AS game_id, '
        'COALESCE(t.DDNet7, 0) AS ddnet7, '
        'maps.Server AS category, maps.Stars AS stars '
        'FROM record_teamrace t JOIN ('
        'SELECT Map, Timestamp, Time, ID, GameID, COALESCE(DDNet7, 0) AS DDNet7 '
        'FROM record_teamrace selected '
        f"WHERE {' AND '.join(team_conditions)} "
        'GROUP BY Map, Timestamp, Time, ID, GameID, COALESCE(DDNet7, 0) '
        f'ORDER BY Timestamp DESC{limit}'
        ') matched ON t.Map = matched.Map AND t.Timestamp = matched.Timestamp '
        'AND t.Time = matched.Time AND t.ID = matched.ID '
        'AND t.GameID <=> matched.GameID '
        'AND COALESCE(t.DDNet7, 0) = matched.DDNet7 '
        'LEFT JOIN record_maps maps ON maps.Map = t.Map',
        team_arguments,
    )
    teams_by_key = {}
    for row in rows_as_dicts(cursor):
        team = teams_by_key.setdefault(mysql_team_key(row), [])
        if row['player_name'] not in {member['player_name'] for member in team}:
            team.append(row)

    linked_finishes = set()
    team_results = []
    for team_rows in teams_by_key.values():
        first = team_rows[0]
        members = sorted(row['player_name'] for row in team_rows)
        team_finish_keys = set()
        for member in team_rows:
            team_finish_keys.add((
                member['map_name'], member['player_name'], member['finished_at'],
                member['time_value'], member['game_id'], bool(member['ddnet7']),
            ))
        linked_finishes.update(team_finish_keys)
        linked_races = [
            finish for finish in finishes
            if (
                finish['map_name'], finish['player_name'], finish['finished_at'],
                finish['time_value'], finish['game_id'], bool(finish['ddnet7']),
            ) in team_finish_keys
        ]
        team_results.append({
            'kind': 'team',
            'kind_label': 'Team Rank',
            'impact': f'{len(members)} Ranks + 1 Team Rank',
            'map_name': first['map_name'],
            'players': ', '.join(members),
            'player_names': members,
            'finished_at': first['finished_at'],
            'time_value': first['time_value'],
            'server': linked_races[0]['server'] if linked_races else '',
            'category': first['category'] or '',
            'stars': first['stars'],
            'game_id': first['game_id'],
            'team_id': first['team_id'],
            'target': {
                'kind': 'team',
                'map_name': first['map_name'],
                'finished_at': token_datetime(first['finished_at']),
                'time_value': first['time_value'],
                'team_id': bytes(first['team_id']).hex(),
                'game_id': first['game_id'],
                'ddnet7': bool(first['ddnet7']),
            },
        })

    results = list(team_results)
    for row in finishes:
        linked_key = (
            row['map_name'], row['player_name'], row['finished_at'],
            row['time_value'], row['game_id'], bool(row['ddnet7']),
        )
        if linked_key in linked_finishes:
            continue
        results.append({
            'kind': 'rank',
            'kind_label': 'Normal Rank',
            'impact': '1 Rank',
            'map_name': row['map_name'],
            'players': row['player_name'],
            'player_names': [row['player_name']],
            'finished_at': row['finished_at'],
            'time_value': row['time_value'],
            'server': row['server'],
            'category': row['category'] or '',
            'stars': row['stars'],
            'game_id': row['game_id'],
            'has_checkpoints': has_checkpoint_values(row),
            'target': {
                'kind': 'rank',
                'map_name': row['map_name'],
                'player_name': row['player_name'],
                'finished_at': token_datetime(row['finished_at']),
                'time_value': row['time_value'],
                'server': row['server'],
            },
        })
    results.sort(key=lambda row: row['finished_at'], reverse=True)
    if player_only:
        results.sort(key=lambda row: (
            category_order(row['category']), row['stars'] or 0, row['map_name'].casefold(),
        ))
    else:
        results = results[:SEARCH_LIMIT]
    if not player_only:
        add_mysql_places(cursor, results, cleaned_data['map_name'])
    return results


def search_postgresql(cursor, cleaned_data):
    finish_conditions = []
    finish_arguments = []
    if cleaned_data.get('map_name'):
        finish_conditions.append('m.name = %s')
        finish_arguments.append(cleaned_data['map_name'])
    if cleaned_data.get('player_name'):
        finish_conditions.append('p.name = %s')
        finish_arguments.append(cleaned_data['player_name'])
    if cleaned_data.get('time') is not None:
        finish_conditions.append('f.time_cs BETWEEN %s AND %s')
        centiseconds = round(cleaned_data['time'] * 100)
        finish_arguments.extend((centiseconds - 1, centiseconds + 1))
    if cleaned_data.get('finished_on'):
        start = datetime.combine(cleaned_data['finished_on'], time.min)
        finish_conditions.append('f.finished_at >= %s AND f.finished_at < %s')
        finish_arguments.extend((start, start + timedelta(days=1)))
    if cleaned_data.get('game_id'):
        finish_conditions.append('f.game_uuid = %s')
        finish_arguments.append(cleaned_data['game_id'])

    player_only = bool(cleaned_data.get('player_name') and not cleaned_data.get('map_name'))
    limit = '' if player_only else f' LIMIT {SEARCH_LIMIT}'
    cursor.execute(
        f'SELECT {postgres_finish_columns()}, m.category FROM record_finish f '
        'JOIN record_map m ON m.map_id = f.map_id '
        'JOIN record_player p ON p.player_id = f.player_id '
        f"WHERE {' AND '.join(finish_conditions)} "
        f'ORDER BY f.finished_at DESC{limit}',
        finish_arguments,
    )
    finishes = rows_as_dicts(cursor)

    team_conditions = []
    team_arguments = []
    if cleaned_data.get('map_name'):
        team_conditions.append('m.name = %s')
        team_arguments.append(cleaned_data['map_name'])
    if cleaned_data.get('player_name'):
        team_conditions.append(
            'EXISTS (SELECT 1 FROM record_team_player selected_tp '
            'JOIN record_player selected_p ON selected_p.player_id = selected_tp.player_id '
            'WHERE selected_tp.team_id = t.team_id AND selected_p.name = %s)'
        )
        team_arguments.append(cleaned_data['player_name'])
    if cleaned_data.get('time') is not None:
        team_conditions.append('t.time_cs BETWEEN %s AND %s')
        centiseconds = round(cleaned_data['time'] * 100)
        team_arguments.extend((centiseconds - 1, centiseconds + 1))
    if cleaned_data.get('finished_on'):
        start = datetime.combine(cleaned_data['finished_on'], time.min)
        team_conditions.append('t.finished_at >= %s AND t.finished_at < %s')
        team_arguments.extend((start, start + timedelta(days=1)))
    if cleaned_data.get('game_id'):
        team_conditions.append('t.game_uuid = %s')
        team_arguments.append(cleaned_data['game_id'])

    cursor.execute(
        'SELECT t.team_id, t.map_id, m.name AS map_name, '
        't.time_cs AS time_value, t.finished_at, t.server, '
        't.game_uuid AS game_id, t.ddnet7, m.category FROM record_team t '
        'JOIN record_map m ON m.map_id = t.map_id '
        f"WHERE {' AND '.join(team_conditions)} "
        f'ORDER BY t.finished_at DESC{limit}',
        team_arguments,
    )
    team_rows = rows_as_dicts(cursor)

    linked_finishes = set()
    team_results = []
    for team_row in team_rows:
        cursor.execute(
            'SELECT tp.player_id, p.name AS player_name '
            'FROM record_team_player tp '
            'JOIN record_player p ON p.player_id = tp.player_id '
            'WHERE tp.team_id = %s ORDER BY p.name',
            (team_row['team_id'],),
        )
        members = rows_as_dicts(cursor)
        for member in members:
            linked_finishes.add((
                team_row['map_id'], member['player_id'], team_row['time_value'],
                team_row['finished_at'], team_row['game_id'],
            ))
        team_results.append({
            'kind': 'team',
            'kind_label': 'Team Rank',
            'impact': f'{len(members)} Ranks + 1 Team Rank',
            'map_name': team_row['map_name'],
            'players': ', '.join(member['player_name'] for member in members),
            'player_names': [member['player_name'] for member in members],
            'finished_at': team_row['finished_at'],
            'time_value': team_row['time_value'] / 100,
            'server': team_row['server'],
            'game_id': team_row['game_id'],
            'team_id': team_row['team_id'],
            'map_id': team_row['map_id'],
            'category': team_row['category'] or '',
            'stars': None,
            'target': {'kind': 'team', 'team_id': bytes(team_row['team_id']).hex()},
        })

    results = list(team_results)
    for row in finishes:
        linked_key = (
            row['map_id'], row['player_id'], row['time_value'],
            row['finished_at'], row['game_id'],
        )
        if linked_key in linked_finishes:
            continue
        results.append({
            'kind': 'rank',
            'kind_label': 'Normal Rank',
            'impact': '1 Rank',
            'map_name': row['map_name'],
            'players': row['player_name'],
            'player_names': [row['player_name']],
            'player_id': row['player_id'],
            'map_id': row['map_id'],
            'category': row['category'] or '',
            'stars': None,
            'finished_at': row['finished_at'],
            'time_value': row['time_value'] / 100,
            'server': row['server'],
            'game_id': row['game_id'],
            'has_checkpoints': has_checkpoint_values(row),
            'target': {
                'kind': 'rank',
                'map_id': row['map_id'],
                'player_id': row['player_id'],
                'time_value': row['time_value'],
                'finished_at': token_datetime(row['finished_at']),
                'server': row['server'],
            },
        })
    results.sort(key=lambda row: row['finished_at'], reverse=True)
    if player_only:
        results.sort(key=lambda row: (
            category_order(row['category']), row['map_name'].casefold(),
        ))
    else:
        results = results[:SEARCH_LIMIT]
    map_id = None if player_only else (
        team_rows[0]['map_id'] if team_rows else finishes[0]['map_id'] if finishes else None
    )
    if results and not player_only:
        add_postgresql_places(cursor, results, map_id)
    for row in results:
        row.pop('map_id', None)
    return results


def preview_target(target, lock=False, cursor=None):
    connection = database()
    if cursor is None:
        with connection.cursor() as own_cursor:
            return preview_target(target, lock=lock, cursor=own_cursor)
    if target['kind'] == 'selection':
        preview = preview_selection(cursor, target, lock)
        source = target.get('source', {})
        if source.get('kind') in ('checkpoint', 'checkpoint_group', 'leaderboard_checkpoint'):
            add_checkpoint_context(preview, source, connection.vendor)
        return preview
    if target['kind'] == 'leaderboard_checkpoint':
        if target.get('database_vendor') != connection.vendor:
            raise RankConflict('The score database changed. Run the checkpoint scan again.')
        preview = preview_leaderboard_checkpoint(cursor, target, lock, connection.vendor)
        add_checkpoint_context(preview, target, connection.vendor)
        return preview
    if target['kind'] in ('checkpoint', 'checkpoint_group'):
        if target.get('database_vendor') != connection.vendor:
            raise RankConflict('The score database changed. Run the checkpoint scan again.')
        if target['kind'] == 'checkpoint_group':
            preview = preview_checkpoint_group(cursor, target, lock, connection.vendor)
        elif connection.vendor == 'mysql':
            preview = preview_mysql_checkpoint(cursor, target, lock)
        else:
            preview = preview_postgresql_checkpoint(cursor, target, lock)
        add_checkpoint_context(preview, target, connection.vendor)
        return preview
    if target['kind'] == 'player':
        if connection.vendor == 'mysql':
            return preview_mysql_player(cursor, target['player_name'], lock)
        return preview_postgresql_player(cursor, target['player_name'], lock)
    if connection.vendor == 'mysql':
        return preview_mysql_target(cursor, target, lock)
    return preview_postgresql_target(cursor, target, lock)


def preview_leaderboard_checkpoint(cursor, target, lock, vendor):
    _, values = checkpoint_target_values(target)
    try:
        depth = int(target['leaderboard_depth'])
        minimum_players = int(target['minimum_players'])
        margin_ticks = int(target.get('checkpoint_margin', 0))
    except (KeyError, TypeError, ValueError) as error:
        raise RankGraveyardError('Invalid leaderboard checkpoint cluster.') from error
    if (
        not 1 <= depth <= 1000
        or not 2 <= minimum_players <= 100
        or margin_ticks not in (0, 1, 2, 5)
    ):
        raise RankGraveyardError('Invalid leaderboard checkpoint cluster.')
    cluster_target = dict(target)
    cluster_target.update({
        'kind': 'checkpoint',
        'player_name': None,
        'expected_row_count': None,
        'parallel_scan': True,
    })
    preview = (
        preview_mysql_checkpoint(cursor, cluster_target, lock)
        if vendor == 'mysql'
        else preview_postgresql_checkpoint(cursor, cluster_target, lock)
    )
    source_player = target.get('player_name')
    source_rank = target.get('source_rank')
    if not isinstance(source_rank, dict) or source_rank.get('kind') != 'rank':
        raise RankGraveyardError('Invalid leaderboard checkpoint cluster.')
    source_time = (
        source_rank['time_value'] / 100
        if source_rank.get('map_id')
        else source_rank['time_value']
    )
    for entry in preview['entries']:
        entry['source_match'] = (
            source_player in entry['player_names']
            and entry['time_seconds'] == source_time
            and token_datetime(entry['finished_at']) == source_rank['finished_at']
        )
    if not any(entry['source_match'] for entry in preview['entries']):
        raise RankConflict('The rank which matched this cluster is no longer available.')
    label = checkpoint_label(target['start_checkpoint'], target['end_checkpoint'])
    signature = checkpoint_signature(values, vendor)
    preview['target'] = (
        f"{target['map_name']} / Suspicious Cluster / {label} / {signature}"[:255]
    )
    preview['source_player'] = source_player
    preview['checkpoint_margin'] = checkpoint_margin_label(margin_ticks)
    return preview


def preview_selection(cursor, target, lock):
    source = target.get('source')
    targets = target.get('targets')
    if (
        not isinstance(source, dict)
        or source.get('kind') == 'selection'
        or not isinstance(targets, list)
        or not targets
    ):
        raise RankGraveyardError('Invalid rank selection.')
    keys = [target_key(item) for item in targets if isinstance(item, dict)]
    if len(keys) != len(targets) or len(set(keys)) != len(keys):
        raise RankGraveyardError('Invalid rank selection.')
    if source.get('kind') == 'player':
        previews = [preview_target(item, lock=lock, cursor=cursor) for item in targets]
        player_name = source.get('player_name')
        if not player_name or any(
            player_name not in entry['player_names']
            for preview in previews
            for entry in preview['entries']
        ):
            raise RankGraveyardError('A selected rank does not belong to this review.')
        source_type = 'player'
        source_label = player_name
    else:
        source_preview = preview_target(
            source,
            lock=lock and source.get('kind') != 'leaderboard_checkpoint',
            cursor=cursor,
        )
        available = {target_key(entry['target']) for entry in source_preview['entries']}
        if not set(keys).issubset(available):
            raise RankGraveyardError('A selected rank does not belong to this review.')
        previews = [preview_target(item, lock=lock, cursor=cursor) for item in targets]
        source_type = source_preview['target_type']
        source_label = source_preview['target']
    finishes = {}
    teams = {}
    missing = {}
    for preview in previews:
        finishes.update({finish_key(row): row for row in preview['finishes']})
        teams.update({target_key(team_target(team)): team for team in preview['teams']})
        missing.update({target_key(rank_target(row)): row for row in preview['missing']})
    return build_preview(
        source_type, source_label,
        list(finishes.values()), list(teams.values()), list(missing.values()),
    )


def checkpoint_target_values(target):
    columns = checkpoint_columns(
        target.get('start_checkpoint'), target.get('end_checkpoint')
    )
    values = target.get('values')
    if not isinstance(values, list) or len(values) != len(columns):
        raise RankGraveyardError('Invalid checkpoint cluster.')
    return columns, values


def preview_checkpoint_group(cursor, target, lock, vendor):
    clusters = target.get('clusters')
    if not isinstance(clusters, list) or not 2 <= len(clusters) <= SCAN_LIMIT:
        raise RankGraveyardError('Invalid associated checkpoint group.')
    first = clusters[0]
    expected = (
        first.get('map_name'), first.get('start_checkpoint'),
        first.get('end_checkpoint'),
        first.get('player_name'), first.get('database_vendor'),
    )
    for cluster in clusters:
        values = (
            cluster.get('map_name'), cluster.get('start_checkpoint'),
            cluster.get('end_checkpoint'),
            cluster.get('player_name'), cluster.get('database_vendor'),
        )
        if cluster.get('kind') != 'checkpoint' or values != expected:
            raise RankGraveyardError('Invalid associated checkpoint group.')
    previews = [
        preview_mysql_checkpoint(cursor, cluster, lock)
        if vendor == 'mysql'
        else preview_postgresql_checkpoint(cursor, cluster, lock)
        for cluster in clusters
    ]
    finishes = {}
    teams = {}
    missing = {}
    for preview in previews:
        finishes.update({finish_key(row): row for row in preview['finishes']})
        teams.update({target_key(team_target(team)): team for team in preview['teams']})
        missing.update({target_key(row): row for row in preview['missing']})
    label = checkpoint_label(first['start_checkpoint'], first['end_checkpoint'])
    selected_player = f" / {first['player_name']}" if first.get('player_name') else ''
    return build_preview(
        'checkpoint',
        f"{first['map_name']}{selected_player} / {len(clusters)} Associated Clusters / {label}"[:255],
        list(finishes.values()), list(teams.values()), list(missing.values()),
    )


def preview_mysql_checkpoint(cursor, target, lock):
    columns, values = checkpoint_target_values(target)
    margin_ticks = int(target.get('checkpoint_margin', 0))
    if margin_ticks not in (0, 1, 2, 5):
        raise RankGraveyardError('Invalid checkpoint margin.')
    margin = checkpoint_margin_value(margin_ticks, 'mysql')
    suffix = ' FOR UPDATE' if lock else ''
    checkpoint_where = ' AND '.join(
        f'ABS(CAST(r.{column} AS DOUBLE) - %s) <= %s' for column in columns
    )
    comparisons = [item for value in values for item in (value, margin)]
    arguments = [target['map_name'], *comparisons]
    if target.get('player_name'):
        checkpoint_where += ' AND r.Name = %s'
        arguments.append(target['player_name'])
    if target.get('parallel_scan') and not lock:
        finishes = parallel_mysql_checkpoint_finishes(
            cursor, target['map_name'], checkpoint_where, arguments[1:]
        )
    else:
        cursor.execute(
            f'SELECT {mysql_finish_columns()} FROM record_race r '
            f'WHERE r.Map = %s AND {checkpoint_where}' + suffix,
            arguments,
        )
        finishes = {finish_key(row): row for row in rows_as_dicts(cursor)}
    if not finishes:
        raise RankNotFound('The checkpoint cluster no longer exists.')
    expected_row_count = target.get('expected_row_count')
    if expected_row_count is not None and len(finishes) != expected_row_count:
        raise RankConflict('The checkpoint cluster changed. Run the scan again.')

    candidate_where = ' AND '.join(
        f'ABS(CAST(candidate.{column} AS DOUBLE) - %s) <= %s' for column in columns
    )
    if target.get('player_name'):
        candidate_where += ' AND candidate.Name = %s'
    team_join = (
        'FROM record_teamrace t JOIN record_teamrace selected '
        'ON t.Map = selected.Map AND t.Timestamp = selected.Timestamp '
        'AND t.Time = selected.Time AND t.ID = selected.ID '
        'AND t.GameID <=> selected.GameID '
        'AND COALESCE(t.DDNet7, 0) = COALESCE(selected.DDNet7, 0) '
        'JOIN record_race candidate ON candidate.Map = selected.Map '
        'AND candidate.Name = selected.Name AND candidate.Timestamp = selected.Timestamp '
        'AND candidate.Time = selected.Time AND candidate.GameID <=> selected.GameID '
        'AND COALESCE(candidate.DDNet7, 0) = COALESCE(selected.DDNet7, 0) '
        f'WHERE candidate.Map = %s AND {candidate_where}'
    )
    cursor.execute(
        'SELECT t.Map AS map_name, t.Name AS player_name, '
        't.Timestamp AS finished_at, CAST(t.Time AS DOUBLE) AS time_value, '
        't.ID AS team_id, t.GameID AS game_id, '
        'COALESCE(t.DDNet7, 0) AS ddnet7 ' + team_join + suffix,
        arguments,
    )
    teams_by_key = {}
    for row in rows_as_dicts(cursor):
        team = teams_by_key.setdefault(mysql_team_key(row), {'rows': {}, 'members': set()})
        team['rows'][row['player_name']] = row
        team['members'].add(row['player_name'])

    cursor.execute(
        f'SELECT {mysql_finish_columns("r")}, '
        't.Map AS linked_map, t.Timestamp AS linked_at, '
        'CAST(t.Time AS DOUBLE) AS linked_time, '
        't.ID AS linked_team_id, t.GameID AS linked_game_id, '
        'COALESCE(t.DDNet7, 0) AS linked_ddnet7, t.Name AS linked_member '
        'FROM record_race r JOIN record_teamrace t '
        'ON r.Map = t.Map AND r.Name = t.Name AND r.Timestamp = t.Timestamp '
        'AND r.Time = t.Time AND r.GameID <=> t.GameID '
        'AND COALESCE(r.DDNet7, 0) = COALESCE(t.DDNet7, 0) '
        'JOIN record_teamrace selected ON t.Map = selected.Map '
        'AND t.Timestamp = selected.Timestamp AND t.Time = selected.Time '
        'AND t.ID = selected.ID AND t.GameID <=> selected.GameID '
        'AND COALESCE(t.DDNet7, 0) = COALESCE(selected.DDNet7, 0) '
        'JOIN record_race candidate ON candidate.Map = selected.Map '
        'AND candidate.Name = selected.Name AND candidate.Timestamp = selected.Timestamp '
        'AND candidate.Time = selected.Time AND candidate.GameID <=> selected.GameID '
        'AND COALESCE(candidate.DDNet7, 0) = COALESCE(selected.DDNet7, 0) '
        f'WHERE candidate.Map = %s AND {candidate_where}' + suffix,
        arguments,
    )
    matches = {}
    finish_teams = {}
    for row in rows_as_dicts(cursor):
        team_key = (
            row.pop('linked_map'), row.pop('linked_at'), row.pop('linked_time'),
            bytes(row.pop('linked_team_id')), row.pop('linked_game_id'),
            bool(row.pop('linked_ddnet7')),
        )
        member = row.pop('linked_member')
        finish_teams.setdefault(finish_key(row), set()).add(team_key)
        matches.setdefault((team_key, member), {})[finish_key(row)] = row
    if any(len(team_keys) > 1 for team_keys in finish_teams.values()):
        raise RankAmbiguous('A race row matches more than one team finish.')

    teams = []
    missing = []
    for team_key, team in teams_by_key.items():
        team_rows = list(team['rows'].values())
        for member in team_rows:
            member_matches = list(matches.get((team_key, member['player_name']), {}).values())
            if len(member_matches) > 1:
                raise RankAmbiguous(
                    f"The team member {member['player_name']} matches more than one race row."
                )
            if member_matches:
                finishes[finish_key(member_matches[0])] = member_matches[0]
            else:
                missing.append(missing_finish(member))
        teams.append({'rows': team_rows, 'members': sorted(team['members'])})
    label = checkpoint_label(target['start_checkpoint'], target['end_checkpoint'])
    signature = checkpoint_signature(values, 'mysql')
    selected_player = f" / {target['player_name']}" if target.get('player_name') else ''
    return build_preview(
        'checkpoint', f"{target['map_name']}{selected_player} / {label} / {signature}"[:255],
        list(finishes.values()), teams, missing,
    )


def parallel_mysql_checkpoint_finishes(cursor, map_name, checkpoint_where, values):
    cursor.execute(
        'SELECT COUNT(*) FROM record_race FORCE INDEX (idx_map_time_name) '
        'WHERE Map = %s',
        (map_name,),
    )
    row_count = cursor.fetchone()[0]
    if not row_count:
        return {}

    bounds = []
    for offset in (row_count // 4, row_count // 2, row_count * 3 // 4):
        cursor.execute(
            'SELECT Time FROM record_race FORCE INDEX (idx_map_time_name) '
            'WHERE Map = %s ORDER BY Time, Name LIMIT 1 OFFSET %s',
            (map_name, offset),
        )
        bounds.append(float(cursor.fetchone()[0]))
    time_ranges = [
        (None, bounds[0]),
        (bounds[0], bounds[1]),
        (bounds[1], bounds[2]),
        (bounds[2], None),
    ]

    def find_range(time_range):
        low, high = time_range
        where = checkpoint_where
        arguments = [map_name, *values]
        if low is not None:
            where += ' AND r.Time >= %s'
            arguments.append(low)
        if high is not None:
            where += ' AND r.Time < %s'
            arguments.append(high)
        connection = connections['ddnet_db']
        try:
            with connection.cursor() as range_cursor:
                range_cursor.execute(
                    f'SELECT {mysql_finish_columns()} FROM record_race r '
                    'FORCE INDEX (idx_map_time_name) '
                    f'WHERE r.Map = %s AND {where}',
                    arguments,
                )
                return rows_as_dicts(range_cursor)
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = [row for batch in pool.map(find_range, time_ranges) for row in batch]
    return {finish_key(row): row for row in rows}


def preview_postgresql_checkpoint(cursor, target, lock):
    columns, values = checkpoint_target_values(target)
    margin_ticks = int(target.get('checkpoint_margin', 0))
    if margin_ticks not in (0, 1, 2, 5):
        raise RankGraveyardError('Invalid checkpoint margin.')
    margin = checkpoint_margin_value(margin_ticks, 'postgresql')
    if margin_ticks:
        checkpoint_where = ' AND '.join(
            f'ABS(({postgres_checkpoint_value_expression(int(column[2:]))}) - %s) <= %s'
            for column in columns
        )
        checkpoint_arguments = [item for value in values for item in (value, margin)]
    else:
        key = postgres_checkpoint_expression(
            target['start_checkpoint'], target['end_checkpoint']
        )
        checkpoint_where = f'{key} = %s'
        checkpoint_arguments = [struct.pack(f'<{len(columns)}i', *values)]
    suffix = ' FOR UPDATE OF f' if lock else ''
    player_where = ' AND p.name = %s' if target.get('player_name') else ''
    arguments = [target['map_name'], *checkpoint_arguments]
    if target.get('player_name'):
        arguments.append(target['player_name'])
    cursor.execute(
        f'SELECT {postgres_finish_columns()} FROM record_finish f '
        'JOIN record_map m ON m.map_id = f.map_id '
        'JOIN record_player p ON p.player_id = f.player_id '
        f'WHERE m.name = %s AND {checkpoint_where}' + player_where + suffix,
        arguments,
    )
    finishes = {finish_key(row): row for row in rows_as_dicts(cursor)}
    if not finishes:
        raise RankNotFound('The checkpoint cluster no longer exists.')
    expected_row_count = target.get('expected_row_count')
    if expected_row_count is not None and len(finishes) != expected_row_count:
        raise RankConflict('The checkpoint cluster changed. Run the scan again.')

    cursor.execute(
        'SELECT DISTINCT t.team_id FROM record_team t '
        'JOIN record_team_player tp ON tp.team_id = t.team_id '
        'JOIN record_finish f ON f.map_id = t.map_id AND f.player_id = tp.player_id '
        'AND f.time_cs = t.time_cs AND f.finished_at = t.finished_at '
        'AND f.game_uuid IS NOT DISTINCT FROM t.game_uuid '
        'JOIN record_map m ON m.map_id = f.map_id '
        'JOIN record_player p ON p.player_id = f.player_id '
        f'WHERE m.name = %s AND {checkpoint_where}' + player_where,
        arguments,
    )
    team_ids = [bytes(row[0]) for row in cursor.fetchall()]
    teams = []
    missing = []
    finish_teams = {}
    for team_id in team_ids:
        preview = preview_postgresql_team(
            cursor, {'kind': 'team', 'team_id': team_id.hex()}, lock
        )
        for row in preview['finishes']:
            finish_teams.setdefault(finish_key(row), set()).add(team_id)
            finishes[finish_key(row)] = row
        teams.extend(preview['teams'])
        missing.extend(preview['missing'])
    if any(len(team_ids) > 1 for team_ids in finish_teams.values()):
        raise RankAmbiguous('A race row matches more than one team finish.')
    label = checkpoint_label(target['start_checkpoint'], target['end_checkpoint'])
    signature = checkpoint_signature(values, 'postgresql')
    selected_player = f" / {target['player_name']}" if target.get('player_name') else ''
    return build_preview(
        'checkpoint', f"{target['map_name']}{selected_player} / {label} / {signature}"[:255],
        list(finishes.values()), teams, missing,
    )


def preview_mysql_target(cursor, target, lock):
    suffix = ' FOR UPDATE' if lock else ''
    if target['kind'] == 'rank':
        cursor.execute(
            f'SELECT {mysql_finish_columns()} FROM record_race r '
            'WHERE Map = %s AND Name = %s AND Timestamp = %s '
            'AND Time = %s AND Server = %s' + suffix,
            (
                target['map_name'], target['player_name'],
                parse_datetime(target['finished_at']), target['time_value'],
                target['server'],
            ),
        )
        finishes = rows_as_dicts(cursor)
        if len(finishes) != 1:
            raise RankNotFound('The selected rank no longer exists.')
        finish = finishes[0]
        cursor.execute(
            'SELECT Map AS map_name, Timestamp AS finished_at, '
            'CAST(Time AS DOUBLE) AS time_value, '
            'ID AS team_id, GameID AS game_id, COALESCE(DDNet7, 0) AS ddnet7 '
            'FROM record_teamrace WHERE Map = %s AND Name = %s '
            'AND Timestamp = %s AND Time = %s AND GameID <=> %s '
            'AND COALESCE(DDNet7, 0) = %s' + suffix,
            (
                finish['map_name'], finish['player_name'], finish['finished_at'],
                finish['time_value'], finish['game_id'], finish['ddnet7'],
            ),
        )
        linked = rows_as_dicts(cursor)
        signatures = {mysql_team_key(row): row for row in linked}
        if len(signatures) > 1:
            raise RankAmbiguous('This rank matches more than one team finish.')
        if signatures:
            row = next(iter(signatures.values()))
            team_target = {
                'kind': 'team',
                'map_name': row['map_name'],
                'finished_at': token_datetime(row['finished_at']),
                'time_value': row['time_value'],
                'team_id': bytes(row['team_id']).hex(),
                'game_id': row['game_id'],
                'ddnet7': bool(row['ddnet7']),
            }
            return preview_mysql_team(cursor, team_target, lock)
        return build_preview('rank', f"{finish['map_name']} / {finish['player_name']}", finishes, [], [])
    return preview_mysql_team(cursor, target, lock)


def preview_mysql_team(cursor, target, lock):
    suffix = ' FOR UPDATE' if lock else ''
    arguments = (
        target['map_name'], parse_datetime(target['finished_at']),
        target['time_value'], bytes.fromhex(target['team_id']),
        target.get('game_id'), bool(target.get('ddnet7')),
    )
    cursor.execute(
        'SELECT Map AS map_name, Name AS player_name, Timestamp AS finished_at, '
        'CAST(Time AS DOUBLE) AS time_value, ID AS team_id, GameID AS game_id, '
        'COALESCE(DDNet7, 0) AS ddnet7 FROM record_teamrace '
        'WHERE Map = %s AND Timestamp = %s AND Time = %s AND ID = %s '
        'AND GameID <=> %s AND COALESCE(DDNet7, 0) = %s' + suffix,
        arguments,
    )
    team_rows = rows_as_dicts(cursor)
    if not team_rows:
        raise RankNotFound('The selected teamrank no longer exists.')

    finishes = []
    missing = []
    for member in team_rows:
        cursor.execute(
            f'SELECT {mysql_finish_columns()} FROM record_race r '
            'WHERE Map = %s AND Name = %s AND Timestamp = %s AND Time = %s '
            'AND GameID <=> %s AND COALESCE(DDNet7, 0) = %s' + suffix,
            (
                member['map_name'], member['player_name'], member['finished_at'],
                member['time_value'], member['game_id'], member['ddnet7'],
            ),
        )
        matches = rows_as_dicts(cursor)
        if len(matches) > 1:
            raise RankAmbiguous(
                f"The team member {member['player_name']} matches more than one race row."
            )
        if matches:
            finishes.append(matches[0])
        else:
            missing.append(missing_finish(member))
    roster = ', '.join(sorted(row['player_name'] for row in team_rows))
    team = {'rows': team_rows, 'members': sorted(row['player_name'] for row in team_rows)}
    return build_preview('team', f"{team_rows[0]['map_name']} / {roster}", finishes, [team], missing)


def preview_mysql_player(cursor, player_name, lock, map_name=None):
    suffix = ' FOR UPDATE' if lock else ''
    map_filter = ' AND r.Map = %s' if map_name else ''
    arguments = (player_name, map_name) if map_name else (player_name,)
    cursor.execute(
        f'SELECT {mysql_finish_columns()} FROM record_race r '
        'WHERE r.Name = %s' + map_filter + suffix,
        arguments,
    )
    finishes = {finish_key(row): row for row in rows_as_dicts(cursor)}

    cursor.execute(
        'SELECT t.Map AS map_name, t.Name AS player_name, '
        't.Timestamp AS finished_at, CAST(t.Time AS DOUBLE) AS time_value, '
        't.ID AS team_id, '
        't.GameID AS game_id, COALESCE(t.DDNet7, 0) AS ddnet7 '
        'FROM record_teamrace t JOIN record_teamrace selected '
        'ON t.Map = selected.Map AND t.Timestamp = selected.Timestamp '
        'AND t.Time = selected.Time AND t.ID = selected.ID '
        'AND t.GameID <=> selected.GameID '
        'AND COALESCE(t.DDNet7, 0) = COALESCE(selected.DDNet7, 0) '
        'WHERE selected.Name = %s'
        + (' AND selected.Map = %s' if map_name else '') + suffix,
        arguments,
    )
    team_rows = rows_as_dicts(cursor)
    teams_by_key = {}
    for row in team_rows:
        team = teams_by_key.setdefault(mysql_team_key(row), {'rows': [], 'members': []})
        if row['player_name'] not in team['members']:
            team['rows'].append(row)
            team['members'].append(row['player_name'])

    cursor.execute(
        f'SELECT {mysql_finish_columns("r")}, '
        't.Map AS linked_map, t.Timestamp AS linked_at, '
        'CAST(t.Time AS DOUBLE) AS linked_time, '
        't.ID AS linked_team_id, t.GameID AS linked_game_id, '
        'COALESCE(t.DDNet7, 0) AS linked_ddnet7, t.Name AS linked_member '
        'FROM record_race r JOIN record_teamrace t '
        'ON r.Map = t.Map AND r.Name = t.Name AND r.Timestamp = t.Timestamp '
        'AND r.Time = t.Time AND r.GameID <=> t.GameID '
        'AND COALESCE(r.DDNet7, 0) = COALESCE(t.DDNet7, 0) '
        'JOIN record_teamrace selected ON t.Map = selected.Map '
        'AND t.Timestamp = selected.Timestamp AND t.Time = selected.Time '
        'AND t.ID = selected.ID AND t.GameID <=> selected.GameID '
        'AND COALESCE(t.DDNet7, 0) = COALESCE(selected.DDNet7, 0) '
        'WHERE selected.Name = %s'
        + (' AND selected.Map = %s' if map_name else '') + suffix,
        arguments,
    )
    linked = rows_as_dicts(cursor)
    matches = {}
    finish_teams = {}
    for row in linked:
        team_key = (
            row.pop('linked_map'), row.pop('linked_at'), row.pop('linked_time'),
            bytes(row.pop('linked_team_id')), row.pop('linked_game_id'),
            bool(row.pop('linked_ddnet7')),
        )
        member = row.pop('linked_member')
        finish_teams.setdefault(finish_key(row), set()).add(team_key)
        matches.setdefault((team_key, member), []).append(row)

    if any(len(team_keys) > 1 for team_keys in finish_teams.values()):
        raise RankAmbiguous('A race row matches more than one team finish.')

    missing = []
    for team_key, team in teams_by_key.items():
        for member in team['rows']:
            member_matches = matches.get((team_key, member['player_name']), [])
            if len(member_matches) > 1:
                raise RankAmbiguous(
                    f"The team member {member['player_name']} matches more than one race row."
                )
            if member_matches:
                row = member_matches[0]
                finishes[finish_key(row)] = row
            else:
                missing.append(missing_finish(member))
        team['members'].sort()
    return build_preview('player', player_name, list(finishes.values()), list(teams_by_key.values()), missing)


def preview_postgresql_target(cursor, target, lock):
    suffix = ' FOR UPDATE OF f' if lock else ''
    if target['kind'] == 'rank':
        cursor.execute(
            f'SELECT {postgres_finish_columns()} FROM record_finish f '
            'JOIN record_map m ON m.map_id = f.map_id '
            'JOIN record_player p ON p.player_id = f.player_id '
            'WHERE f.map_id = %s AND f.player_id = %s AND f.time_cs = %s '
            'AND f.finished_at = %s AND f.server = %s' + suffix,
            (
                target['map_id'], target['player_id'], target['time_value'],
                parse_datetime(target['finished_at']), target['server'],
            ),
        )
        finishes = rows_as_dicts(cursor)
        if len(finishes) != 1:
            raise RankNotFound('The selected rank no longer exists.')
        finish = finishes[0]
        cursor.execute(
            'SELECT t.team_id FROM record_team t '
            'JOIN record_team_player tp ON tp.team_id = t.team_id '
            'WHERE t.map_id = %s AND tp.player_id = %s AND t.time_cs = %s '
            'AND t.finished_at = %s AND t.game_uuid IS NOT DISTINCT FROM %s',
            (
                finish['map_id'], finish['player_id'], finish['time_value'],
                finish['finished_at'], finish['game_id'],
            ),
        )
        team_ids = [bytes(row[0]) for row in cursor.fetchall()]
        if len(team_ids) > 1:
            raise RankAmbiguous('This rank matches more than one team finish.')
        if team_ids:
            return preview_postgresql_team(cursor, {'kind': 'team', 'team_id': team_ids[0].hex()}, lock)
        return build_preview('rank', f"{finish['map_name']} / {finish['player_name']}", finishes, [], [])
    return preview_postgresql_team(cursor, target, lock)


def preview_postgresql_team(cursor, target, lock):
    suffix = ' FOR UPDATE OF t, tp' if lock else ''
    cursor.execute(
        'SELECT t.team_id, t.map_id, m.name AS map_name, t.roster_hash, '
        't.time_cs AS time_value, t.finished_at, t.server, '
        't.game_uuid AS game_id, t.member_count, t.ddnet7, '
        'tp.player_id, p.name AS player_name FROM record_team t '
        'JOIN record_map m ON m.map_id = t.map_id '
        'JOIN record_team_player tp ON tp.team_id = t.team_id '
        'JOIN record_player p ON p.player_id = tp.player_id '
        'WHERE t.team_id = %s' + suffix,
        (bytes.fromhex(target['team_id']),),
    )
    member_rows = rows_as_dicts(cursor)
    if not member_rows:
        raise RankNotFound('The selected teamrank no longer exists.')
    team = postgres_team(member_rows)
    finishes, missing = postgresql_team_finishes(cursor, team, lock)
    roster = ', '.join(member['player_name'] for member in team['members'])
    return build_preview('team', f"{team['map_name']} / {roster}", finishes, [team], missing)


def preview_postgresql_player(cursor, player_name, lock, map_name=None):
    finish_suffix = ' FOR UPDATE OF f' if lock else ''
    arguments = (player_name, map_name) if map_name else (player_name,)
    cursor.execute(
        f'SELECT {postgres_finish_columns()} FROM record_finish f '
        'JOIN record_map m ON m.map_id = f.map_id '
        'JOIN record_player p ON p.player_id = f.player_id '
        'WHERE p.name = %s' + (' AND m.name = %s' if map_name else '') + finish_suffix,
        arguments,
    )
    finishes = {finish_key(row): row for row in rows_as_dicts(cursor)}

    team_suffix = ' FOR UPDATE OF t, tp' if lock else ''
    cursor.execute(
        'SELECT t.team_id, t.map_id, m.name AS map_name, t.roster_hash, '
        't.time_cs AS time_value, t.finished_at, t.server, '
        't.game_uuid AS game_id, t.member_count, t.ddnet7, '
        'tp.player_id, p.name AS player_name FROM record_team t '
        'JOIN record_map m ON m.map_id = t.map_id '
        'JOIN record_team_player selected_tp ON selected_tp.team_id = t.team_id '
        'JOIN record_player selected_p ON selected_p.player_id = selected_tp.player_id '
        'JOIN record_team_player tp ON tp.team_id = t.team_id '
        'JOIN record_player p ON p.player_id = tp.player_id '
        'WHERE selected_p.name = %s'
        + (' AND m.name = %s' if map_name else '') + team_suffix,
        arguments,
    )
    rows = rows_as_dicts(cursor)
    grouped = {}
    for row in rows:
        grouped.setdefault(bytes(row['team_id']), []).append(row)

    teams = []
    missing = []
    finish_teams = {}
    for member_rows in grouped.values():
        team = postgres_team(member_rows)
        linked, team_missing = postgresql_team_finishes(cursor, team, lock)
        for row in linked:
            finish_teams.setdefault(finish_key(row), set()).add(team['team_id'])
            finishes[finish_key(row)] = row
        missing.extend(team_missing)
        teams.append(team)
    if any(len(team_ids) > 1 for team_ids in finish_teams.values()):
        raise RankAmbiguous('A race row matches more than one team finish.')
    return build_preview('player', player_name, list(finishes.values()), teams, missing)


def preview_player_overview(player_name):
    connection = database()
    with connection.cursor() as cursor:
        if connection.vendor == 'mysql':
            cursor.execute(
                'SELECT Map AS map_name, COUNT(*) AS finish_count '
                'FROM record_race WHERE Name = %s GROUP BY Map',
                (player_name,),
            )
            finishes = rows_as_dicts(cursor)
            cursor.execute(
                'SELECT map_name, COUNT(*) AS team_count FROM ('
                'SELECT Map AS map_name FROM record_teamrace WHERE Name = %s '
                'GROUP BY Map, Timestamp, Time, ID, GameID, COALESCE(DDNet7, 0)'
                ') player_teams GROUP BY map_name',
                (player_name,),
            )
        else:
            cursor.execute(
                'SELECT m.name AS map_name, COUNT(*) AS finish_count '
                'FROM record_finish f JOIN record_map m ON m.map_id = f.map_id '
                'JOIN record_player p ON p.player_id = f.player_id '
                'WHERE p.name = %s GROUP BY m.name',
                (player_name,),
            )
            finishes = rows_as_dicts(cursor)
            cursor.execute(
                'SELECT m.name AS map_name, COUNT(*) AS team_count '
                'FROM record_team t JOIN record_map m ON m.map_id = t.map_id '
                'JOIN record_team_player tp ON tp.team_id = t.team_id '
                'JOIN record_player p ON p.player_id = tp.player_id '
                'WHERE p.name = %s GROUP BY m.name',
                (player_name,),
            )
        teams = rows_as_dicts(cursor)

    groups = {
        row['map_name']: {
            'map_name': row['map_name'],
            'finish_count': row['finish_count'],
            'team_count': 0,
        }
        for row in finishes
    }
    for row in teams:
        group = groups.setdefault(row['map_name'], {
            'map_name': row['map_name'], 'finish_count': 0, 'team_count': 0,
        })
        group['team_count'] = row['team_count']
    map_groups = sorted(groups.values(), key=lambda row: row['map_name'].casefold())
    for group in map_groups:
        group['record_count'] = group['finish_count'] + group['team_count']
        group['individual_preview'] = group['record_count'] <= PLAYER_MAP_PREVIEW_LIMIT
    return {
        'target_type': 'player',
        'target': player_name,
        'player_scope': True,
        'player_map_groups': map_groups,
        'finish_count': sum(row['finish_count'] for row in map_groups),
        'team_count': sum(row['team_count'] for row in map_groups),
        'entries': [],
        'finishes': [],
        'teams': [],
        'missing': [],
    }


def preview_player_map(player_name, map_name):
    overview = preview_player_overview(player_name)
    group = next(
        (item for item in overview['player_map_groups'] if item['map_name'] == map_name),
        None,
    )
    if group is None:
        raise RankNotFound('The selected player has no ranks on this map.')
    if not group['individual_preview']:
        raise RankGraveyardError('This map has too many ranks for an inline review.')
    connection = database()
    with connection.cursor() as cursor:
        if connection.vendor == 'mysql':
            return preview_mysql_player(cursor, player_name, False, map_name)
        return preview_postgresql_player(cursor, player_name, False, map_name)


def postgres_team(member_rows):
    first = member_rows[0]
    members = [
        {'player_id': row['player_id'], 'player_name': row['player_name']}
        for row in member_rows
    ]
    members.sort(key=lambda row: row['player_name'])
    return {
        'team_id': bytes(first['team_id']),
        'map_id': first['map_id'],
        'map_name': first['map_name'],
        'roster_hash': bytes(first['roster_hash']),
        'time_value': first['time_value'],
        'finished_at': first['finished_at'],
        'server': first['server'],
        'game_id': first['game_id'],
        'member_count': first['member_count'],
        'ddnet7': bool(first['ddnet7']),
        'members': members,
    }


def postgresql_team_finishes(cursor, team, lock):
    suffix = ' FOR UPDATE OF f' if lock else ''
    finishes = []
    missing = []
    for member in team['members']:
        cursor.execute(
            f'SELECT {postgres_finish_columns()} FROM record_finish f '
            'JOIN record_map m ON m.map_id = f.map_id '
            'JOIN record_player p ON p.player_id = f.player_id '
            'WHERE f.map_id = %s AND f.player_id = %s AND f.time_cs = %s '
            'AND f.finished_at = %s AND f.game_uuid IS NOT DISTINCT FROM %s' + suffix,
            (
                team['map_id'], member['player_id'], team['time_value'],
                team['finished_at'], team['game_id'],
            ),
        )
        matches = rows_as_dicts(cursor)
        if len(matches) > 1:
            raise RankAmbiguous(
                f"The team member {member['player_name']} matches more than one race row."
            )
        if matches:
            finishes.append(matches[0])
        else:
            missing.append({
                'map_name': team['map_name'],
                'player_name': member['player_name'],
                'finished_at': token_datetime(team['finished_at']),
                'time_value': team['time_value'] / 100,
                'game_id': team['game_id'],
            })
    return finishes, missing


def missing_finish(row):
    return {
        'map_name': row['map_name'],
        'player_name': row['player_name'],
        'finished_at': token_datetime(row['finished_at']),
        'time_value': row['time_value'],
        'game_id': row['game_id'],
    }


def finish_checkpoint_times(row):
    if row.get('cp_times') is not None:
        values = [value / 100 for value in struct.unpack('<25i', bytes(row['cp_times']))]
    else:
        values = [float(row.get(f'cp{number}') or 0) for number in range(1, 26)]
    return [
        {'label': f'CP{number}', 'time': value}
        for number, value in enumerate(values, start=1)
        if value > 0
    ]


def has_checkpoint_values(row):
    if row.get('cp_times') is not None:
        return any(value > 0 for value in struct.unpack('<25i', bytes(row['cp_times'])))
    return any(float(row.get(f'cp{number}') or 0) > 0 for number in range(1, 26))


def checkpoint_rows(finishes):
    return [
        {
            'row_key': finish_key(row),
            'player_name': row['player_name'],
            'checkpoints': finish_checkpoint_times(row),
        }
        for row in finishes
    ]


def add_checkpoint_context(preview, target, vendor):
    clusters = target.get('clusters', [target])
    first = clusters[0]
    columns = checkpoint_columns(
        first.get('start_checkpoint'), first.get('end_checkpoint')
    )
    signatures = []
    for number, cluster in enumerate(clusters, start=1):
        _, values = checkpoint_target_values(cluster)
        margin_ticks = int(cluster.get('checkpoint_margin', 0))
        if margin_ticks not in (0, 1, 2, 5):
            raise RankGraveyardError('Invalid checkpoint margin.')
        signatures.append({
            'number': number,
            'values': values,
            'text': checkpoint_signature(values, vendor),
            'margin': checkpoint_margin_label(margin_ticks),
            'margin_value': checkpoint_margin_value(margin_ticks, vendor),
        })

    matches_by_key = {}
    selected_labels = {column.upper() for column in columns}
    for finish in preview['finishes']:
        actual = leaderboard_checkpoint_values(finish, columns, vendor)
        matches_by_key[finish_key(finish)] = [
            signature for signature in signatures
            if all(actual) and all(
                abs(actual_value - expected_value) <= signature['margin_value']
                for actual_value, expected_value in zip(actual, signature['values'])
            )
        ]
    for entry in preview['entries']:
        entry_matches = set()
        for checkpoint_row in entry['checkpoint_rows']:
            checkpoint_row['signature_matches'] = matches_by_key.get(
                checkpoint_row['row_key'], []
            )
            entry_matches.update(
                signature['number']
                for signature in checkpoint_row['signature_matches']
            )
            for checkpoint in checkpoint_row['checkpoints']:
                checkpoint['selected'] = checkpoint['label'] in selected_labels
        entry['checkpoint_signature_numbers'] = sorted(entry_matches)

    preview['checkpoint_label'] = checkpoint_label(
        first['start_checkpoint'], first['end_checkpoint']
    )
    preview['checkpoint_signatures'] = signatures
    if len(signatures) > 1:
        groups = [
            {
                'heading': f'Cluster {signature["number"]}',
                'note': (
                    f'Signature {signature["number"]}: {signature["text"]}. '
                    f'Margin: {signature["margin"]}.'
                ),
                'entries': [],
            }
            for signature in signatures
        ]
        connecting = {
            'heading': 'Ranks Connecting Multiple Clusters',
            'note': 'These Ranks Contain Race Rows Matching More Than One Cluster Signature.',
            'entries': [],
        }
        linked = {
            'heading': 'Team-Linked Ranks',
            'note': 'These ranks are included through a linked team rank and do not directly match a signature.',
            'entries': [],
        }
        for entry in preview['entries']:
            numbers = entry['checkpoint_signature_numbers']
            if len(numbers) == 1:
                groups[numbers[0] - 1]['entries'].append(entry)
            elif numbers:
                connecting['entries'].append(entry)
            else:
                linked['entries'].append(entry)
        preview['checkpoint_entry_groups'] = groups + [
            group for group in (connecting, linked) if group['entries']
        ]


def build_preview(target_type, target, finishes, teams, missing):
    for row in finishes:
        row['time_seconds'] = (
            row['time_value'] / 100 if row.get('map_id') is not None else row['time_value']
        )
    for team in teams:
        members = team['members']
        team['member_names'] = [
            member['player_name'] if isinstance(member, dict) else member
            for member in members
        ]
        if 'time_value' in team:
            team['time_seconds'] = team['time_value'] / 100
        else:
            team['time_seconds'] = team['rows'][0]['time_value']
    finishes.sort(key=lambda row: (row['map_name'], row['player_name'], row['finished_at']))
    teams.sort(key=lambda row: team_sort_key(row))
    linked_finishes = set()
    entries = []
    for team in teams:
        matching = [row for row in finishes if finish_matches_team(row, team)]
        linked_finishes.update(finish_key(row) for row in matching)
        first = team if 'map_name' in team else team['rows'][0]
        player_names = team['member_names']
        race_count = len(matching)
        entries.append({
            'kind': 'team',
            'kind_label': 'Team Rank',
            'target': team_target(team),
            'map_name': first['map_name'],
            'players': ', '.join(player_names),
            'player_names': player_names,
            'time_seconds': team['time_seconds'],
            'finished_at': first['finished_at'],
            'server': first.get('server') or (matching[0]['server'] if matching else ''),
            'game_id': first.get('game_id'),
            'impact': f'{race_count} Race Row{plural(race_count)} + 1 Team Rank',
            'race_count': race_count,
            'team_count': 1,
            'checkpoint_rows': checkpoint_rows(matching),
        })
    for row in finishes:
        if finish_key(row) in linked_finishes:
            continue
        entries.append({
            'kind': 'rank',
            'kind_label': 'Normal Rank',
            'target': rank_target(row),
            'map_name': row['map_name'],
            'players': row['player_name'],
            'player_names': [row['player_name']],
            'time_seconds': row['time_seconds'],
            'finished_at': row['finished_at'],
            'server': row['server'],
            'game_id': row.get('game_id'),
            'impact': '1 Race Row',
            'race_count': 1,
            'team_count': 0,
            'checkpoint_rows': checkpoint_rows([row]),
        })
    entries.sort(key=lambda row: row['finished_at'], reverse=True)
    return {
        'target_type': target_type,
        'target': target,
        'finishes': finishes,
        'teams': teams,
        'entries': entries,
        'missing': missing,
    }


def preview_entry(preview, target):
    if target['kind'] == 'team':
        for team in preview['teams']:
            if team_target(team) == target:
                finishes = [
                    row for row in preview['finishes']
                    if finish_matches_team(row, team)
                ]
                return build_preview('team', preview['target'], finishes, [team], [])
    else:
        for row in preview['finishes']:
            if rank_target(row) == target:
                return build_preview('rank', preview['target'], [row], [], [])
    raise RankNotFound('The selected archived rank does not exist.')


def plural(count):
    return '' if count == 1 else 's'


def finish_matches_team(finish, team):
    if 'map_id' in team:
        player_ids = {member['player_id'] for member in team['members']}
        return (
            finish.get('map_id') == team['map_id']
            and finish.get('player_id') in player_ids
            and finish['time_value'] == team['time_value']
            and finish['finished_at'] == team['finished_at']
            and finish.get('game_id') == team.get('game_id')
        )
    first = team['rows'][0]
    return (
        finish['map_name'] == first['map_name']
        and finish['player_name'] in team['members']
        and finish['time_value'] == first['time_value']
        and finish['finished_at'] == first['finished_at']
        and finish.get('game_id') == first.get('game_id')
        and bool(finish.get('ddnet7')) == bool(first.get('ddnet7'))
    )


def team_sort_key(team):
    if 'map_name' in team:
        return team['map_name'], team['finished_at']
    return team['rows'][0]['map_name'], team['rows'][0]['finished_at']


def graveyard(target, actor_id, actor_name, reason):
    connection = database()
    action_id = uuid.uuid4()
    with transaction.atomic(using='ddnet_db'):
        with connection.cursor() as cursor:
            preview = preview_target(target, lock=True, cursor=cursor)
            detail_values = {
                'missing': preview['missing'],
            }
            detail_target = target.get('source', target)
            if detail_target['kind'] in ('checkpoint', 'checkpoint_group'):
                clusters = detail_target.get('clusters', [detail_target])
                first = clusters[0]
                detail_values['checkpoint'] = {
                    'map_name': first['map_name'],
                    'player_name': first.get('player_name'),
                    'selection': checkpoint_label(
                        first['start_checkpoint'], first['end_checkpoint']
                    ),
                    'signatures': [
                        checkpoint_signature(cluster['values'], cluster['database_vendor'])
                        for cluster in clusters
                    ],
                }
            details = json.dumps(detail_values, ensure_ascii=False)
            history = history_fields(preview, detail_values)
            cursor.execute(
                'INSERT INTO record_control_history '
                '(action_id, target_type, created_at, created_by_id, '
                'created_by_name, reason, details, summary, map_name, map_count, '
                'player_name, player_count, finish_count, team_count) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '
                '%s, %s, %s, %s)',
                (
                    str(action_id), preview['target_type'], utc_now(), actor_id,
                    actor_name, reason, details,
                    history['summary'], history['map_name'], history['map_count'],
                    history['player_name'], history['player_count'],
                    history['finish_count'], history['team_count'],
                ),
            )
            if connection.vendor == 'mysql':
                archive_mysql(cursor, action_id, preview)
                delete_mysql(cursor, preview)
            else:
                archive_postgresql(cursor, action_id, preview)
                delete_postgresql(cursor, preview)
                rebuild_postgresql_players(
                    cursor,
                    {row['player_id'] for row in preview['finishes']},
                )
    return action_id


def history_fields(preview, details):
    map_names = {entry['map_name'] for entry in preview['entries']}
    player_names = {
        player_name
        for entry in preview['entries']
        for player_name in entry['player_names']
    }
    checkpoint = details.get('checkpoint') or {}
    player_name = checkpoint.get('player_name')
    if not player_name and preview['target_type'] == 'player':
        player_name = preview['target']
    if not player_name and len(player_names) == 1:
        player_name = next(iter(player_names))
    return {
        'summary': preview['target'],
        'map_name': next(iter(map_names)) if len(map_names) == 1 else None,
        'map_count': len(map_names),
        'player_name': player_name,
        'player_count': len(player_names),
        'finish_count': len(preview['finishes']),
        'team_count': len(preview['teams']),
    }


def archive_mysql(cursor, action_id, preview):
    if preview['finishes']:
        placeholders = ', '.join(['%s'] * 33)
        cursor.executemany(
            'INSERT INTO record_race_graveyard '
            '(action_id, Map, Name, Timestamp, Time, Server, '
            + ', '.join(f'cp{number}' for number in range(1, 26))
            + ', GameID, DDNet7) VALUES (' + placeholders + ')',
            [mysql_finish_values(action_id, row) for row in preview['finishes']],
        )
        if cursor.rowcount != len(preview['finishes']):
            raise RankConflict('Not every race row was archived.')
    team_rows = [row for team in preview['teams'] for row in team['rows']]
    if team_rows:
        cursor.executemany(
            'INSERT INTO record_teamrace_graveyard '
            '(action_id, Map, Name, Timestamp, Time, ID, GameID, DDNet7) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
            [
                (
                    str(action_id), row['map_name'], row['player_name'],
                    row['finished_at'], row['time_value'], row['team_id'],
                    row['game_id'], row['ddnet7'],
                )
                for row in team_rows
            ],
        )
        if cursor.rowcount != len(team_rows):
            raise RankConflict('Not every teamrank row was archived.')


def mysql_finish_values(action_id, row):
    checkpoints = [row[f'cp{number}'] for number in range(1, 26)]
    return (
        str(action_id), row['map_name'], row['player_name'], row['finished_at'],
        row['time_value'], row['server'], *checkpoints, row['game_id'], row['ddnet7'],
    )


def delete_mysql(cursor, preview):
    if preview['finishes']:
        cursor.executemany(
            'DELETE FROM record_race WHERE Map = %s AND Name = %s '
            'AND Time = %s AND Timestamp = %s AND Server = %s',
            [
                (
                    row['map_name'], row['player_name'], row['time_value'],
                    row['finished_at'], row['server'],
                )
                for row in preview['finishes']
            ],
        )
        if cursor.rowcount != len(preview['finishes']):
            raise RankConflict('The live race rows changed before deletion.')
    team_rows = [row for team in preview['teams'] for row in team['rows']]
    if team_rows:
        cursor.executemany(
            'DELETE FROM record_teamrace WHERE Map = %s AND Name = %s '
            'AND Timestamp = %s AND Time = %s AND ID = %s',
            [
                (
                    row['map_name'], row['player_name'], row['finished_at'],
                    row['time_value'], row['team_id'],
                )
                for row in team_rows
            ],
        )
        if cursor.rowcount != len(team_rows):
            raise RankConflict('The live teamrank rows changed before deletion.')


def archive_postgresql(cursor, action_id, preview):
    if preview['finishes']:
        cursor.executemany(
            'INSERT INTO record_finish_graveyard '
            '(action_id, map_id, map_name, player_id, player_name, time_cs, '
            'finished_at, server, game_uuid, cp_times, ddnet7) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
            [
                (
                    str(action_id), row['map_id'], row['map_name'],
                    row['player_id'], row['player_name'], row['time_value'],
                    row['finished_at'], row['server'], row['game_id'],
                    row['cp_times'], row['ddnet7'],
                )
                for row in preview['finishes']
            ],
        )
        if cursor.rowcount != len(preview['finishes']):
            raise RankConflict('Not every finish row was archived.')
    if preview['teams']:
        cursor.executemany(
            'INSERT INTO record_team_graveyard '
            '(action_id, team_id, map_id, map_name, roster_hash, time_cs, '
            'finished_at, server, game_uuid, member_count, ddnet7) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)',
            [
                (
                    str(action_id), team['team_id'], team['map_id'],
                    team['map_name'], team['roster_hash'], team['time_value'],
                    team['finished_at'], team['server'], team['game_id'],
                    team['member_count'], team['ddnet7'],
                )
                for team in preview['teams']
            ],
        )
        members = [
            (str(action_id), team['team_id'], member['player_id'], member['player_name'])
            for team in preview['teams']
            for member in team['members']
        ]
        cursor.executemany(
            'INSERT INTO record_team_player_graveyard '
            '(action_id, team_id, player_id, player_name) VALUES (%s, %s, %s, %s)',
            members,
        )
        if cursor.rowcount != len(members):
            raise RankConflict('Not every team member was archived.')


def delete_postgresql(cursor, preview):
    if preview['finishes']:
        cursor.executemany(
            'DELETE FROM record_finish WHERE map_id = %s AND player_id = %s '
            'AND time_cs = %s AND finished_at = %s AND server = %s',
            [
                (
                    row['map_id'], row['player_id'], row['time_value'],
                    row['finished_at'], row['server'],
                )
                for row in preview['finishes']
            ],
        )
        if cursor.rowcount != len(preview['finishes']):
            raise RankConflict('The live finish rows changed before deletion.')
    if preview['teams']:
        members = [
            (team['team_id'], member['player_id'])
            for team in preview['teams']
            for member in team['members']
        ]
        cursor.executemany(
            'DELETE FROM record_team_player WHERE team_id = %s AND player_id = %s',
            members,
        )
        if cursor.rowcount != len(members):
            raise RankConflict('The live team membership changed before deletion.')
        cursor.executemany(
            'DELETE FROM record_team WHERE team_id = %s',
            [(team['team_id'],) for team in preview['teams']],
        )
        if cursor.rowcount != len(preview['teams']):
            raise RankConflict('The live teamranks changed before deletion.')


def rebuild_postgresql_players(cursor, player_ids):
    for player_id in sorted(player_ids):
        cursor.execute('DELETE FROM record_best WHERE player_id = %s', (player_id,))
        cursor.execute(
            'INSERT INTO record_best '
            '(map_id, player_id, server, time_cs, cp_time_cs, cp_times, '
            'finish_count, first_finished, last_finished) '
            'SELECT f.map_id, f.player_id, f.server, MIN(f.time_cs), '
            'cp.time_cs, cp.cp_times, COUNT(*), MIN(f.finished_at), MAX(f.finished_at) '
            'FROM record_finish f LEFT JOIN LATERAL ('
            'SELECT f2.time_cs, f2.cp_times FROM record_finish f2 '
            'WHERE f2.map_id = f.map_id AND f2.player_id = f.player_id '
            'AND f2.server = f.server AND f2.cp_times IS NOT NULL '
            'ORDER BY f2.time_cs LIMIT 1) cp ON true '
            'WHERE f.player_id = %s '
            'GROUP BY f.map_id, f.player_id, f.server, cp.time_cs, cp.cp_times',
            (player_id,),
        )
        cursor.execute(
            'UPDATE record_player SET first_finished = ('
            "SELECT MIN(finished_at) FROM record_finish WHERE player_id = %s "
            "AND finished_at > '1970-01-01') WHERE player_id = %s",
            (player_id, player_id),
        )


def list_actions(cleaned_data):
    connection = database()
    conditions = []
    arguments = []
    query = cleaned_data.get('query')
    game_id = cleaned_data.get('game_id')
    action_type = cleaned_data.get('action_type')
    control = cleaned_data.get('control')
    state = cleaned_data.get('state')
    if query:
        if connection.vendor == 'mysql':
            conditions.append(
                '(a.summary LIKE %s OR a.map_name LIKE %s '
                'OR a.player_name LIKE %s OR a.created_by_name LIKE %s '
                'OR EXISTS (SELECT 1 FROM record_race_graveyard r '
                'WHERE r.action_id = a.action_id AND r.Name LIKE %s) '
                'OR EXISTS (SELECT 1 FROM record_teamrace_graveyard t '
                'WHERE t.action_id = a.action_id AND t.Name LIKE %s) '
                "OR (a.target_type = 'save_restore' AND a.details LIKE %s))"
            )
        else:
            conditions.append(
                '(a.summary LIKE %s OR a.map_name LIKE %s '
                'OR a.player_name LIKE %s OR a.created_by_name LIKE %s '
                'OR EXISTS (SELECT 1 FROM record_finish_graveyard f '
                'WHERE f.action_id = a.action_id AND f.player_name LIKE %s) '
                'OR EXISTS (SELECT 1 FROM record_team_player_graveyard tp '
                'WHERE tp.action_id = a.action_id AND tp.player_name LIKE %s) '
                "OR (a.target_type = 'save_restore' AND CAST(a.details AS TEXT) LIKE %s))"
            )
        arguments.extend([f'%{query}%'] * 7)
    if action_type:
        conditions.append('a.target_type = %s')
        arguments.append(action_type)
    if control == 'save':
        conditions.append("a.target_type = 'save_restore'")
    elif control == 'rank':
        conditions.append("a.target_type <> 'save_restore'")
    if game_id:
        if connection.vendor == 'mysql':
            conditions.append(
                '(EXISTS (SELECT 1 FROM record_race_graveyard r '
                'WHERE r.action_id = a.action_id AND r.GameID = %s) OR '
                'EXISTS (SELECT 1 FROM record_teamrace_graveyard t '
                'WHERE t.action_id = a.action_id AND t.GameID = %s))'
            )
        else:
            conditions.append(
                '(EXISTS (SELECT 1 FROM record_finish_graveyard f '
                'WHERE f.action_id = a.action_id AND f.game_uuid = %s) OR '
                'EXISTS (SELECT 1 FROM record_team_graveyard t '
                'WHERE t.action_id = a.action_id AND t.game_uuid = %s))'
            )
        arguments.extend((game_id, game_id))
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ''
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT a.action_id, a.target_type, a.created_at, '
            'a.created_by_name, a.reason, a.details, a.restored_at, '
            'a.restored_by_name, a.summary, a.map_name, a.map_count, '
            'a.player_name, a.player_count, a.finish_count, a.team_count '
            f'FROM record_control_history a {where} '
            f'ORDER BY a.created_at DESC LIMIT {SEARCH_LIMIT}',
            arguments,
        )
        actions = rows_as_dicts(cursor)
        for action in actions:
            details_value = action.pop('details')
            details = (
                json.loads(details_value)
                if isinstance(details_value, str)
                else details_value
            )
            action['content_count'] = action['finish_count'] + action['team_count']
            action['type_label'] = {
                'rank': 'Normal Rank',
                'team': 'Team Rank',
                'player': 'Player',
                'checkpoint': 'Checkpoint Scan',
                'save_restore': 'Save Recovery',
            }.get(action['target_type'], action['target_type'].title())
            action['details'] = details
            if action['target_type'] == 'save_restore':
                from .save_recovery import save_status
                action['state'], action['state_key'] = save_status(details['save'], cursor)
                action['content_count'] = 1
            elif action['restored_at'] is not None:
                action['state'] = 'In Live Database'
                action['state_key'] = 'live'
            elif details.get('entry_restores'):
                action['state'] = 'Partially In Live Database'
                action['state_key'] = 'partial'
            else:
                action['state'] = 'Outside Live Database'
                action['state_key'] = 'outside'
        add_history_players(cursor, connection.vendor, actions)
        if state:
            actions = [
                action for action in actions
                if (action['state_key'] == 'live') == (state == 'restored')
            ]
        for action in actions:
            action.pop('details', None)
        return actions


def add_history_players(cursor, vendor, actions):
    if not actions:
        return
    rank_actions = [
        action for action in actions if action.get('target_type') != 'save_restore'
    ]
    action_ids = [str(action['action_id']) for action in rank_actions]
    players_by_action = {}
    if not action_ids:
        rows = []
    else:
        placeholders = ', '.join(['%s'] * len(action_ids))
        if vendor == 'mysql':
            cursor.execute(
                'SELECT action_id, Name AS player_name FROM record_race_graveyard '
                f'WHERE action_id IN ({placeholders}) UNION '
                'SELECT action_id, Name AS player_name FROM record_teamrace_graveyard '
                f'WHERE action_id IN ({placeholders})',
                action_ids + action_ids,
            )
        else:
            cursor.execute(
                'SELECT action_id, player_name FROM record_finish_graveyard '
                f'WHERE action_id IN ({placeholders}) UNION '
                'SELECT action_id, player_name FROM record_team_player_graveyard '
                f'WHERE action_id IN ({placeholders})',
                action_ids + action_ids,
            )
        rows = rows_as_dicts(cursor)
    for row in rows:
        players_by_action.setdefault(str(row['action_id']), set()).add(row['player_name'])
    for action in actions:
        if action.get('target_type') == 'save_restore':
            player_names = action['details']['save']['players']
        else:
            player_names = sorted(
                players_by_action.get(str(action['action_id']), ()), key=str.casefold,
            )
        source_player = action['player_name']
        if source_player:
            player_names = [source_player] + [
                player_name for player_name in player_names
                if player_name != source_player
            ]
        action['player_names'] = player_names
        action['other_player_count'] = (
            max(len(player_names) - 1, 0) if source_player else 0
        )


def get_action_record(cursor, action_id, lock=False):
    suffix = ' FOR UPDATE' if lock else ''
    cursor.execute(
        'SELECT action_id, target_type, created_at, created_by_id, '
        'created_by_name, reason, details, restored_at, restored_by_id, '
        'restored_by_name, restore_reason, summary FROM record_control_history '
        'WHERE action_id = %s' + suffix,
        (str(action_id),),
    )
    rows = rows_as_dicts(cursor)
    if not rows:
        raise RankNotFound('The Records action does not exist.')
    action = rows[0]
    if isinstance(action['details'], str):
        action['details'] = json.loads(action['details'])
    return action


def get_history_action(action_id):
    connection = database()
    with connection.cursor() as cursor:
        return get_action_record(cursor, action_id)


def get_action_overview(action_id):
    connection = database()
    with connection.cursor() as cursor:
        action = get_action_record(cursor, action_id)
        if connection.vendor == 'mysql':
            cursor.execute(
                'SELECT Map AS map_name, COUNT(*) AS finish_count '
                'FROM record_race_graveyard WHERE action_id = %s GROUP BY Map',
                (str(action_id),),
            )
            finishes = rows_as_dicts(cursor)
            cursor.execute(
                'SELECT map_name, COUNT(*) AS team_count FROM ('
                'SELECT Map AS map_name FROM record_teamrace_graveyard '
                'WHERE action_id = %s '
                'GROUP BY Map, Timestamp, Time, ID, GameID, COALESCE(DDNet7, 0)'
                ') archived_teams GROUP BY map_name',
                (str(action_id),),
            )
        else:
            cursor.execute(
                'SELECT map_name, COUNT(*) AS finish_count '
                'FROM record_finish_graveyard WHERE action_id = %s GROUP BY map_name',
                (str(action_id),),
            )
            finishes = rows_as_dicts(cursor)
            cursor.execute(
                'SELECT map_name, COUNT(*) AS team_count '
                'FROM record_team_graveyard WHERE action_id = %s GROUP BY map_name',
                (str(action_id),),
            )
        teams = rows_as_dicts(cursor)

    groups = {
        row['map_name']: {
            'map_name': row['map_name'],
            'finish_count': row['finish_count'],
            'team_count': 0,
        }
        for row in finishes
    }
    for row in teams:
        group = groups.setdefault(row['map_name'], {
            'map_name': row['map_name'], 'finish_count': 0, 'team_count': 0,
        })
        group['team_count'] = row['team_count']
    map_groups = sorted(groups.values(), key=lambda row: row['map_name'].casefold())
    for group in map_groups:
        group['record_count'] = group['finish_count'] + group['team_count']
        group['individual_preview'] = group['record_count'] <= PLAYER_MAP_PREVIEW_LIMIT
    restores = action['details'].get('entry_restores', [])
    action['partial_restore_count'] = len(restores)
    action['preview'] = {
        'action_scope': True,
        'action_map_groups': map_groups,
        'finish_count': sum(row['finish_count'] for row in map_groups),
        'team_count': sum(row['team_count'] for row in map_groups),
        'entries': [],
        'missing': [],
    }
    return action


def get_action(action_id, lock=False, cursor=None, map_name=None):
    connection = database()
    if cursor is None:
        with connection.cursor() as own_cursor:
            return get_action(
                action_id, lock=lock, cursor=own_cursor, map_name=map_name,
            )
    action = get_action_record(cursor, action_id, lock)
    if connection.vendor == 'mysql':
        preview = archived_mysql_preview(cursor, action_id, action, map_name)
    else:
        preview = archived_postgresql_preview(cursor, action_id, action, map_name)
    restores = {
        target_key(item['target']): item
        for item in action['details'].get('entry_restores', [])
    }
    for entry in preview['entries']:
        entry['restore'] = restores.get(target_key(entry['target']))
        entry['restored'] = action['restored_at'] is not None or entry['restore'] is not None
    action['partial_restore_count'] = len(restores)
    action['preview'] = preview
    return action


def archived_mysql_preview(cursor, action_id, action, map_name=None):
    map_filter = ' AND r.Map = %s' if map_name else ''
    arguments = (str(action_id), map_name) if map_name else (str(action_id),)
    cursor.execute(
        f'SELECT {mysql_finish_columns("r")} FROM record_race_graveyard r '
        'WHERE action_id = %s' + map_filter,
        arguments,
    )
    finishes = rows_as_dicts(cursor)
    map_filter = ' AND Map = %s' if map_name else ''
    cursor.execute(
        'SELECT Map AS map_name, Name AS player_name, Timestamp AS finished_at, '
        'CAST(Time AS DOUBLE) AS time_value, ID AS team_id, GameID AS game_id, '
        'COALESCE(DDNet7, 0) AS ddnet7 FROM record_teamrace_graveyard '
        'WHERE action_id = %s' + map_filter,
        arguments,
    )
    teams_by_key = {}
    for row in rows_as_dicts(cursor):
        team = teams_by_key.setdefault(mysql_team_key(row), {'rows': [], 'members': []})
        team['rows'].append(row)
        team['members'].append(row['player_name'])
    for team in teams_by_key.values():
        team['members'].sort()
    return build_preview(
        action['target_type'], action['summary'], finishes,
        list(teams_by_key.values()), [
            row for row in action['details'].get('missing', [])
            if not map_name or row['map_name'] == map_name
        ],
    )


def archived_postgresql_preview(cursor, action_id, action, map_name=None):
    map_filter = ' AND map_name = %s' if map_name else ''
    arguments = (str(action_id), map_name) if map_name else (str(action_id),)
    cursor.execute(
        'SELECT map_id, map_name, player_id, player_name, time_cs AS time_value, '
        'finished_at, server, game_uuid AS game_id, cp_times, ddnet7 '
        'FROM record_finish_graveyard WHERE action_id = %s' + map_filter,
        arguments,
    )
    finishes = rows_as_dicts(cursor)
    map_filter = ' AND t.map_name = %s' if map_name else ''
    cursor.execute(
        'SELECT t.team_id, t.map_id, t.map_name, t.roster_hash, '
        't.time_cs AS time_value, t.finished_at, t.server, '
        't.game_uuid AS game_id, t.member_count, t.ddnet7, '
        'tp.player_id, tp.player_name FROM record_team_graveyard t '
        'JOIN record_team_player_graveyard tp '
        'ON tp.action_id = t.action_id AND tp.team_id = t.team_id '
        'WHERE t.action_id = %s' + map_filter,
        arguments,
    )
    grouped = {}
    for row in rows_as_dicts(cursor):
        grouped.setdefault(bytes(row['team_id']), []).append(row)
    teams = [postgres_team(rows) for rows in grouped.values()]
    return build_preview(
        action['target_type'], action['summary'], finishes, teams,
        [
            row for row in action['details'].get('missing', [])
            if not map_name or row['map_name'] == map_name
        ],
    )


def restore(action_id, actor_id, actor_name, reason):
    connection = database()
    with transaction.atomic(using='ddnet_db'):
        with connection.cursor() as cursor:
            action = get_action(action_id, lock=True, cursor=cursor)
            if action['restored_at'] is not None:
                raise RankConflict('This action was already restored.')
            pending = [entry for entry in action['preview']['entries'] if not entry['restored']]
            if not pending:
                raise RankConflict('Every rank in this action was already restored.')
            previews = [preview_entry(action['preview'], entry['target']) for entry in pending]
            preview = {
                'finishes': [row for item in previews for row in item['finishes']],
                'teams': [team for item in previews for team in item['teams']],
            }
            if connection.vendor == 'mysql':
                check_mysql_restore(cursor, preview)
                restore_mysql(cursor, preview)
            else:
                check_postgresql_restore(cursor, preview)
                restore_postgresql(cursor, preview)
                rebuild_postgresql_players(
                    cursor,
                    {row['player_id'] for row in preview['finishes']},
                )
            cursor.execute(
                'UPDATE record_control_history SET restored_at = %s, '
                'restored_by_id = %s, restored_by_name = %s, restore_reason = %s '
                'WHERE action_id = %s AND restored_at IS NULL',
                (utc_now(), actor_id, actor_name, reason, str(action_id)),
            )
            if cursor.rowcount != 1:
                raise RankConflict('The graveyard action changed before restoration.')


def restore_entry(action_id, target, actor_id, actor_name, reason):
    connection = database()
    with transaction.atomic(using='ddnet_db'):
        with connection.cursor() as cursor:
            action = get_action(action_id, lock=True, cursor=cursor)
            if action['restored_at'] is not None:
                raise RankConflict('This action was already restored.')
            selected = next(
                (
                    entry for entry in action['preview']['entries']
                    if entry['target'] == target
                ),
                None,
            )
            if selected is None:
                raise RankNotFound('The selected archived rank does not exist.')
            if selected['restored']:
                raise RankConflict('This rank was already restored.')
            preview = preview_entry(action['preview'], target)
            if connection.vendor == 'mysql':
                check_mysql_restore(cursor, preview)
                restore_mysql(cursor, preview)
            else:
                check_postgresql_restore(cursor, preview)
                restore_postgresql(cursor, preview)
                rebuild_postgresql_players(
                    cursor,
                    {row['player_id'] for row in preview['finishes']},
                )
            restored_at = utc_now()
            restores = action['details'].setdefault('entry_restores', [])
            restores.append({
                'target': target,
                'restored_at': token_datetime(restored_at),
                'restored_by_id': actor_id,
                'restored_by_name': actor_name,
                'reason': reason,
            })
            values: list[object] = [json.dumps(action['details'], ensure_ascii=False)]
            update = 'UPDATE record_control_history SET details = %s'
            if len(restores) == len(action['preview']['entries']):
                update += (', restored_at = %s, restored_by_id = %s, '
                           'restored_by_name = %s, restore_reason = %s')
                values.extend((restored_at, actor_id, actor_name, 'Restored individually.'))
            values.append(str(action_id))
            cursor.execute(update + ' WHERE action_id = %s AND restored_at IS NULL', values)
            if cursor.rowcount != 1:
                raise RankConflict('The graveyard action changed before restoration.')


def check_mysql_restore(cursor, preview):
    for row in preview['finishes']:
        cursor.execute(
            'SELECT 1 FROM record_race WHERE Map = %s AND Name = %s '
            'AND Time = %s AND Timestamp = %s AND Server = %s FOR UPDATE',
            (
                row['map_name'], row['player_name'], row['time_value'],
                row['finished_at'], row['server'],
            ),
        )
        if cursor.fetchone() is not None:
            raise RankConflict(
                f"A live race row already exists for {row['player_name']} on {row['map_name']}."
            )
    for team in preview['teams']:
        check_mysql_roster_conflict(cursor, team)


def check_mysql_roster_conflict(cursor, archived_team):
    archived_names = sorted(archived_team['members'])
    first = archived_team['rows'][0]
    cursor.execute(
        'SELECT Map AS map_name, Timestamp AS finished_at, '
        'CAST(Time AS DOUBLE) AS time_value, '
        'ID AS team_id, GameID AS game_id, COALESCE(DDNet7, 0) AS ddnet7 '
        'FROM record_teamrace WHERE Map = %s AND Name = %s FOR UPDATE',
        (first['map_name'], archived_names[0]),
    )
    candidates = {mysql_team_key(row): row for row in rows_as_dicts(cursor)}
    for candidate in candidates.values():
        cursor.execute(
            'SELECT Name FROM record_teamrace WHERE Map = %s AND Timestamp = %s '
            'AND Time = %s AND ID = %s AND GameID <=> %s '
            'AND COALESCE(DDNet7, 0) = %s FOR UPDATE',
            (
                candidate['map_name'], candidate['finished_at'],
                candidate['time_value'], candidate['team_id'],
                candidate['game_id'], candidate['ddnet7'],
            ),
        )
        if sorted(row[0] for row in cursor.fetchall()) == archived_names:
            raise RankConflict(
                f"A live teamrank already exists for {', '.join(archived_names)} "
                f"on {first['map_name']}."
            )


def restore_mysql(cursor, preview):
    if preview['finishes']:
        placeholders = ', '.join(['%s'] * 32)
        cursor.executemany(
            'INSERT INTO record_race '
            '(Map, Name, Timestamp, Time, Server, '
            + ', '.join(f'cp{number}' for number in range(1, 26))
            + ', GameID, DDNet7) VALUES (' + placeholders + ')',
            [mysql_live_finish_values(row) for row in preview['finishes']],
        )
        if cursor.rowcount != len(preview['finishes']):
            raise RankConflict('Not every race row was restored.')
    team_rows = [row for team in preview['teams'] for row in team['rows']]
    if team_rows:
        cursor.executemany(
            'INSERT INTO record_teamrace '
            '(Map, Name, Timestamp, Time, ID, GameID, DDNet7) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s)',
            [
                (
                    row['map_name'], row['player_name'], row['finished_at'],
                    row['time_value'], row['team_id'], row['game_id'], row['ddnet7'],
                )
                for row in team_rows
            ],
        )
        if cursor.rowcount != len(team_rows):
            raise RankConflict('Not every teamrank row was restored.')


def mysql_live_finish_values(row):
    checkpoints = [row[f'cp{number}'] for number in range(1, 26)]
    return (
        row['map_name'], row['player_name'], row['finished_at'], row['time_value'],
        row['server'], *checkpoints, row['game_id'], row['ddnet7'],
    )


def check_postgresql_restore(cursor, preview):
    for row in preview['finishes']:
        cursor.execute(
            'SELECT 1 FROM record_finish WHERE map_id = %s AND player_id = %s '
            'AND time_cs = %s AND finished_at = %s AND server = %s FOR UPDATE',
            (
                row['map_id'], row['player_id'], row['time_value'],
                row['finished_at'], row['server'],
            ),
        )
        if cursor.fetchone() is not None:
            raise RankConflict(
                f"A live finish already exists for {row['player_name']} on {row['map_name']}."
            )
    for team in preview['teams']:
        cursor.execute(
            'SELECT 1 FROM record_team WHERE map_id = %s AND roster_hash = %s FOR UPDATE',
            (team['map_id'], team['roster_hash']),
        )
        if cursor.fetchone() is not None:
            roster = ', '.join(member['player_name'] for member in team['members'])
            raise RankConflict(
                f'A live teamrank already exists for {roster} on {team["map_name"]}.'
            )


def restore_postgresql(cursor, preview):
    if preview['finishes']:
        cursor.executemany(
            'INSERT INTO record_finish '
            '(map_id, player_id, time_cs, finished_at, server, game_uuid, cp_times, ddnet7) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
            [
                (
                    row['map_id'], row['player_id'], row['time_value'],
                    row['finished_at'], row['server'], row['game_id'],
                    row['cp_times'], row['ddnet7'],
                )
                for row in preview['finishes']
            ],
        )
        if cursor.rowcount != len(preview['finishes']):
            raise RankConflict('Not every finish row was restored.')
    if preview['teams']:
        cursor.executemany(
            'INSERT INTO record_team '
            '(team_id, map_id, roster_hash, time_cs, finished_at, server, '
            'game_uuid, member_count, ddnet7) '
            'VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)',
            [
                (
                    team['team_id'], team['map_id'], team['roster_hash'],
                    team['time_value'], team['finished_at'], team['server'],
                    team['game_id'], team['member_count'], team['ddnet7'],
                )
                for team in preview['teams']
            ],
        )
        members = [
            (team['team_id'], member['player_id'])
            for team in preview['teams']
            for member in team['members']
        ]
        cursor.executemany(
            'INSERT INTO record_team_player (team_id, player_id) VALUES (%s, %s)',
            members,
        )
        if cursor.rowcount != len(members):
            raise RankConflict('Not every team member was restored.')


def schema_ready():
    connection = database()
    archive_table = (
        'record_race_graveyard'
        if connection.vendor == 'mysql'
        else 'record_finish_graveyard'
    )
    with connection.cursor() as cursor:
        cursor.execute('SELECT 1 FROM record_control_history LIMIT 1')
        cursor.fetchone()
        cursor.execute(f'SELECT 1 FROM {archive_table} LIMIT 1')
        cursor.fetchone()
