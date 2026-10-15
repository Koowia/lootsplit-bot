import discord
import json
import re
from discord import app_commands
from discord.ext import commands
from datetime import datetime

from config import GUILD_NAME
from db import db_all, db_run, get_player, add_log
from sheets import trigger_sync
from utils import (
    parse_amount, plural, clean_nick, resolve_target, officer_name,
    find_member_by_nick, is_officer_interaction, access_denied, text_embed,
    send_app_error, nick_autocomplete
)


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


class Economy(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name='split', description='Распределить лут между игроками')
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
    async def split(self, interaction: discord.Interaction, amount: str, players: str | None = None,
                    nick1: str | None = None, nick2: str | None = None, nick3: str | None = None, nick4: str | None = None, nick5: str | None = None, nick6: str | None = None, nick7: str | None = None, nick8: str | None = None, nick9: str | None = None, nick10: str | None = None, nick11: str | None = None, nick12: str | None = None, nick13: str | None = None, nick14: str | None = None, nick15: str | None = None, nick16: str | None = None, nick17: str | None = None, nick18: str | None = None, nick19: str | None = None, nick20: str | None = None, nick21: str | None = None, nick22: str | None = None):
        await interaction.response.defer()
        total_amount = parse_amount(amount)
        raw_nicks = []
        if players:
            raw_nicks += [n.strip() for n in re.split(r'[,\s]+', players) if n.strip()]
        for value in (nick1, nick2, nick3, nick4, nick5, nick6, nick7, nick8, nick9, nick10,
                      nick11, nick12, nick13, nick14, nick15, nick16, nick17, nick18, nick19, nick20,
                      nick21, nick22):
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

    @app_commands.command(name='pay', description='Выплатить накопленный баланс игроку')
    @app_commands.check(is_officer_interaction)
    @app_commands.describe(target='Ник игрока')
    async def pay(self, interaction: discord.Interaction, target: str):
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

    @app_commands.command(name='payall', description='Выплатить ВСЕМ накопленные балансы')
    @app_commands.check(is_officer_interaction)
    async def payall(self, interaction: discord.Interaction):
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

    @app_commands.command(name='set', description='Ручная правка баланса')
    @app_commands.check(is_officer_interaction)
    @app_commands.describe(nick='Ник игрока', amount='Новый баланс целым числом')
    async def set_balance(self, interaction: discord.Interaction, nick: str, amount: int):
        nick = await resolve_target(interaction.guild, nick)
        player = get_player(nick)
        if player is None:
            await send_app_error(interaction, text_embed(f'❌ Игрок **{nick}** не найден в базе', discord.Color.red()))
            return
        db_run('UPDATE players SET balance = ? WHERE lower(nick) = lower(?)', (amount, nick))
        add_log(officer_name(interaction.user), 'set', json.dumps({'nick': nick, 'old': player[1], 'new': amount}, ensure_ascii=False))
        await interaction.response.send_message(embed=text_embed(f'✅ Баланс **{nick}** изменён: **{player[1]:,}** → **{amount:,.0f}** 💰', discord.Color.green()))
        trigger_sync()

    @app_commands.command(name='compens', description='Добавить компенсацию к балансу игрока')
    @app_commands.check(is_officer_interaction)
    @app_commands.describe(nick='Ник игрока', amount='Сумма: 5м, 5m или 5000000')
    async def compens(self, interaction: discord.Interaction, nick: str, amount: str):
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


# Автодополнение
for _i in range(1, 23):
    async def _split_autocomplete(interaction: discord.Interaction, current: str):
        return await nick_autocomplete(interaction, current)
    Economy.split.autocomplete(f'nick{_i}')(_split_autocomplete)

Economy.pay.autocomplete('target')(nick_autocomplete)
Economy.set_balance.autocomplete('nick')(nick_autocomplete)
Economy.compens.autocomplete('nick')(nick_autocomplete)


async def setup(bot):
    await bot.add_cog(Economy(bot))