# cogs/ban.py
import re
import datetime
import discord
from discord.ext import commands

from config import Config, RANKS


# ── Парсинг длительности бана ──────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^(\d+)(m|h|d|w)$", re.IGNORECASE)

_UNIT_SECONDS = {
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}

_UNIT_NAMES = {
    "m": "мин.",
    "h": "ч.",
    "d": "дн.",
    "w": "нед.",
}


def parse_duration(raw: str) -> datetime.timedelta | None:
    """Разбирает строку вида '15m', '2h', '3d', '1w' в timedelta. Возвращает None при ошибке."""
    m = _DURATION_RE.match(raw.strip())
    if not m:
        return None
    amount, unit = int(m.group(1)), m.group(2).lower()
    return datetime.timedelta(seconds=amount * _UNIT_SECONDS[unit])


def fmt_duration(raw: str) -> str:
    """Человекочитаемое описание для embed'а."""
    m = _DURATION_RE.match(raw.strip())
    if not m:
        return raw
    amount, unit = m.group(1), m.group(2).lower()
    return f"{amount} {_UNIT_NAMES[unit]}"


def fmt_until(dt: datetime.datetime) -> str:
    """Форматирует дату окончания бана."""
    return dt.strftime("%d.%m.%Y %H:%M UTC")


# ── Хелпер: проверка роли модератора ──────────────────────────────────────────

def _is_moderator(member: discord.Member) -> bool:
    return any(r.name == Config.MODERATOR_ROLE_NAME for r in member.roles)


# ── Хелпер: снять ранговые роли и выдать/снять BANNED ─────────────────────────

async def _apply_ban_roles(guild: discord.Guild, member: discord.Member, ban: bool):
    """
    ban=True  → снимает все ранговые роли, выдаёт BANNED
    ban=False → снимает BANNED (ранговая роль восстановится при следующей смене ELO)
    """
    banned_role = discord.utils.get(guild.roles, name="BANNED")
    rank_roles = {r[4] for r in RANKS}  # названия ранговых ролей из config

    roles_to_remove = []
    roles_to_add = []

    if ban:
        for role in member.roles:
            if role.name in rank_roles:
                roles_to_remove.append(role)
        if banned_role and banned_role not in member.roles:
            roles_to_add.append(banned_role)
    else:
        if banned_role and banned_role in member.roles:
            roles_to_remove.append(banned_role)

    try:
        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason="Ban system")
        if roles_to_add:
            await member.add_roles(*roles_to_add, reason="Ban system")
    except discord.Forbidden:
        pass  # нет прав — молча пропускаем


# ── Cog ───────────────────────────────────────────────────────────────────────

class Ban(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _is_guild(self, ctx) -> bool:
        return bool(ctx.guild and ctx.guild.id == Config.GUILD_ID)

    # ── !ban @игрок <длительность> ────────────────────────────────────────────

    @commands.command(name="ban")
    async def ban_cmd(self, ctx: commands.Context, member: discord.Member = None, duration: str = None):
        if not self._is_guild(ctx):
            return

        if not _is_moderator(ctx.author):
            await ctx.send("❌ Только модераторы могут использовать эту команду.")
            return

        if member is None or duration is None:
            await ctx.send(
                "⚠️ Использование: `!ban @игрок <длительность>`\n"
                "Длительность: `15m` (мин.), `2h` (часы), `3d` (дни), `1w` (неделя)\n"
                "Пример: `!ban @игрок 15m`"
            )
            return

        delta = parse_duration(duration)
        if delta is None:
            await ctx.send(
                "⚠️ Неверный формат длительности.\n"
                "Примеры: `15m`, `2h`, `3d`, `1w`"
            )
            return

        # Нельзя банить модератора
        if _is_moderator(member):
            await ctx.send("❌ Нельзя заблокировать модератора.")
            return

        # Нельзя банить самого себя
        if member.id == ctx.author.id:
            await ctx.send("❌ Нельзя заблокировать самого себя.")
            return

        player = await self.bot.db.get_player(member.id)
        if not player:
            await ctx.send(f"❌ Игрок {member.mention} не зарегистрирован.")
            return

        banned_until = datetime.datetime.utcnow() + delta

        # Записываем бан в БД
        await self.bot.db.set_ban(member.id, banned_until, ctx.author.id, duration)

        # Меняем роли
        await _apply_ban_roles(ctx.guild, member, ban=True)

        embed = discord.Embed(
            title="🔨  Игрок заблокирован",
            color=0xFF0000,
        )
        embed.add_field(name="👤 Игрок", value=member.mention, inline=True)
        embed.add_field(name="⏱ Длительность", value=fmt_duration(duration), inline=True)
        embed.add_field(name="📅 До", value=fmt_until(banned_until), inline=True)
        embed.add_field(name="🛡 Модератор", value=ctx.author.mention, inline=True)
        embed.set_footer(text=f"ID: {member.id}")

        await ctx.send(embed=embed)

        # Уведомляем самого игрока в ЛС (если возможно)
        try:
            dm_embed = discord.Embed(
                title="🔨  Вы заблокированы на этом сервере",
                color=0xFF0000,
                description=(
                    f"**Длительность:** {fmt_duration(duration)}\n"
                    f"**До:** {fmt_until(banned_until)}\n\n"
                    "До окончания бана вы не можете пользоваться функциями бота."
                ),
            )
            await member.send(embed=dm_embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ── !unban @игрок ─────────────────────────────────────────────────────────

    @commands.command(name="unban")
    async def unban_cmd(self, ctx: commands.Context, member: discord.Member = None):
        if not self._is_guild(ctx):
            return

        if not _is_moderator(ctx.author):
            await ctx.send("❌ Только модераторы могут использовать эту команду.")
            return

        if member is None:
            await ctx.send("⚠️ Использование: `!unban @игрок`")
            return

        player = await self.bot.db.get_player(member.id)
        if not player:
            await ctx.send(f"❌ Игрок {member.mention} не зарегистрирован.")
            return

        ban_info = await self.bot.db.get_ban(member.id)
        if not ban_info:
            await ctx.send(f"ℹ️ {member.mention} не заблокирован.")
            return

        await self.bot.db.remove_ban(member.id)
        await _apply_ban_roles(ctx.guild, member, ban=False)

        embed = discord.Embed(
            title="✅  Блокировка снята",
            color=0x2ECC71,
        )
        embed.add_field(name="👤 Игрок", value=member.mention, inline=True)
        embed.add_field(name="🛡 Модератор", value=ctx.author.mention, inline=True)
        await ctx.send(embed=embed)

        # Уведомляем игрока
        try:
            await member.send("✅ Ваша блокировка на сервере снята. Вы снова можете пользоваться ботом.")
        except (discord.Forbidden, discord.HTTPException):
            pass


async def setup(bot):
    await bot.add_cog(Ban(bot))
