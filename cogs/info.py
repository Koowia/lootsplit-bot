import discord
import json
import re
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta

from db import db_all, get_player
from utils import (
    clean_nick, resolve_target, text_embed, send_app_error,
    nick_autocomplete, plural, is_officer_interaction
)


class Info(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name='balance', description='Проверить баланс')
    @app_commands.describe(nick='Ник игрока (пусто = свой баланс)')
    async def balance(self, interaction: discord.Interaction, nick: str | None = None):
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

    @app_commands.command(name='history', description='История операций игрока')
    @app_commands.describe(nick='Ник игрока (пусто = своя история)')
    async def player_history(self, interaction: discord.Interaction, nick: str | None = None):
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

    @app_commands.command(name='week', description='Статистика за календарную неделю')
    async def weekly_stats(self, interaction: discord.Interaction):
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

    @app_commands.command(name='payouts', description='Кто ждёт выплат')
    @app_commands.check(is_officer_interaction)
    async def payouts_list(self, interaction: discord.Interaction):
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


# Автодополнение
Info.balance.autocomplete('nick')(nick_autocomplete)
Info.player_history.autocomplete('nick')(nick_autocomplete)


async def setup(bot):
    await bot.add_cog(Info(bot))