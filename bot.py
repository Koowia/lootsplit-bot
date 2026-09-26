import discord
from discord.ext import commands, tasks
import aiohttp
import asyncio
import sqlite3
import json
import re
import os
import shutil
import time
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
    if op_type == 'split':
        return f"Сплит {data['amount']:,.0f} между {len(data['nicks'])} чел (по {data['per_person']:,.0f}): {', '.join(data['nicks'])}"
    if op_type == 'pay':
        return f"Выплата {data['nick']}: {data['amount']:,.0f}"
    if op_type == 'pay_all':
        return f"Массовая выплата {len(data['payouts'])} чел на {sum(data['payouts'].values()):,.0f}"
    if op_type == 'set':
        return f"Баланс {data['nick']}: {data['old']:,.0f} → {data['new']:,.0f}"
    return details


async def sheet_request(data, retries=3):
    for attempt in range(retries):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(GOOGLE_SCRIPT_URL, json=data, timeout=aiohttp.ClientTimeout(total=30)) as response:
                    text = await response.text()
                    if not text.startswith('{'):
                        print(f'⚠️ Скрипт вернул не-JSON (попытка {attempt + 1}): {text[:200]}')
                        if attempt < retries - 1:
                            await asyncio.sleep(3)
                            continue
                    return text
        except Exception as e:
            print(f'⚠️ Ошибка запроса к скрипту (попытка {attempt + 1}): {type(e).__name__}')
            if attempt < retries - 1:
                await asyncio.sleep(3)
    return 'ERROR'


async def sheet_get_players():
    result = await sheet_request({'action': 'get_all'})
    if result.startswith('ERROR'):
        return None
    try:
        data = json.loads(result)
    except json.JSONDecodeError:
        return None
    return data.get('players', [])


async def sync_sheets():
    sheet_players = await sheet_get_players()
    if sheet_players is None:
        return False

    for item in sheet_players:
        nick = str(item['nick']).strip()
        sheet_balance = int(item['balance'])
        player = get_player(nick)
        if player is None:
            db_run('INSERT INTO players (nick, balance, last_pushed) VALUES (?, ?, ?)', (nick, sheet_balance, sheet_balance))
            add_log('Таблица', 'sheet_add', f'{nick}: {sheet_balance}')
        elif sheet_balance != player[1] and player[1] == player[2]:
            db_run('UPDATE players SET balance = ?, last_pushed = ? WHERE lower(nick) = lower(?)', (sheet_balance, sheet_balance, nick))
            add_log('Таблица', 'sheet_edit', f'{nick}: {player[1]} → {sheet_balance}')

    players = [{'nick': r[0], 'balance': r[1]} for r in db_all('SELECT nick, balance FROM players')]
    logs = [{'date': r[0], 'author': r[1], 'type': r[2], 'details': format_details(r[2], r[3])}
            for r in db_all('SELECT date, author, op_type, details FROM operations WHERE synced = 0')]
    if not players:
        print('⚠️ Отказ синка: база пуста, таблица не тронута')
        return False
    result = await sheet_request({'action': 'sync', 'players': players, 'logs': logs})
    if result.startswith('ERROR'):
        return False

    db_run('UPDATE players SET last_pushed = balance')
    db_run('UPDATE operations SET synced = 1 WHERE synced = 0')
    await sheet_request({
        'action': 'heartbeat',
        'status': '🟢 Бот работает',
        'time': datetime.now().strftime('%d.%m.%Y %H:%M'),
        'players_count': len(players)
    })
    return True


def clean_nick(discord_nick: str) -> str:
    nick = discord_nick
    for _ in range(3):
        nick = re.sub(r'^\[.*?\]\s*', '', nick)
        nick = re.sub(r'^!+\s*', '', nick)
    return nick.strip()


async def resolve_target(ctx, raw: str) -> str:
    match = re.fullmatch(r'<@!?(\d+)>', raw.strip())
    if match:
        member = ctx.guild.get_member(int(match.group(1)))
        if member:
            return clean_nick(member.display_name)
    return clean_nick(raw.replace('@', '').strip())


def officer_name(ctx) -> str:
    return clean_nick(ctx.author.display_name)


def find_member_by_nick(guild, nick):
    target = clean_nick(nick).lower()
    for member in guild.members:
        if clean_nick(member.display_name).lower() == target:
            return member
    return None


