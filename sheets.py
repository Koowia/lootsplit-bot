import aiohttp
import asyncio
import json
import sqlite3
from datetime import datetime

from config import GOOGLE_SCRIPT_URL, DB_PATH
from db import db_all, db_run, get_player, add_log
from utils import format_details


def sheet_json(result: str) -> dict:
    try:
        value = json.loads(result)
        if isinstance(value, dict):
            return value
    except (json.JSONDecodeError, TypeError):
        pass
    return {'error': 'Google вернул некорректный ответ'}


async def sheet_request(data: dict, retries: int = 3) -> str:
    action = data.get('action', '?')
    error = 'нет ответа'
    async with aiohttp.ClientSession() as session:
        for attempt in range(retries):
            try:
                async with session.post(
                    GOOGLE_SCRIPT_URL, json=data,
                    timeout=aiohttp.ClientTimeout(total=30), allow_redirects=False
                ) as response:
                    status = response.status
                    if status in (301, 302, 303):
                        location = response.headers.get('Location')
                        if not location:
                            raise ValueError('redirect without Location')
                        text = ''
                        for get_attempt in range(6):
                            if get_attempt:
                                await asyncio.sleep(2)
                            async with session.get(location, timeout=aiohttp.ClientTimeout(total=30)) as reply:
                                status = reply.status
                                text = await reply.text()
                                if status == 200 and text.lstrip().startswith('{'):
                                    break
                    else:
                        text = await response.text()
                value = sheet_json(text)
                if status == 200 and not value.get('error'):
                    return json.dumps(value, ensure_ascii=False)
                error = str(value.get('error') or f'HTTP {status}')
                if value.get('error') and value['error'] != 'Google вернул некорректный ответ':
                    if value['error'] != 'busy, try again':
                        print(f'⚠️ Google {action}: {error}')
                        return json.dumps(value, ensure_ascii=False)
                error = f'HTTP {status}: {error}'
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                error = type(exc).__name__
            if attempt < retries - 1:
                await asyncio.sleep(3)
    print(f'⚠️ Google {action}: {error} (после {retries} попыток)')
    return json.dumps({'error': error}, ensure_ascii=False)


async def sheet_get_players():
    data = sheet_json(await sheet_request({'action': 'get_all'}))
    raw = data.get('players')
    if data.get('protocol') != 2:
        print('⚠️ Google get_all: требуется новая опубликованная версия Apps Script (protocol 2)')
        return None
    if data.get('error') or not isinstance(raw, list):
        return None
    
    players, seen = [], set()
    skipped = 0
    
    for item in raw:
        try:
            nick = str(item.get('nick', '')).strip()
            balance = item.get('balance')
            
            if not nick:
                skipped += 1
                continue
            
            if nick.lower() in seen:
                print(f'⚠️ Дубликат ника пропущен: {nick}')
                skipped += 1
                continue
            
            if isinstance(balance, bool) or not isinstance(balance, (int, float)):
                print(f'⚠️ Некорректный баланс у {nick}: {balance!r} — пропущен')
                skipped += 1
                continue
            
            if int(balance) != balance:
                print(f'⚠️ Дробный баланс у {nick}: {balance} — округлен')
                balance = int(balance)
            
            players.append({'nick': nick, 'balance': int(balance)})
            seen.add(nick.lower())
            
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            print(f'⚠️ Ошибка обработки строки {item}: {e} — пропущена')
            skipped += 1
            continue
    
    if skipped > 0:
        print(f'⚠️ Пропущено {skipped} проблемных строк из {len(raw)}')
    
    return players


async def _sync_sheets():
    sheet_players = await sheet_get_players()
    if sheet_players is None:
        return False

    sheet_nicks = set()
    for item in sheet_players:
        nick = str(item['nick']).strip()
        sheet_nicks.add(nick.lower())
        try:
            sheet_balance = int(item['balance'])
        except (ValueError, TypeError):
            print(f'⚠️ Пропуск строки с битым балансом: {nick!r} → {item["balance"]!r}')
            continue
        player = get_player(nick)
        if player is None:
            db_run('INSERT INTO players (nick, balance, last_pushed) VALUES (?, ?, ?)', (nick, sheet_balance, sheet_balance))
            add_log('Таблица', 'sheet_add', f'{nick}: {sheet_balance}')
        elif sheet_balance != player[1] and player[1] == player[2]:
            db_run('UPDATE players SET balance = ?, last_pushed = ? WHERE lower(nick) = lower(?)', (sheet_balance, sheet_balance, nick))
            add_log('Таблица', 'sheet_edit', f'{nick}: {player[1]} → {sheet_balance}')

    if len(sheet_nicks) >= 5:
        removed = 0
        for (nick,) in db_all('SELECT nick FROM players'):
            if nick.lower() not in sheet_nicks:
                db_run('DELETE FROM players WHERE lower(nick) = lower(?)', (nick,))
                add_log('Таблица', 'sheet_remove', nick)
                removed += 1
        if removed > 0:
            print(f'✅ Синк: удалено {removed} игроков (удалены из таблицы)')

    players = [{'nick': r[0], 'balance': r[1]} for r in db_all('SELECT nick, balance FROM players')]
    pending = db_all('SELECT id, date, author, op_type, details FROM operations WHERE synced = 0 ORDER BY id')
    source_id = db_all("SELECT value FROM sync_meta WHERE key = 'source_id'")[0][0]
    logs = [{'key': f'{source_id}:{r[0]}', 'date': r[1], 'author': r[2],
             'type': r[3], 'details': format_details(r[3], r[4])} for r in pending]
    if not players:
        print('⚠️ Отказ синка: база пуста, таблица не тронута')
        return False

    players.sort(key=lambda p: p['nick'].lower())
    total = len(players)
    CHUNK = 100
    for offset in range(0, total, CHUNK):
        chunk = players[offset:offset + CHUNK]
        result = sheet_json(await sheet_request({
            'action': 'sync_players', 'protocol': 2, 'offset': offset, 'players': chunk
        }))
        if result.get('ok') is not True:
            print(f'⚠️ sync_players offset={offset}: {result.get("error")}')
            return False

    commit = sheet_json(await sheet_request({
        'action': 'sync_commit', 'protocol': 2, 'total': total, 'logs': logs,
        'heartbeat': {'status': '🟢 Бот работает',
                      'time': datetime.now().strftime('%d.%m.%Y %H:%M'),
                      'players_count': total}
    }))
    if commit.get('ok') is not True or commit.get('protocol') != 2 or commit.get('count') != total:
        print(f'⚠️ sync_commit: {commit.get("error", "не подтверждён")}')
        return False

    with sqlite3.connect(DB_PATH) as conn:
        conn.executemany('UPDATE players SET last_pushed = ? WHERE nick = ?',
                         [(r['balance'], r['nick']) for r in players])
        conn.executemany(
            'UPDATE operations SET synced = 1 WHERE id = ? AND op_type = ? AND details = ?',
            [(r[0], r[3], r[4]) for r in pending])
    print(f'✅ Синк завершён: {len(players)} игроков, {len(logs)} логов')
    return True


# Глобальное состояние синка
sync_lock = asyncio.Lock()
last_sync_ok = None
last_sync_at = None


def trigger_sync():
    asyncio.create_task(sync_sheets())


async def sync_sheets():
    global last_sync_ok, last_sync_at
    async with sync_lock:
        try:
            success = await _sync_sheets()
        except Exception as exc:
            print(f'⚠️ Синк: {type(exc).__name__}: {exc}')
            success = False
        last_sync_ok = success
        last_sync_at = datetime.now()
        return success