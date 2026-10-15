import discord
import json
import time
import shutil
import aiohttp
from discord import app_commands
from discord.ext import commands, tasks
from datetime import datetime, time as dt_time, timedelta

from config import (
    DISCORD_TOKEN, GUILD_NAME, SYNC_INTERVAL_MIN,
    HEARTBEAT_STALE_MIN, STARTUP_STATE_FILE, CRASH_WINDOW_MIN, CRASH_ALERT_THRESHOLD
)
from db import db_all, db_run, get_player, add_log, db_init
from sheets import (
    sheet_request, sheet_json, sheet_get_players, sync_sheets, trigger_sync
)
import sheets
from utils import (
    clean_nick, resolve_target, officer_name, is_officer_interaction,
    is_officer, access_denied, text_embed, format_details, 
    send_app_error
)


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_sync_ok = None
        self.last_sync_at = None

    @app_commands.command(name='sync', description='Синхронизация с таблицей')
    @app_commands.check(is_officer_interaction)
    async def manual_sync(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=text_embed('⏳ Синхронизирую с таблицей...'))
        success = await sync_sheets()
        if success:
            await interaction.edit_original_response(embed=text_embed('✅ Синхронизация завершена', discord.Color.green()))
        else:
            await interaction.edit_original_response(embed=text_embed('❌ Синхронизация не удалась, попробуй позже', discord.Color.red()))

    @app_commands.command(name='check', description='Полная диагностика бота')
    @app_commands.check(is_officer_interaction)
    async def system_check(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=text_embed('🔍 Делаю диагностику...'))

        checks = []

        discord_ok = self.bot.is_ready()
        checks.append(('Discord', f'задержка {round(self.bot.latency * 1000)} мс', discord_ok))

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
        if sheets.last_sync_at is not None:
            sync_state = ('успешно' if sheets.last_sync_ok else 'ошибка') + sheets.last_sync_at.strftime(' в %H:%M:%S')
        fresh = sheets.last_sync_at is not None and datetime.now() - sheets.last_sync_at < timedelta(minutes=HEARTBEAT_STALE_MIN)
        checks.append(('Синхронизация', f'{sync_state}; неотправленных логов: {pending_logs}',
               sheets.last_sync_ok is True and fresh and pending_logs == 0))

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

    @app_commands.command(name='last', description='Последние операции')
    @app_commands.check(is_officer_interaction)
    @app_commands.describe(count='Сколько показать (по умолчанию 5, максимум 20)')
    async def last_ops(self, interaction: discord.Interaction, count: int = 5):
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

    @app_commands.command(name='undo', description='Отменить операцию')
    @app_commands.check(is_officer_interaction)
    @app_commands.describe(op_id='Номер операции (пусто = отменить последнюю)')
    async def undo(self, interaction: discord.Interaction, op_id: int | None = None):
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

    @app_commands.command(name='help', description='Список команд')
    async def help_command(self, interaction: discord.Interaction):
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


async def setup(bot):
    await bot.add_cog(Admin(bot))