async def notify_split_participants(ctx, nicks, per_person, author_name):
    delivered = 0
    failed = []
    for nick in nicks:
        member = find_member_by_nick(ctx.guild, nick)
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


def is_officer_ctx(ctx) -> bool:
    return is_officer(ctx.author)


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


def trigger_sync():
    asyncio.create_task(sync_sheets())


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
    print(f'✅ Бот {bot.user} запущен!')
    print('✅ SQLite подключена')
    periodic_sync.start()
    daily_backup.start()
    await crash_alert_check()


@tasks.loop(minutes=SYNC_INTERVAL_MIN)
async def periodic_sync():
    await sync_sheets()


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
    if result.startswith('{'):
        print(f'✅ Бэкап сохранён: {len(players)} игроков')
    else:
        print('⚠️ Бэкап не сохранился')


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        embed = discord.Embed(
            title='❌ Неизвестная команда',
            description=f"Команда `{ctx.message.content.split()[0]}` не существует",
            color=discord.Color.red()
        )
        embed.add_field(name='Что делать', value='Напиши `!help` чтобы увидеть список команд', inline=False)
        await ctx.send(embed=embed)
    elif isinstance(error, commands.CheckFailure):
        await ctx.send(embed=access_denied())
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=text_embed('❌ Не хватает аргументов. Напиши `!help` для справки.', discord.Color.red()))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=text_embed('❌ Неверный формат аргументов. Напиши `!help` для справки.', discord.Color.red()))
    else:
        print(f'⚠️ Ошибка: {error}')


