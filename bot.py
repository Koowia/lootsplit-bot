import discord
from discord import app_commands
from discord.ext import commands, tasks
import asyncio
import json
import os
from datetime import datetime, time as dt_time

from config import DISCORD_TOKEN, SYNC_INTERVAL_MIN, STARTUP_STATE_FILE, CRASH_WINDOW_MIN, CRASH_ALERT_THRESHOLD
from db import db_all, db_run, db_init
from sheets import sheet_get_players, sync_sheets, sheet_request, sheet_json
from utils import access_denied, text_embed, send_app_error


intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents, help_command=None)


@bot.event
async def on_ready():
    db_init()
    
    # Миграция при первом запуске
    if not db_all('SELECT nick FROM players LIMIT 1'):
        sheet_players = await sheet_get_players()
        if sheet_players:
            for item in sheet_players:
                db_run('INSERT OR IGNORE INTO players (nick, balance, last_pushed) VALUES (?, ?, ?)',
                       (str(item['nick']).strip(), int(item['balance']), int(item['balance'])))
            print(f'✅ Миграция: импортировано {len(sheet_players)} игроков')
    
    # Загрузка модулей
    for cog in ['cogs.economy', 'cogs.drafts', 'cogs.info', 'cogs.admin']:
        try:
            await bot.load_extension(cog)
            print(f'✅ Загружен: {cog}')
        except Exception as e:
            print(f'❌ Ошибка загрузки {cog}: {e}')
    
    # Синхронизация команд
    for guild in bot.guilds:
        await bot.tree.sync(guild=guild)
    await bot.tree.sync()
    
    print(f'✅ Бот {bot.user} запущен!')
    print('✅ SQLite подключена')
    
    # Запуск фоновых задач
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
        from config import ALERT_OWNER_IDS
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


if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)