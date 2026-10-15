import discord
import json
from discord import app_commands
from discord.ext import commands
from datetime import datetime, timedelta

from config import GUILD_NAME
from db import db_all, db_run, get_player, add_log
from sheets import trigger_sync
from utils import (
    parse_amount, clean_nick, resolve_target, officer_name,
    is_officer_interaction, text_embed,  send_app_error
)
from cogs.economy import notify_split_participants


drafts = {}


class DraftPlayerSelect(discord.ui.Select):
    def __init__(self, author_id, offset, label, exclude=None):
        self.author_id = author_id
        self.offset = offset
        self.exclude = exclude or []

        rows = db_all("SELECT nick FROM players ORDER BY nick LIMIT 25 OFFSET ?", (offset,))
        rows = [r for r in rows if r[0] not in self.exclude]
        options = [discord.SelectOption(label=r[0], value=r[0]) for r in rows]

        if not options:
            options = [discord.SelectOption(label="-", value="none")]

        super().__init__(
            placeholder=label,
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
        if added:
            embed.add_field(name="Добавлено", value=f"+{added}", inline=True)

        if draft['nicks']:
            list_text = '\n'.join(f"{i+1}. {n}" for i, n in enumerate(draft['nicks']))
            embed.add_field(name=f"Все выбранные ({len(draft['nicks'])})", value=list_text, inline=False)

        embed.set_footer(text="Выбирай из списков или /splitfinish сумма - завершить")

        view = DraftView(self.author_id, draft['nicks'])
        await interaction.response.edit_message(embed=embed, view=view)


class DraftView(discord.ui.View):
    def __init__(self, author_id, exclude=None):
        super().__init__(timeout=1800)
        self.author_id = author_id

        total = db_all('SELECT COUNT(*) FROM players')[0][0]
        offsets = [0, 25, 50, 75, 100]
        labels = ['Игроки 1-25', 'Игроки 26-50', 'Игроки 51-75', 'Игроки 76-100', 'Игроки 101-125']

        for offset, label in zip(offsets, labels):
            if offset < total:
                self.add_item(DraftPlayerSelect(author_id, offset, label, exclude))

        clear_btn = discord.ui.Button(label="🗑️ Очистить", style=discord.ButtonStyle.danger)
        clear_btn.callback = self.clear_draft
        self.add_item(clear_btn)

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
        embed.set_footer(text="Выбирай из списков или /splitfinish сумма - завершить")

        view = DraftView(self.author_id)
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


class Drafts(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name='splitstart', description='Начать большой сплит (поиск по нику)')
    @app_commands.check(is_officer_interaction)
    async def split_start(self, interaction: discord.Interaction):
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
                    list_text = '\n'.join(f"{i+1}. {n}" for i, n in enumerate(draft['nicks']))
                    embed.add_field(name=f"Все выбранные ({len(draft['nicks'])})", value=list_text, inline=False)

                embed.set_footer(text="Печатай ник или /splitfinish сумма - завершить")

                view = DraftView(interaction.user.id, draft['nicks'])
                await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
                return

        drafts[interaction.user.id] = {
            'nicks': [],
            'expires': datetime.now() + timedelta(minutes=30)
        }

        embed = discord.Embed(
            title="📝 Новый сплит (черновик)",
            description="Выбирай игроков из списков ниже. Каждый список - до 25 человек.",
            color=discord.Color.blue()
        )
        embed.add_field(name="Выбрано", value="0 чел.", inline=True)
        embed.add_field(name="Лимит времени", value="30 минут", inline=True)
        embed.set_footer(text="Когда все выбраны - напиши: /splitfinish сумма")

        view = DraftView(interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name='splitfinish', description='Завершить черновик и провести сплит')
    @app_commands.check(is_officer_interaction)
    @app_commands.describe(amount='Сумма: 50м, 50m или 50000000')
    async def split_finish(self, interaction: discord.Interaction, amount: str):
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


async def setup(bot):
    await bot.add_cog(Drafts(bot))