@bot.command(name='split')
@commands.check(is_officer_ctx)
async def split(ctx, amount: str, *, players_arg: str):
    total_amount = parse_amount(amount)
    raw_nicks = [n.strip() for n in re.split(r'[,\s]+', players_arg) if n.strip()]
    nicks = list(dict.fromkeys([await resolve_target(ctx, n) for n in raw_nicks]))

    if not nicks:
        await ctx.send(embed=text_embed('❌ Укажи участников! Пример: `!split 30м @ник1, @ник2`', discord.Color.red()))
        return

    not_found = [n for n in nicks if get_player(n) is None]
    if not_found:
        embed = discord.Embed(
            title='❌ Операция отменена',
            description=f"Не найдены в базе: {', '.join(not_found)}",
            color=discord.Color.red()
        )
        embed.add_field(name='Что делать', value='Проверь написание. Новых игроков добавляй в таблицу (столбец B), бот подхватит автоматически', inline=False)
        await ctx.send(embed=embed)
        return

    count = len(nicks)
    per_person = int(total_amount / count)

    for n in nicks:
        db_run('UPDATE players SET balance = balance + ? WHERE lower(nick) = lower(?)', (per_person, n))

    add_log(officer_name(ctx), 'split', json.dumps({'amount': total_amount, 'per_person': per_person, 'nicks': nicks}, ensure_ascii=False))

    delivered, failed = await notify_split_participants(ctx, nicks, per_person, officer_name(ctx))

    embed = discord.Embed(
        title='✅ Лут распределен!',
        description=f'**{ctx.author.display_name}** провел сплит',
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
    await ctx.send(embed=embed)
    trigger_sync()


@bot.command(name='pay')
@commands.check(is_officer_ctx)
async def pay(ctx, target: str | None = None):
    if target is None or target.lower() == 'all':
        rows = db_all('SELECT nick, balance FROM players WHERE balance > 0')
        if not rows:
            await ctx.send(embed=text_embed('❌ Нет игроков с положительным балансом', discord.Color.red()))
            return
        total = sum(r[1] for r in rows)
        db_run('UPDATE players SET balance = 0 WHERE balance > 0')
        add_log(officer_name(ctx), 'pay_all', json.dumps({'payouts': {r[0]: r[1] for r in rows}}, ensure_ascii=False))

        for nick, amount in {r[0]: r[1] for r in rows}.items():
            member = find_member_by_nick(ctx.guild, nick)
            if member:
                try:
                    await member.send(embed=discord.Embed(
                        title=f'💸 Выплата | {GUILD_NAME}',
                        description=f'Тебе выплачено: **{amount:,}**\n💼 Баланс обнулён.\nВыплатил: {officer_name(ctx)}',
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
        await ctx.send(embed=embed)
    else:
        target = await resolve_target(ctx, target)
        player = get_player(target)
        if player is None:
            await ctx.send(embed=text_embed(f'❌ Игрок **{target}** не найден в базе', discord.Color.red()))
            return
        if player[1] == 0:
            await ctx.send(embed=text_embed(f'❌ У **{target}** баланс уже ноль', discord.Color.red()))
            return
        db_run('UPDATE players SET balance = 0 WHERE lower(nick) = lower(?)', (target,))
        add_log(officer_name(ctx), 'pay', json.dumps({'nick': target, 'amount': player[1]}, ensure_ascii=False))

        member = find_member_by_nick(ctx.guild, target)
        if member:
            try:
                await member.send(embed=discord.Embed(
                    title=f'💸 Выплата | {GUILD_NAME}',
                    description=f'Тебе выплачено: **{player[1]:,}**\n💼 Баланс обнулён.\nВыплатил: {officer_name(ctx)}',
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
        await ctx.send(embed=embed)
    trigger_sync()


@bot.command(name='balance')
async def balance(ctx, *, nick: str | None = None):
    if nick is None:
        nick = clean_nick(ctx.author.display_name)
    nick = await resolve_target(ctx, nick)

    player = get_player(nick)
    if player is None:
        embed = discord.Embed(
            title='❌ Игрок не найден',
            description=f'Ник **{nick}** отсутствует в базе',
            color=discord.Color.red()
        )
        embed.add_field(name='Возможные причины', value='• Опечатка в нике\n• Игрок не добавлен в таблицу\n• Другой ник в игре и Discord', inline=False)
        await ctx.send(embed=embed)
        return

    embed = discord.Embed(
        title=f'💼 Баланс: {player[0]}',
        color=discord.Color.blue()
    )
    embed.add_field(name='Текущий баланс', value=f'**{player[1]:,.0f}** 💰', inline=False)
    await ctx.send(embed=embed)


@bot.command(name='set')
@commands.check(is_officer_ctx)
async def set_balance(ctx, nick: str, amount: int):
    nick = await resolve_target(ctx, nick)
    player = get_player(nick)
    if player is None:
        await ctx.send(embed=text_embed(f'❌ Игрок **{nick}** не найден в базе', discord.Color.red()))
        return
    db_run('UPDATE players SET balance = ? WHERE lower(nick) = lower(?)', (amount, nick))
    add_log(officer_name(ctx), 'set', json.dumps({'nick': nick, 'old': player[1], 'new': amount}, ensure_ascii=False))
    await ctx.send(embed=text_embed(f'✅ Баланс **{nick}** изменён: **{player[1]:,}** → **{amount:,.0f}** 💰', discord.Color.green()))
    trigger_sync()


@bot.command(name='undo')
@commands.check(is_officer_ctx)
async def undo(ctx, op_id: int | None = None):
    if op_id is None:
        rows = db_all("SELECT id FROM operations WHERE op_type NOT LIKE '%cancelled%' AND op_type != 'undo' ORDER BY id DESC LIMIT 1")
        if not rows:
            await ctx.send(embed=text_embed('❌ Нечего отменять', discord.Color.red()))
            return
        op_id = rows[0][0]

    rows = db_all('SELECT op_type, details FROM operations WHERE id = ?', (op_id,))
    if not rows:
        await ctx.send(embed=text_embed(f'❌ Операция #{op_id} не найдена', discord.Color.red()))
        return

    op_type, details = rows[0]

    if op_type.endswith('cancelled'):
        await ctx.send(embed=text_embed(f'❌ Операция #{op_id} уже была отменена', discord.Color.red()))
        return
    if op_type == 'undo':
        await ctx.send(embed=text_embed('❌ Отмену отменить нельзя', discord.Color.red()))
        return
    if op_type.startswith('sheet'):
        await ctx.send(embed=text_embed('❌ Операции из таблицы отменить нельзя — правь баланс через `!set`', discord.Color.red()))
        return

    try:
        data = json.loads(details)
    except json.JSONDecodeError:
        await ctx.send(embed=text_embed('❌ Операция в старом формате, автоматический откат невозможен. Используй `!set`', discord.Color.red()))
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
    else:
        await ctx.send(embed=text_embed(f'❌ Тип {op_type} не поддерживает откат', discord.Color.red()))
        return

    db_run('UPDATE operations SET op_type = ? WHERE id = ?', (op_type + '_cancelled', op_id))
    add_log(officer_name(ctx), 'undo', f'отменена операция #{op_id}: {summary}')
    await ctx.send(embed=text_embed(f'✅ Операция #{op_id} отменена: {summary}', discord.Color.green()))
    trigger_sync()


@bot.command(name='last')
@commands.check(is_officer_ctx)
async def last_ops(ctx, count: int = 5):
    if count > 20:
        count = 20
    rows = db_all('SELECT id, date, author, op_type, details FROM operations ORDER BY id DESC LIMIT ?', (count,))
    if not rows:
        await ctx.send(embed=text_embed('📜 История пуста'))
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
    await ctx.send(embed=embed)


@bot.command(name='sync')
@commands.check(is_officer_ctx)
async def manual_sync(ctx):
    msg = await ctx.send(embed=text_embed('⏳ Синхронизирую с таблицей...'))
    success = await sync_sheets()
    if success:
        await msg.edit(embed=text_embed('✅ Синхронизация завершена', discord.Color.green()))
    else:
        await msg.edit(embed=text_embed('❌ Синхронизация не удалась, попробуй позже', discord.Color.red()))


@bot.command(name='help')
async def help_command(ctx):
    embed = discord.Embed(
        title='📖 CoE LootSplit - Команды',
        color=discord.Color.dark_gold()
    )
    embed.add_field(
        name='💰 Экономика',
        value='`!balance` - свой баланс\n`!balance [ник]` - баланс игрока\n`!history` - своя история операций\n`!history [ник]` - история операции игрока\n`!week` - статистика за неделю',
        inline=False
    )
    if is_officer(ctx.author):
        embed.add_field(
            name='💸 Лут',
            value='`!split <сумма> <ники>` - распределить\n`!pay <ник>` - выплатить всё\n`!pay all` - выплатить всех\n`!payouts` - балансы к выдаче',
            inline=False
        )
        embed.add_field(
            name='⚙️ Управление',
            value='`!set <ник> <сумма>` - правка баланса\n`!undo [номер]` - отменить операцию (номер из `!last`)\n`!last` - история операций\n`!sync` - синхронизация с таблицей\n`!check` - полная диагностика бота',
            inline=False
        )
        embed.color = discord.Color.gold()
        embed.set_footer(text=f'CoE LootSplit • {GUILD_NAME} • Режим: Офицер')
    else:
        embed.set_footer(text=f'CoE LootSplit • {GUILD_NAME}')
    await ctx.send(embed=embed)


@bot.command(name='check')
@commands.check(is_officer_ctx)
async def system_check(ctx):
    msg = await ctx.send(embed=text_embed('🔍 Делаю диагностику...'))

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
    checks.append(('Синхронизация', f'неотправленных логов: {pending_logs}', pending_logs < 50))

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
    await msg.edit(content='', embed=embed)


@bot.command(name='history')
async def player_history(ctx, *, nick: str | None = None):
    if nick is None:
        nick = clean_nick(ctx.author.display_name)
    nick = await resolve_target(ctx, nick)

    player = get_player(nick)
    if player is None:
        await ctx.send(embed=text_embed(f'❌ Игрок **{nick}** не найден в базе', discord.Color.red()))
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

    await ctx.send(embed=embed)


@bot.command(name='week')
async def weekly_stats(ctx):
    nick = clean_nick(ctx.author.display_name)
    player = get_player(nick)
    if player is None:
        await ctx.send(embed=text_embed(f'❌ Игрок **{nick}** не найден в базе', discord.Color.red()))
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

    week_label = f'{week_start.strftime("%d.%m")} — {(week_start + timedelta(days=6)).strftime("%d.%m")}'
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
    await ctx.send(embed=embed)


@bot.command(name='payouts')
@commands.check(is_officer_ctx)
async def payouts_list(ctx):
    rows = db_all('SELECT nick, balance FROM players WHERE balance > 0 ORDER BY balance DESC')

    if not rows:
        await ctx.send(embed=text_embed('💸 Никто не ждёт выплат - все балансы обнулены', discord.Color.green()))
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
    embed.set_footer(text='Выплата: !pay <ник> или !pay all')
    await ctx.send(embed=embed)


if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)