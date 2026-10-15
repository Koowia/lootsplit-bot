import re
import json
import discord
from config import OFFICER_ROLE_IDS


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


def is_officer(member: discord.Member) -> bool:
    if member.guild.owner_id == member.id:
        return True
    user_role_ids = [role.id for role in member.roles]
    return any(role_id in user_role_ids for role_id in OFFICER_ROLE_IDS)


def is_officer_interaction(interaction) -> bool:
    return is_officer(interaction.user)


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


async def send_app_error(interaction: discord.Interaction, embed):
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def nick_autocomplete(interaction: discord.Interaction, current: str):
    from db import db_all
    rows = db_all("SELECT nick FROM players WHERE lower(nick) LIKE lower(?) ORDER BY nick LIMIT 25", (f"%{current}%",))
    return [discord.app_commands.Choice(name=r[0], value=r[0]) for r in rows]