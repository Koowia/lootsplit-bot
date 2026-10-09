import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiohttp
import asyncio
import sqlite3
import json
import re
import os
import shutil
import time
import uuid
from datetime import datetime, time as dt_time, timedelta

DISCORD_TOKEN = os.environ.get('DISCORD_TOKEN', '')
GOOGLE_SCRIPT_URL = os.environ.get('GOOGLE_SCRIPT_URL', '')
GUILD_NAME = os.environ.get('GUILD_NAME', 'Your Guild')
OFFICER_ROLE_IDS = [int(x) for x in os.environ.get('OFFICER_ROLE_IDS', '').split(',') if x.strip()]
ALERT_OWNER_IDS = [int(x) for x in os.environ.get('ALERT_OWNER_IDS', '').split(',') if x.strip()]
DB_PATH = os.environ.get('DB_PATH', 'lootsplit.db')
STARTUP_STATE_FILE = os.environ.get('STARTUP_STATE_FILE', 'startup_state.json')
SYNC_INTERVAL_MIN = 30
HEARTBEAT_STALE_MIN = 60
CRASH_WINDOW_MIN = 10
CRASH_ALERT_THRESHOLD = 3

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)


def db_run(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(sql, params)
    conn.commit()
    conn.close()


def db_all(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def db_init():
    db_run('CREATE TABLE IF NOT EXISTS players (nick TEXT PRIMARY KEY, balance INTEGER NOT NULL DEFAULT 0, last_pushed INTEGER NOT NULL DEFAULT 0)')
    db_run('CREATE TABLE IF NOT EXISTS sync_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    db_run("INSERT OR IGNORE INTO sync_meta (key, value) VALUES ('source_id', ?)", (uuid.uuid4().hex,))
    db_run('CREATE TABLE IF NOT EXISTS operations (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, author TEXT, op_type TEXT, details TEXT, synced INTEGER NOT NULL DEFAULT 0)')


def get_player(nick):
    rows = db_all('SELECT nick, balance, last_pushed FROM players WHERE lower(nick) = lower(?)', (nick,))
    return rows[0] if rows else None


def add_log(author, op_type, details):
    db_run('INSERT INTO operations (date, author, op_type, details) VALUES (?, ?, ?, ?)',
           (datetime.now().strftime('%d.%m.%Y %H:%M'), author, op_type, details))


def format_details(op_type, details):
    try:
        data = json.loads(details)
    except (json.JSONDecodeError, TypeError):
        return details
    base_type = op_type.removesuffix('_cancelled')
    if base_type == 'split':
        return f"Сплит {data['amount']:,.0f} между {len(data['nicks'])} чел (по {data['per_person']:,.0f}): {', '.join(data['nicks'])}"
    if base_type == 'pay':
        return f"Выплата {data['nick']}: {data['amount']:,.0f}"
    if base_type == 'pay_all':
        return f"Массовая выплата {len(data['payouts'])} чел на {sum(data['payouts'].values()):,.0f}"
    if base_type == 'set':
        return f"Баланс {data['nick']}: {data['old']:,.0f} → {data['new']:,.0f}"
    if base_type == 'compens':
        return f"Компенсация {data['nick']}: +{data['amount']:,.0f}"
    return details


def sheet_json(result):
    try:
        value = json.loads(result)
        if isinstance(value, dict):
            return value
    except (json.JSONDecodeError, TypeError):
        pass
    return {'error': 'Google вернул некорректный ответ'}


async def sheet_request(data, retries=3):
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
    try:
        for item in raw:
            nick = item['nick'].strip()
            balance = item['balance']
            if not nick or nick.lower() in seen:
                raise ValueError('empty or duplicate nick')
            if isinstance(balance, bool) or not isinstance(balance, (int, float)):
                raise ValueError('balance must be numeric')
            if int(balance) != balance:
                raise ValueError('balance must be an integer')
            players.append({'nick': nick, 'balance': int(balance)})
            seen.add(nick.lower())
    except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
        print('⚠️ Google get_all: некорректные игроки; синк остановлен')
        return None
    return players


async def _sync_sheets():
    sheet_players = await sheet_get_players()
    if sheet_players is None:
        print('⚠️ get_all не ответил, синк без проверки таблицы')
        sheet_players = []

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
    elif not sheet_nicks:
        print('⚠️ get_all пустой, пропускаем проверку удалений')

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


def clean_nick(discord_nick: str) -> str:
    nick = discord_nick
    for _ in range(3):
        nick = re.sub(r'^\[.*?\]\s*', '', nick)
        nick = re.sub(r'^!+\s*', '', nick)
    return nick.strip()


async def resolve_target(guild, raw: str) -> str:
    match = re.fullmatch(r'<@!?(\d+)>', raw.strip())
    if match:
        member = guild.get_member(int(match.group(1)))
        if member:
            return clean_nick(member.display_name)
    return clean_nick(raw.replace('@', '').strip())


def officer_name(member) -> str:
    return clean_nick(member.display_name)


def find_member_by_nick(guild, nick):
    target = clean_nick(nick).lower()
    for member in guild.members:
        if clean_nick(member.display_name).lower() == target:
            return member
    return None


async def notify_split_participants(guild, nicks, per_person, author_name):
    delivered = 0
    failed = []
    for nick in nicks:
        member = find_member_by_nick(guild, nick)
        if member is None:
            failed.append(nick)
            continue
        balance = get_player(nick)[1]
        embed = discord.Embed(
            title=f'💰 Лут-сплит | {GUILD_NAME}',
            description=f'Тебе начислено: **+{per_person:,}**\n💼 Баланс: **{balance:,}**\nСплит провёл: {author_name}',
            color=discord.Color.gold()
        )
        try:
            await member.send(embed=embed)
            delivered += 1
        except Exception:
            failed.append(nick)
    return delivered, failed


def is_officer_interaction(interaction) -> bool:
    return is_officer(interaction.user)


def is_officer(member: discord.Member) -> bool:
    if member.guild.owner_id == member.id:
        return True
    user_role_ids = [role.id for role in member.roles]
    return any(role_id in user_role_ids for role_id in OFFICER_ROLE_IDS)


def parse_amount(raw: str) -> float:
    raw = raw.lower().replace('м', 'm')
    if 'm' in raw:
        return float(raw.replace('m', '')) * 1000000
    return float(raw)


def plural(n: int, forms: tuple) -> str:
    n = abs(n) % 100
    if 10 < n < 20:
        return forms[2]
    n = n % 10
    if n == 1:
        return forms[0]
    if 1 < n < 5:
        return forms[1]
    return forms[2]


def access_denied():
    return discord.Embed(
        title='🚫 Доступ запрещён',
        description='У вас нет доступа для данной команды',
        color=discord.Color.red()
    )


def text_embed(text, color=None):
    return discord.Embed(description=text, color=color or discord.Color.greyple())


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


async def nick_autocomplete(interaction: discord.Interaction, current: str):
    rows = db_all("SELECT nick FROM players WHERE lower(nick) LIKE lower(?) ORDER BY nick LIMIT 25", (f"%{current}%",))
    return [app_commands.Choice(name=r[0], value=r[0]) for r in rows]


async def crash_alert_check():
    try:
        with open(STARTUP_STATE_FILE, encoding='utf-8') as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {'last_start': None, 'crashes': 0}

    now = datetime.now()
    if state.get('last_start'):
        try:
            last_start = datetime.strptime(state['last_start'], '%d.%m.%Y %H:%M:%S')
            gap_min = (now - last_start).total_seconds() / 60
            if gap_min < CRASH_WINDOW_MIN:
                state['crashes'] += 1
            else:
                state['crashes'] = 0
        except ValueError:
            state['crashes'] = 0
    state['last_start'] = now.strftime('%d.%m.%Y %H:%M:%S')

    with open(STARTUP_STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f)

    if state['crashes'] >= CRASH_ALERT_THRESHOLD:
        for owner_id in ALERT_OWNER_IDS:
            try:
                user = await bot.fetch_user(owner_id)
                await user.send(embed=discord.Embed(
                    title='🚨 Бот в цикле падений',
                    description=f'Перезапусков подряд: {state["crashes"]} за {CRASH_WINDOW_MIN} минут.\nПроверьте: journalctl -u lootsplit -n 50',
                    color=discord.Color.red()
                ))
            except Exception:
                pass
        state['crashes'] = 0
        with open(STARTUP_STATE_FILE, 'w', encoding='utf-8') as f:
            json.dump(state, f)


async def send_app_error(interaction: discord.Interaction, embed):
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.event
async def on_ready():
    db_init()
    if not db_all('SELECT nick FROM players LIMIT 1'):
        sheet_players = await sheet_get_players()
        if sheet_players:
            for item in sheet_players:
                db_run('INSERT OR IGNORE INTO players (nick, balance, last_pushed) VALUES (?, ?, ?)',
                       (str(item['nick']).strip(), int(item['balance']), int(item['balance'])))
            print(f'✅ Миграция: импортировано {len(sheet_players)} игроков')
    for guild in bot.guilds:
        await bot.tree.sync(guild=guild)
    await bot.tree.sync()
    print(f'✅ Бот {bot.user} запущен!')
    print('✅ SQLite подключена')
    if not periodic_sync.is_running():
        periodic_sync.start()
    if not daily_backup.is_running():
        daily_backup.start()
    if not warmup_google.is_running():
        warmup_google.start()
    await crash_alert_check()


@tasks.loop(minutes=SYNC_INTERVAL_MIN)
async def periodic_sync():
    await sync_sheets()


@tasks.loop(minutes=4)
async def warmup_google():
    players_count = db_all('SELECT COUNT(*) FROM players')[0][0]
    result = await sheet_request({
        'action': 'heartbeat',
        'status': '🟢 Бот работает',
        'time': datetime.now().strftime('%d.%m.%Y %H:%M'),
        'players_count': players_count
    })
    if sheet_json(result).get('ok') is True:
        print('🔥 Прогрев Google Script')
    else:
        print('⚠️ Прогрев не удался')


@tasks.loop(time=dt_time(4, 0))
async def daily_backup():
    players = [{'nick': r[0], 'balance': r[1]} for r in db_all('SELECT nick, balance FROM players')]
    if not players:
        print('⚠️ Бэкап пропущен: база пуста')
        return
    result = await sheet_request({
        'action': 'backup',
        'players': players,
        'date': datetime.now().strftime('%d.%m.%Y %H:%M')
    })
    if sheet_json(result).get('ok') is True:
        print(f'✅ Бэкап сохранён: {len(players)} игроков')
    else:
        print('⚠️ Бэкап не сохранился')


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        await send_app_error(interaction, access_denied())
    elif isinstance(error, (app_commands.TransformerError, app_commands.CommandInvokeError)):
        await send_app_error(interaction, text_embed('❌ Неверный формат аргумента. Проверь ввод и попробуй ещё раз.', discord.Color.red()))
    else:
        print(f'⚠️ Ошибка команды: {type(error).__name__}: {error}')
        await send_app_error(interaction, text_embed('❌ Ошибка выполнения команды. Попробуй позже.', discord.Color.red()))


@bot.tree.command(name='split', description='Распределить лут между игроками')
@app_commands.check(is_officer_interaction)
@app_commands.describe(
    amount='Сумма: 24м, 24m или 24000000',
    players='Ники текстом через пробел или запятую (для больших сплитов)',
    nick1='Игрок 1 (выбери из списка)',
    nick2='Игрок 2 (выбери из списка)',
    nick3='Игрок 3 (выбери из списка)',
    nick4='Игрок 4 (выбери из списка)',
    nick5='Игрок 5 (выбери из списка)',
    nick6='Игрок 6 (выбери из списка)',
    nick7='Игрок 7 (выбери из списка)',
    nick8='Игрок 8 (выбери из списка)',
    nick9='Игрок 9 (выбери из списка)',
    nick10='Игрок 10 (выбери из списка)',
    nick11='Игрок 11 (выбери из списка)',
    nick12='Игрок 12 (выбери из списка)',
    nick13='Игрок 13 (выбери из списка)',
    nick14='Игрок 14 (выбери из списка)',
    nick15='Игрок 15 (выбери из списка)',
    nick16='Игрок 16 (выбери из списка)',
    nick17='Игрок 17 (выбери из списка)',
    nick18='Игрок 18 (выбери из списка)',
    nick19='Игрок 19 (выбери из списка)',
    nick20='Игрок 20 (выбери из списка)',
    nick21='Игрок 21 (выбери из списка)',
    nick22='Игрок 22 (выбери из списка)'
)
async def split(interaction: discord.Interaction, amount: str, players: str | None = None,
                nick1: str | None = None, nick2: str | None = None, nick3: str | None = None, nick4: str | None = None, nick5: str | None = None, nick6: str | None = None, nick7: str | None = None, nick8: str | None = None, nick9: str | None = None, nick10: str | None = None, nick11: str | None = None, nick12: str | None = None, nick13: str | None = None, nick14: str | None = None, nick15: str | None = None, nick16: str | None = None, nick17: str | None = None, nick18: str | None = None, nick19: str | None = None, nick20: str | None = None, nick21: str | None = None, nick22: str | None = None):
    await interaction.response.defer()
    total_amount = parse_amount(amount)
    raw_nicks = []
    if players:
        raw_nicks += [n.strip() for n in re.split(r'[,\s]+', players) if n.strip()]
    for value in (nick1,
                  nick2,
                  nick3,
                  nick4,
                  nick5,
                  nick6,
                  nick7,
                  nick8,
                  nick9,
                  nick10,
                  nick11,
                  nick12,
                  nick13,
                  nick14,
                  nick15,
                  nick16,
                  nick17,
                  nick18,
                  nick19,
                  nick20,
                  nick21,
                  nick22):
        if value:
            raw_nicks.append(value)
    nicks = list(dict.fromkeys([await resolve_target(interaction.guild, n) for n in raw_nicks]))

    if not nicks:
        await send_app_error(interaction, text_embed('❌ Укажи участников: через поля nick1-20 или текстом в players', discord.Color.red()))
        return

    not_found = [n for n in nicks if get_player(n) is None]
    if not_found:
        embed = discord.Embed(
            title='❌ Операция отменена',
            description=f"Не найдены в базе: {', '.join(not_found)}",
            color=discord.Color.red()
        )
        embed.add_field(name='Что делать', value='Проверь написание. Новых игроков добавляй в таблицу, бот подхватит автоматически', inline=False)
        await send_app_error(interaction, embed)
        return

    count = len(nicks)
    per_person = int(total_amount / count)

    for n in nicks:
        db_run('UPDATE players SET balance = balance + ? WHERE lower(nick) = lower(?)', (per_person, n))

    add_log(officer_name(interaction.user), 'split', json.dumps({'amount': total_amount, 'per_person': per_person, 'nicks': nicks}, ensure_ascii=False))

    delivered, failed = await notify_split_participants(interaction.guild, nicks, per_person, officer_name(interaction.user))

    embed = discord.Embed(
        title='✅ Лут распределен!',
        description=f'**{interaction.user.display_name}** провел сплит',
        color=discord.Color.gold()
    )
    embed.add_field(name='Сумма к распределению', value=f'{total_amount:,.0f} 💰', inline=False)
    embed.add_field(name='На человека', value=f'**{per_person:,.0f}** 💵', inline=False)
    embed.add_field(name='Участников', value=f'{count} чел.', inline=True)

    notice = f'📩 Уведомлено: {delivered}/{count}'
    if failed:
        shown = ', '.join(failed[:5]) + ('...' if len(failed) > 5 else '')
        notice += f' (не найдены в Discord: {shown})'
    embed.add_field(name='Уведомления', value=notice, inline=False)

    embed.set_footer(text=f'CoE LootSplit • {datetime.now().strftime("%d.%m.%Y")}')
    await interaction.followup.send(embed=embed)
    trigger_sync()


for _i in range(1, 23):
    async def _split_autocomplete(interaction: discord.Interaction, current: str):
        return await nick_autocomplete(interaction, current)
    split.autocomplete(f'nick{_i}')(_split_autocomplete)


drafts = {}


class DraftPlayerSelect(discord.ui.Select):
    def __init__(self, author_id, page=0, search=""):
        self.author_id = author_id
        self.page = page
        self.search = search

        if search:
            rows = db_all("SELECT nick FROM players WHERE lower(nick) LIKE lower(?) ORDER BY nick LIMIT 25", (f"%{search}%",))
        else:
            rows = db_all("SELECT nick FROM players ORDER BY nick LIMIT 25 OFFSET ?", (page * 25,))

        options = [discord.SelectOption(label=r[0], value=r[0]) for r in rows]

        if not options:
            options = [discord.SelectOption(label="Никого не найдено", value="none")]

        super().__init__(
            placeholder=f"Выбери игроков... (стр. {page + 1})",
            min_values=1,
            max_values=min(25, len(options)),
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Это не твой черновик", ephemeral=True)
            return

        draft = drafts.get(self.author_id)
        if not draft:
            await interaction.response.send_message("❌ Черновик истёк. Начни заново: `/splitstart`", ephemeral=True)
            return

        if "none" in self.values:
            await interaction.response.send_message("❌ Выбери реального игрока", ephemeral=True)
            return

        added = 0
        for nick in self.values:
            if nick not in draft['nicks']:
                draft['nicks'].append(nick)
                added += 1

        draft['expires'] = datetime.now() + timedelta(minutes=30)

        embed = discord.Embed(
            title="📝 Черновик сплита",
            color=discord.Color.blue()
        )
        embed.add_field(name="Выбрано игроков", value=f"**{len(draft['nicks'])}** чел.", inline=True)
        embed.add_field(name="Добавлено сейчас", value=f"+{added}", inline=True)

        if draft['nicks']:
            list_text = '\n'.join(f"{i+1}. {n}" for i, n in enumerate(draft['nicks']))
            embed.add_field(name=f"Все выбранные ({len(draft['nicks'])})", value=list_text, inline=False)

        embed.set_footer(text="Черновик живёт 30 минут • /splitfinish сумма — завершить")

        view = DraftView(self.author_id, self.page, self.search)
        await interaction.response.edit_message(embed=embed, view=view)


class DraftView(discord.ui.View):
    def __init__(self, author_id, page=0, search=""):
        super().__init__(timeout=900)
        self.author_id = author_id
        self.page = page
        self.search = search

        self.add_item(DraftPlayerSelect(author_id, page, search))

        if page > 0:
            prev_btn = discord.ui.Button(label="◀️ Назад", style=discord.ButtonStyle.secondary)
            prev_btn.callback = self.prev_page
            self.add_item(prev_btn)

        next_btn = discord.ui.Button(label="Вперёд ▶️", style=discord.ButtonStyle.secondary)
        next_btn.callback = self.next_page
        self.add_item(next_btn)

        search_btn = discord.ui.Button(label="🔍 Поиск", style=discord.ButtonStyle.primary)
        search_btn.callback = self.show_search
        self.add_item(search_btn)

        clear_btn = discord.ui.Button(label="🗑️ Очистить", style=discord.ButtonStyle.danger)
        clear_btn.callback = self.clear_draft
        self.add_item(clear_btn)

    async def prev_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Это не твой черновик", ephemeral=True)
            return
        self.page -= 1
        await self.update_view(interaction)

    async def next_page(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Это не твой черновик", ephemeral=True)
            return
        self.page += 1
        await self.update_view(interaction)

    async def show_search(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Это не твой черновик", ephemeral=True)
            return

        await interaction.response.send_modal(SearchModal(self.author_id))

    async def clear_draft(self, interaction: discord.Interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("❌ Это не твой черновик", ephemeral=True)
            return

        draft = drafts.get(self.author_id)
        if draft:
            draft['nicks'] = []

        embed = discord.Embed(
            title="📝 Черновик сплита",
            description="Список очищен",
            color=discord.Color.blue()
        )
        embed.add_field(name="Выбрано игроков", value="0 чел.", inline=True)
        embed.set_footer(text="Черновик живёт 30 минут • /splitfinish сумма — завершить")

        view = DraftView(self.author_id)
        await interaction.response.edit_message(embed=embed, view=view)

    async def update_view(self, interaction: discord.Interaction):
        draft = drafts.get(self.author_id)
        if not draft:
            await interaction.response.send_message("❌ Черновик истёк", ephemeral=True)
            return

        embed = discord.Embed(
            title="📝 Черновик сплита",
            color=discord.Color.blue()
        )
        embed.add_field(name="Выбрано игроков", value=f"**{len(draft['nicks'])}** чел.", inline=True)

        if draft['nicks']:
            list_text = '\n'.join(f"{i+1}. {n}" for i, n in enumerate(draft['nicks']))
            embed.add_field(name=f"Все выбранные ({len(draft['nicks'])})", value=list_text, inline=False)

        embed.set_footer(text="Черновик живёт 30 минут • /splitfinish сумма — завершить")

        view = DraftView(self.author_id, self.page, self.search)
        await interaction.response.edit_message(embed=embed, view=view)


class SearchModal(discord.ui.Modal):
    def __init__(self, author_id):
        super().__init__(title="Поиск игрока")
        self.author_id = author_id
        self.search_input = discord.ui.TextInput(
            label="Введи часть ника",
            placeholder="Например: Kow",
            required=True,
            max_length=50
        )
        self.add_item(self.search_input)

    async def on_submit(self, interaction: discord.Interaction):
        search = self.search_input.value

        draft = drafts.get(self.author_id)
        if not draft:
            await interaction.response.send_message("❌ Черновик истёк", ephemeral=True)
            return

        embed = discord.Embed(
            title="📝 Черновик сплита",
            color=discord.Color.blue()
        )
        embed.add_field(name="Выбрано игроков", value=f"**{len(draft['nicks'])}** чел.", inline=True)
        embed.add_field(name="Поиск", value=f'"{search}"', inline=True)

        if draft['nicks']:
            list_text = '\n'.join(f"{i+1}. {n}" for i, n in enumerate(draft['nicks']))
            embed.add_field(name=f"Все выбранные ({len(draft['nicks'])})", value=list_text, inline=False)

        embed.set_footer(text="Черновик живёт 30 минут • /splitfinish сумма — завершить")

        view = DraftView(self.author_id, 0, search)
        await interaction.response.edit_message(embed=embed, view=view)


class DraftConfirm(discord.ui.View):
    def __init__(self, nicks, amount, author):
        super().__init__(timeout=60)
        self.nicks = nicks
        self.amount = amount
        self.author = author

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user == self.author

    @discord.ui.button(label='✅ Подтвердить сплит', style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)

        count = len(self.nicks)
        per_person = int(self.amount / count)
        author_name = officer_name(self.author)

        for n in self.nicks:
            db_run('UPDATE players SET balance = balance + ? WHERE lower(nick) = lower(?)', (per_person, n))

        add_log(author_name, 'split', json.dumps({
            'amount': self.amount,
            'per_person': per_person,
            'nicks': self.nicks
        }, ensure_ascii=False))

        delivered, failed = await notify_split_participants(interaction.guild, self.nicks, per_person, author_name)

        embed = discord.Embed(
            title='✅ Лут распределен!',
            description=f'**{self.author.display_name}** провел сплит (черновик)',
            color=discord.Color.gold()
        )
        embed.add_field(name='Сумма к распределению', value=f'{self.amount:,.0f} 💰', inline=False)
        embed.add_field(name='На человека', value=f'**{per_person:,.0f}** 💵', inline=False)
        embed.add_field(name='Участников', value=f'{count} чел.', inline=True)

        notice = f'📩 Уведомлено: {delivered}/{count}'
        if failed:
            shown = ', '.join(failed[:5]) + ('...' if len(failed) > 5 else '')
            notice += f' (не найдены в Discord: {shown})'
        embed.add_field(name='Уведомления', value=notice, inline=False)

        embed.set_footer(text=f'CoE LootSplit • {datetime.now().strftime("%d.%m.%Y")}')
        await interaction.followup.send(embed=embed)

        if self.author.id in drafts:
            del drafts[self.author.id]

        trigger_sync()

    @discord.ui.button(label='❌ Отмена', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=text_embed('❌ Сплит отменён'), view=self)
        if self.author.id in drafts:
            del drafts[self.author.id]


@bot.tree.command(name='splitstart', description='Начать большой сплит (выбор из списка)')
@app_commands.check(is_officer_interaction)
async def split_start(interaction: discord.Interaction):
    if interaction.user.id in drafts:
        draft = drafts[interaction.user.id]
        if datetime.now() < draft['expires']:
            draft['expires'] = datetime.now() + timedelta(minutes=30)

            embed = discord.Embed(
                title="📝 Черновик сплита (продолжение)",
                color=discord.Color.blue()
            )
            embed.add_field(name="Выбрано игроков", value=f"**{len(draft['nicks'])}** чел.", inline=True)

            if draft['nicks']:
                shown = draft['nicks'][-10:]
                list_text = '\n'.join(f"{i+1}. {n}" for i, n in enumerate(shown))
                if len(draft['nicks']) > 10:
                    list_text = f"... и ещё {len(draft['nicks']) - 10}\n" + list_text
                embed.add_field(name="Последние добавленные", value=list_text, inline=False)

            embed.set_footer(text="Черновик живёт 30 минут • /splitfinish сумма — завершить • 🆕 Новый — начать заново")

            view = DraftView(interaction.user.id)
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            return

    drafts[interaction.user.id] = {
        'nicks': [],
        'expires': datetime.now() + timedelta(minutes=30)
    }

    embed = discord.Embed(
        title="📝 Новый сплит (черновик)",
        description="Выбирай игроков из списка. Можно добавлять по 25 человек за раз.",
        color=discord.Color.blue()
    )
    embed.add_field(name="Выбрано", value="0 чел.", inline=True)
    embed.add_field(name="Лимит времени", value="30 минут", inline=True)
    embed.set_footer(text="Когда все выбраны — напиши: /splitfinish сумма")

    view = DraftView(interaction.user.id)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name='splitfinish', description='Завершить черновик и провести сплит')
@app_commands.check(is_officer_interaction)
@app_commands.describe(amount='Сумма: 50м, 50m или 50000000')
async def split_finish(interaction: discord.Interaction, amount: str):
    await interaction.response.defer(ephemeral=True)

    draft = drafts.get(interaction.user.id)
    if not draft:
        await interaction.followup.send(
            embed=text_embed('❌ Черновик не найден или истёк. Начни заново: `/splitstart`', discord.Color.red()),
            ephemeral=True
        )
        return

    if datetime.now() > draft['expires']:
        del drafts[interaction.user.id]
        await interaction.followup.send(
            embed=text_embed('❌ Черновик истёк (30 минут). Начни заново: `/splitstart`', discord.Color.red()),
            ephemeral=True
        )
        return

    nicks = draft['nicks']
    if not nicks:
        await interaction.followup.send(
            embed=text_embed('❌ В черновике никого нет. Сначала выбери игроков через `/splitstart`', discord.Color.red()),
            ephemeral=True
        )
        return

    total_amount = parse_amount(amount)
    count = len(nicks)
    per_person = int(total_amount / count)

    not_found = [n for n in nicks if get_player(n) is None]
    if not_found:
        await interaction.followup.send(
            embed=text_embed(f"❌ Не найдены в базе: {', '.join(not_found)}", discord.Color.red()),
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="⚠️ Подтверждение сплита",
        color=discord.Color.orange()
    )
    embed.add_field(name="Игроков", value=f"**{count}** чел.", inline=True)
    embed.add_field(name="Сумма", value=f"**{total_amount:,.0f}** 💰", inline=True)
    embed.add_field(name="Каждому", value=f"**{per_person:,.0f}** 💵", inline=True)

    shown = nicks[:10]
    list_text = '\n'.join(f"• {n}" for n in shown)
    if count > 10:
        list_text += f"\n... и ещё {count - 10}"
    embed.add_field(name="Участники", value=list_text, inline=False)

    embed.set_footer(text="Кнопки живут 60 секунд")

    view = DraftConfirm(nicks, total_amount, interaction.user)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@bot.tree.command(name='pay', description='Выплатить накопленный баланс игроку')
@app_commands.check(is_officer_interaction)
@app_commands.describe(target='Ник игрока')
async def pay(interaction: discord.Interaction, target: str):
    await interaction.response.defer()
    target = await resolve_target(interaction.guild, target)
    player = get_player(target)
    if player is None:
        await interaction.followup.send(embed=text_embed(f'❌ Игрок **{target}** не найден в базе', discord.Color.red()))
        return
    if player[1] == 0:
        await interaction.followup.send(embed=text_embed(f'❌ У **{target}** баланс уже ноль', discord.Color.red()))
        return
    db_run('UPDATE players SET balance = 0 WHERE lower(nick) = lower(?)', (target,))
    add_log(officer_name(interaction.user), 'pay', json.dumps({'nick': target, 'amount': player[1]}, ensure_ascii=False))

    member = find_member_by_nick(interaction.guild, target)
    if member:
        try:
            await member.send(embed=discord.Embed(
                title=f'💸 Выплата | {GUILD_NAME}',
                description=f'Тебе выплачено: **{player[1]:,}**\n💼 Баланс обнулён.\nВыплатил: {officer_name(interaction.user)}',
                color=discord.Color.green()
            ))
        except Exception:
            pass

    embed = discord.Embed(
        title=f'💸 Выплата: {target}',
        color=discord.Color.green()
    )
    embed.add_field(name='Выдано', value=f'**{player[1]:,.0f}** 💰', inline=False)
    embed.add_field(name='Баланс', value='0 (обнулён)', inline=False)
    await interaction.followup.send(embed=embed)
    trigger_sync()

@pay.autocomplete('target')
async def pay_target_autocomplete(interaction: discord.Interaction, current: str):
    return await nick_autocomplete(interaction, current)


class PayAllConfirm(discord.ui.View):
    def __init__(self, rows, author):
        super().__init__(timeout=60)
        self.rows = rows
        self.author = author

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user == self.author

    @discord.ui.button(label='✅ Подтвердить выплату', style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(view=self)

        rows = self.rows
        total = sum(r[1] for r in rows)
        db_run('UPDATE players SET balance = 0 WHERE balance > 0')
        add_log(officer_name(self.author), 'pay_all', json.dumps({'payouts': {r[0]: r[1] for r in rows}}, ensure_ascii=False))

        for nick, amount in {r[0]: r[1] for r in rows}.items():
            member = find_member_by_nick(interaction.guild, nick)
            if member:
                try:
                    await member.send(embed=discord.Embed(
                        title=f'💸 Выплата | {GUILD_NAME}',
                        description=f'Тебе выплачено: **{amount:,}**\n💼 Баланс обнулён.\nВыплатил: {officer_name(self.author)}',
                        color=discord.Color.green()
                    ))
                except Exception:
                    pass

        embed = discord.Embed(
            title='💸 Массовая выплата',
            color=discord.Color.green()
        )
        embed.add_field(name='Выплачено игроков', value=f'{len(rows)} чел.', inline=True)
        embed.add_field(name='Общая сумма', value=f'{total:,.0f} 💰', inline=True)
        await interaction.followup.send(embed=embed)
        trigger_sync()

    @discord.ui.button(label='❌ Отмена', style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(embed=text_embed('❌ Массовая выплата отменена'), view=self)


@bot.tree.command(name='payall', description='Выплатить ВСЕМ накопленные балансы')
@app_commands.check(is_officer_interaction)
async def payall(interaction: discord.Interaction):
    rows = db_all('SELECT nick, balance FROM players WHERE balance > 0 ORDER BY balance DESC')
    if not rows:
        await send_app_error(interaction, text_embed('❌ Нет игроков с положительным балансом', discord.Color.red()))
        return
    total = sum(r[1] for r in rows)
    top_lines = [f'• **{r[0]}** - {r[1]:,}' for r in rows[:10]]
    if len(rows) > 10:
        top_lines.append(f'... и ещё {len(rows) - 10}')
    embed = discord.Embed(
        title='⚠️ Подтверждение массовой выплаты',
        description='\n'.join(top_lines),
        color=discord.Color.orange()
    )
    embed.add_field(name='Игроков', value=f'{len(rows)} чел.', inline=True)
    embed.add_field(name='Суммарно', value=f'**{total:,.0f}** 💰', inline=True)
    embed.set_footer(text='Кнопки живут 60 секунд. Кнопки жмёт только вызвавший.')
    await interaction.response.send_message(embed=embed, view=PayAllConfirm(rows, interaction.user))


@bot.tree.command(name='balance', description='Проверить баланс')
@app_commands.describe(nick='Ник игрока (пусто = свой баланс)')
async def balance(interaction: discord.Interaction, nick: str | None = None):
    if nick is None:
        nick = clean_nick(interaction.user.display_name)
    nick = await resolve_target(interaction.guild, nick)

    player = get_player(nick)
    if player is None:
        embed = discord.Embed(
            title='❌ Игрок не найден',
            description=f'Ник **{nick}** отсутствует в базе',
            color=discord.Color.red()
        )
        embed.add_field(name='Возможные причины', value='• Опечатка в нике\n• Игрок не добавлен в таблицу\n• Другой ник в игре и Discord', inline=False)
        await send_app_error(interaction, embed)
        return

    embed = discord.Embed(
        title=f'💼 Баланс: {player[0]}',
        color=discord.Color.blue()
    )
    embed.add_field(name='Текущий баланс', value=f'**{player[1]:,.0f}** 💰', inline=False)
    await interaction.response.send_message(embed=embed)


@balance.autocomplete('nick')
async def balance_nick_autocomplete(interaction: discord.Interaction, current: str):
    return await nick_autocomplete(interaction, current)


@bot.tree.command(name='set', description='Ручная правка баланса')
@app_commands.check(is_officer_interaction)
@app_commands.describe(nick='Ник игрока', amount='Новый баланс целым числом')
async def set_balance(interaction: discord.Interaction, nick: str, amount: int):
    nick = await resolve_target(interaction.guild, nick)
    player = get_player(nick)
    if player is None:
        await send_app_error(interaction, text_embed(f'❌ Игрок **{nick}** не найден в базе', discord.Color.red()))
        return
    db_run('UPDATE players SET balance = ? WHERE lower(nick) = lower(?)', (amount, nick))
    add_log(officer_name(interaction.user), 'set', json.dumps({'nick': nick, 'old': player[1], 'new': amount}, ensure_ascii=False))
    await interaction.response.send_message(embed=text_embed(f'✅ Баланс **{nick}** изменён: **{player[1]:,}** → **{amount:,.0f}** 💰', discord.Color.green()))
    trigger_sync()


@set_balance.autocomplete('nick')
async def set_nick_autocomplete(interaction: discord.Interaction, current: str):
    return await nick_autocomplete(interaction, current)


@bot.tree.command(name='compens', description='Добавить компенсацию к балансу игрока')
@app_commands.check(is_officer_interaction)
@app_commands.describe(nick='Ник игрока', amount='Сумма: 5м, 5m или 5000000')
async def compens(interaction: discord.Interaction, nick: str, amount: str):
    await interaction.response.defer()
    nick = await resolve_target(interaction.guild, nick)
    player = get_player(nick)
    if player is None:
        await interaction.followup.send(embed=text_embed(f'❌ Игрок **{nick}** не найден в базе', discord.Color.red()), ephemeral=True)
        return
    value = int(parse_amount(amount))
    db_run('UPDATE players SET balance = balance + ? WHERE lower(nick) = lower(?)', (value, nick))
    new_balance = get_player(nick)[1]
    add_log(officer_name(interaction.user), 'compens', json.dumps({'nick': nick, 'amount': value}, ensure_ascii=False))

    member = find_member_by_nick(interaction.guild, nick)
    if member:
        try:
            await member.send(embed=discord.Embed(
                title=f'🎁 Компенсация | {GUILD_NAME}',
                description=f'Тебе выдана компенсация: **+{value:,}**\n💼 Баланс: **{new_balance:,}**\nВыдал: {officer_name(interaction.user)}',
                color=discord.Color.teal()
            ))
        except Exception:
            pass

    await interaction.followup.send(embed=text_embed(f'✅ Компенсация **{nick}**: +{value:,.0f} 💰 (баланс: {new_balance:,})', discord.Color.green()))
    trigger_sync()


@compens.autocomplete('nick')
async def compens_nick_autocomplete(interaction: discord.Interaction, current: str):
    return await nick_autocomplete(interaction, current)


@bot.tree.command(name='undo', description='Отменить операцию')
@app_commands.check(is_officer_interaction)
@app_commands.describe(op_id='Номер операции (пусто = отменить последнюю)')
async def undo(interaction: discord.Interaction, op_id: int | None = None):
    if op_id is None:
        rows = db_all("SELECT id FROM operations WHERE op_type NOT LIKE '%cancelled%' AND op_type != 'undo' ORDER BY id DESC LIMIT 1")
        if not rows:
            await send_app_error(interaction, text_embed('❌ Нечего отменять', discord.Color.red()))
            return
        op_id = rows[0][0]

    rows = db_all('SELECT op_type, details FROM operations WHERE id = ?', (op_id,))
    if not rows:
        await send_app_error(interaction, text_embed(f'❌ Операция #{op_id} не найдена', discord.Color.red()))
        return

    op_type, details = rows[0]

    if op_type.endswith('cancelled'):
        await send_app_error(interaction, text_embed(f'❌ Операция #{op_id} уже была отменена', discord.Color.red()))
        return
    if op_type == 'undo':
        await send_app_error(interaction, text_embed('❌ Отмену отменить нельзя', discord.Color.red()))
        return
    if op_type.startswith('sheet'):
        await send_app_error(interaction, text_embed('❌ Операции из таблицы отменить нельзя. Правь баланс через `/set`', discord.Color.red()))
        return

    try:
        data = json.loads(details)
    except json.JSONDecodeError:
        await send_app_error(interaction, text_embed('❌ Операция в старом формате, автоматический откат невозможен. Используй `/set`', discord.Color.red()))
        return

    if op_type == 'split':
        for n in data['nicks']:
            db_run('UPDATE players SET balance = balance - ? WHERE lower(nick) = lower(?)', (data['per_person'], n))
        summary = f"сплит {data['amount']:,.0f} ({len(data['nicks'])} чел, {data['per_person']:,.0f} каждому)"
    elif op_type == 'pay':
        db_run('UPDATE players SET balance = balance + ? WHERE lower(nick) = lower(?)', (data['amount'], data['nick']))
        summary = f"выплата {data['nick']}: {data['amount']:,.0f}"
    elif op_type == 'pay_all':
        for nick, amount in data['payouts'].items():
            db_run('UPDATE players SET balance = balance + ? WHERE lower(nick) = lower(?)', (amount, nick))
        summary = f"массовая выплата ({len(data['payouts'])} чел)"
    elif op_type == 'set':
        db_run('UPDATE players SET balance = ? WHERE lower(nick) = lower(?)', (data['old'], data['nick']))
        summary = f"правка {data['nick']}: возвращено {data['old']:,.0f}"
    elif op_type == 'compens':
        db_run('UPDATE players SET balance = balance - ? WHERE lower(nick) = lower(?)', (data['amount'], data['nick']))
        summary = f"компенсация {data['nick']}: снято {data['amount']:,.0f}"
    else:
        await send_app_error(interaction, text_embed(f'❌ Тип {op_type} не поддерживает откат', discord.Color.red()))
        return

    db_run('UPDATE operations SET op_type = ? WHERE id = ?', (op_type + '_cancelled', op_id))
    add_log(officer_name(interaction.user), 'undo', f'отменена операция #{op_id}: {summary}')
    await interaction.response.send_message(embed=text_embed(f'✅ Операция #{op_id} отменена: {summary}', discord.Color.green()))
    trigger_sync()


@bot.tree.command(name='last', description='Последние операции')
@app_commands.check(is_officer_interaction)
@app_commands.describe(count='Сколько показать (по умолчанию 5, максимум 20)')
async def last_ops(interaction: discord.Interaction, count: int = 5):
    if count > 20:
        count = 20
    rows = db_all('SELECT id, date, author, op_type, details FROM operations ORDER BY id DESC LIMIT ?', (count,))
    if not rows:
        await interaction.response.send_message(embed=text_embed('📜 История пуста'))
        return
    lines = []
    for r in rows:
        display = format_details(r[3], r[4]) if not r[3].startswith('sheet') and r[3] != 'undo' else r[4]
        lines.append(f'`#{r[0]}` **{r[1]}** | {r[2]} | {display}')
    embed = discord.Embed(
        title='📜 Последние операции',
        description='\n'.join(lines),
        color=discord.Color.greyple()
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='sync', description='Синхронизация с таблицей')
@app_commands.check(is_officer_interaction)
async def manual_sync(interaction: discord.Interaction):
    await interaction.response.send_message(embed=text_embed('⏳ Синхронизирую с таблицей...'))
    success = await sync_sheets()
    if success:
        await interaction.edit_original_response(embed=text_embed('✅ Синхронизация завершена', discord.Color.green()))
    else:
        await interaction.edit_original_response(embed=text_embed('❌ Синхронизация не удалась, попробуй позже', discord.Color.red()))


@bot.tree.command(name='help', description='Список команд')
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title='📖 CoE LootSplit - Команды',
        color=discord.Color.dark_gold()
    )
    embed.add_field(
        name='💰 Экономика',
        value='`/balance` - свой баланс\n`/balance ник` - баланс игрока\n`/history` - своя история операций\n`/history ник` - история операций игрока\n`/week` - статистика за неделю',
        inline=False
    )
    if is_officer(interaction.user):
        embed.add_field(
            name='💸 Лут',
            value='`/split` - распределить лут (до 22 чел)\n`/splitstart` + `/splitfinish` - большой сплит (50+ чел, выбор из списка)\n`/pay` - выплатить игроку\n`/payall` - выплатить всем\n`/payouts` - балансы к выдаче',
            inline=False
        )
        embed.add_field(
            name='⚙️ Управление',
            value='`/set` - правка баланса\n`/compens` - компенсация (+сумма)\n`/undo` - отменить операцию\n`/last` - история операций\n`/sync` - синхронизация с таблицей\n`/check` - полная диагностика бота',
            inline=False
        )
        embed.color = discord.Color.gold()
        embed.set_footer(text=f'CoE LootSplit • {GUILD_NAME} • Режим: Офицер')
    else:
        embed.set_footer(text=f'CoE LootSplit • {GUILD_NAME}')
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='check', description='Полная диагностика бота')
@app_commands.check(is_officer_interaction)
async def system_check(interaction: discord.Interaction):
    await interaction.response.send_message(embed=text_embed('🔍 Делаю диагностику...'))

    checks = []

    discord_ok = bot.is_ready()
    checks.append(('Discord', f'задержка {round(bot.latency * 1000)} мс', discord_ok))

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                'https://discord.com/api/v10/users/@me',
                headers={'Authorization': f'Bot {DISCORD_TOKEN}'},
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                checks.append(('Токен Discord', f'HTTP {response.status}', response.status == 200))
    except Exception as e:
        checks.append(('Токен Discord', f'ошибка: {type(e).__name__}', False))

    try:
        player_count = db_all('SELECT COUNT(*) FROM players')[0][0]
        integrity = db_all('PRAGMA integrity_check')[0][0]
        checks.append(('База SQLite', f'{player_count} игроков, целостность: {integrity}', integrity == 'ok' and player_count > 0))
    except Exception as e:
        checks.append(('База SQLite', f'ошибка: {type(e).__name__}', False))

    t0 = time.monotonic()
    sheet_players = await sheet_get_players()
    elapsed = round(time.monotonic() - t0, 1)
    if sheet_players is None:
        checks.append(('Google API', 'не отвечает', False))
    else:
        checks.append(('Google API', f'ответ за {elapsed} с, в таблице игроков: {len(sheet_players)}', True))

    pending_logs = db_all('SELECT COUNT(*) FROM operations WHERE synced = 0')[0][0]
    sync_state = 'ещё не выполнялась после запуска'
    if last_sync_at is not None:
        sync_state = ('успешно' if last_sync_ok else 'ошибка') + last_sync_at.strftime(' в %H:%M:%S')
    fresh = last_sync_at is not None and datetime.now() - last_sync_at < timedelta(minutes=HEARTBEAT_STALE_MIN)
    checks.append(('Синхронизация', f'{sync_state}; неотправленных логов: {pending_logs}',
                   last_sync_ok is True and fresh and pending_logs == 0))

    disk = shutil.disk_usage('/')
    free_gb = disk.free / (1024 ** 3)
    checks.append(('Диск VPS', f'свободно {free_gb:.1f} ГБ', free_gb > 1))

    with open('/proc/meminfo') as f:
        meminfo = f.read()
    avail_mb = int([line for line in meminfo.split('\n') if line.startswith('MemAvailable')][0].split()[1]) / 1024
    checks.append(('RAM VPS', f'свободно {avail_mb:.0f} МБ', avail_mb > 100))

    lines = [f'{"🟢" if ok else "🔴"} **{name}** - {detail}' for name, detail, ok in checks]
    all_ok = all(ok for _, _, ok in checks)
    embed = discord.Embed(
        title='🔍 Диагностика бота',
        description='\n'.join(lines),
        color=discord.Color.green() if all_ok else discord.Color.red()
    )
    embed.set_footer(text=f'CoE LootSplit • {datetime.now().strftime("%d.%m.%Y %H:%M")}')
    await interaction.edit_original_response(embed=embed)


@bot.tree.command(name='history', description='История операций игрока')
@app_commands.describe(nick='Ник игрока (пусто = своя история)')
async def player_history(interaction: discord.Interaction, nick: str | None = None):
    if nick is None:
        nick = clean_nick(interaction.user.display_name)
    nick = await resolve_target(interaction.guild, nick)

    player = get_player(nick)
    if player is None:
        await send_app_error(interaction, text_embed(f'❌ Игрок **{nick}** не найден в базе', discord.Color.red()))
        return

    rows = db_all('SELECT id, date, author, op_type, details FROM operations ORDER BY id DESC LIMIT 1000')
    target = player[0].lower()

    ops = []
    total_earned = 0
    total_paid = 0
    splits_count = 0

    for r in rows:
        op_id, date, author, op_type, details = r
        base_type = op_type.removesuffix('_cancelled')
        cancelled = op_type.endswith('_cancelled')

        try:
            data = json.loads(details)
        except (json.JSONDecodeError, TypeError):
            data = None

        affected = False
        amount = 0

        if data:
            if base_type == 'split':
                nicks_lower = [n.lower() for n in data.get('nicks', [])]
                if target in nicks_lower:
                    affected = True
                    amount = data['per_person']
                    if not cancelled:
                        total_earned += amount
                        splits_count += 1
            elif base_type == 'pay_all':
                payouts_lower = {k.lower(): v for k, v in data.get('payouts', {}).items()}
                if target in payouts_lower:
                    affected = True
                    amount = payouts_lower[target]
                    if not cancelled:
                        total_paid += amount
            elif base_type == 'pay':
                if str(data.get('nick', '')).lower() == target:
                    affected = True
                    amount = data.get('amount', 0)
                    if not cancelled:
                        total_paid += amount
            elif base_type == 'set':
                if str(data.get('nick', '')).lower() == target:
                    affected = True

        if affected:
            ops.append((date, author, base_type, amount, cancelled, data))

    embed = discord.Embed(
        title=f'📜 История: {player[0]}',
        color=discord.Color.blue()
    )
    embed.add_field(name='💰 Всего начислено', value=f'{total_earned:,.0f}', inline=True)
    embed.add_field(name='💸 Всего выплачено', value=f'{total_paid:,.0f}', inline=True)
    embed.add_field(name='💼 Текущий баланс', value=f'**{player[1]:,.0f}**', inline=True)

    lines = []
    for date, author, base_type, amount, cancelled, data in ops[:10]:
        marker = '↩️' if cancelled else {'split': '✅', 'pay': '💸', 'pay_all': '💸', 'set': '✏️'}.get(base_type, '•')
        if base_type == 'set':
            lines.append(f'{marker} {date} | правка {data["old"]:,} → {data["new"]:,} ({author})')
        else:
            sign = '+' if base_type == 'split' else '-'
            lines.append(f'{marker} {date} | {sign}{amount:,.0f} ({author})')

    if lines:
        embed.description = '\n'.join(lines)
    else:
        embed.description = 'Операций за последние 1000 записей не найдено'

    await interaction.response.send_message(embed=embed)


@player_history.autocomplete('nick')
async def history_nick_autocomplete(interaction: discord.Interaction, current: str):
    return await nick_autocomplete(interaction, current)


@bot.tree.command(name='week', description='Статистика за календарную неделю')
async def weekly_stats(interaction: discord.Interaction):
    nick = clean_nick(interaction.user.display_name)
    player = get_player(nick)
    if player is None:
        await send_app_error(interaction, text_embed(f'❌ Игрок **{nick}** не найден в базе', discord.Color.red()))
        return

    now = datetime.now()
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    prev_start = week_start - timedelta(days=7)

    rows = db_all('SELECT date, op_type, details FROM operations ORDER BY id DESC LIMIT 2000')
    target = player[0].lower()

    earned = 0
    split_count = 0
    cancelled_splits = 0
    paid = 0
    transferred = 0
    set_notes = []
    earned_prev = 0

    for date_str, op_type, details in rows:
        try:
            op_date = datetime.strptime(date_str, '%d.%m.%Y %H:%M')
        except ValueError:
            continue

        this_week = week_start <= op_date <= now
        last_week = prev_start <= op_date < week_start
        if not this_week and not last_week:
            continue

        base_type = op_type.removesuffix('_cancelled')
        cancelled = op_type.endswith('_cancelled')

        try:
            data = json.loads(details)
        except (json.JSONDecodeError, TypeError):
            data = None

        amount = 0
        affected = False

        if data:
            if base_type == 'split':
                nicks_lower = [n.lower() for n in data.get('nicks', [])]
                if target in nicks_lower:
                    affected = True
                    amount = data['per_person']
            elif base_type == 'pay_all':
                payouts_lower = {k.lower(): v for k, v in data.get('payouts', {}).items()}
                if target in payouts_lower:
                    affected = True
                    amount = payouts_lower[target]
            elif base_type in ('pay', 'set'):
                if str(data.get('nick', '')).lower() == target:
                    affected = True
                    amount = data.get('amount', 0)
        elif base_type in ('sheet_add', 'sheet_edit'):
            if details.lower().startswith(target + ':') or target + ':' in details.lower():
                affected = True

        if not affected:
            continue

        if this_week:
            if base_type == 'split':
                if cancelled:
                    cancelled_splits += 1
                else:
                    earned += amount
                    split_count += 1
            elif base_type in ('pay', 'pay_all'):
                if not cancelled:
                    paid += amount
            elif base_type in ('sheet_add', 'sheet_edit'):
                numbers = [int(x.replace(' ', '')) for x in re.findall(r'\d[\d ]*', details)]
                if numbers:
                    transferred += numbers[-1]
            elif base_type == 'set':
                set_notes.append(f'{data["old"]:,} → {data["new"]:,}')
        elif last_week and base_type == 'split' and not cancelled:
            earned_prev += amount

    week_label = f'{week_start.strftime("%d.%m")} - {(week_start + timedelta(days=6)).strftime("%d.%m")}'
    embed = discord.Embed(
        title=f'📊 Твоя неделя: {week_label}',
        color=discord.Color.gold()
    )

    lines = [
        f'💰 Начислено: **{earned:,.0f}** ({split_count} {plural(split_count, ("сплит", "сплита", "сплитов"))})',
    ]
    if split_count > 0:
        lines.append(f'📈 Средняя за сплит: {int(earned / split_count):,}')
    lines.append(f'💸 Выплачено: {paid:,.0f}')

    if cancelled_splits > 0:
        lines.append(f'↩️ Отменено сплитов: {cancelled_splits}')
    if transferred > 0:
        lines.append(f'📋 Перенесено из таблицы: {transferred:,.0f}')
    if set_notes:
        lines.append(f'✏️ Ручная правка: {set_notes[0]}')

    if earned_prev > 0 and earned > 0:
        pct = round((earned - earned_prev) / earned_prev * 100)
        marker = '📈' if pct >= 0 else '📉'
        lines.append(f'{marker} vs прошлой неделей: {pct:+d}%')
    elif earned_prev == 0 and earned > 0:
        lines.append('🔥 Первая активная неделя!')

    lines.append('')
    lines.append(f'💼 Баланс сейчас: **{player[1]:,}**')

    embed.description = '\n'.join(lines)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name='payouts', description='Кто ждёт выплат')
@app_commands.check(is_officer_interaction)
async def payouts_list(interaction: discord.Interaction):
    rows = db_all('SELECT nick, balance FROM players WHERE balance > 0 ORDER BY balance DESC')

    if not rows:
        await interaction.response.send_message(embed=text_embed('💸 Никто не ждёт выплат. Все балансы обнулены', discord.Color.green()))
        return

    total = sum(r[1] for r in rows)
    lines = [f'• **{r[0]}** - {r[1]:,}' for r in rows[:20]]
    if len(rows) > 20:
        lines.append(f'... и ещё {len(rows) - 20}')

    embed = discord.Embed(
        title=f'💸 К выплате: {len(rows)} {plural(len(rows), ("игрок", "игрока", "игроков"))}',
        description='\n'.join(lines),
        color=discord.Color.green()
    )
    embed.add_field(name='Суммарно', value=f'**{total:,.0f}** 💰', inline=False)
    embed.set_footer(text='Выплата: /pay или /payall')
    await interaction.response.send_message(embed=embed)


if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)