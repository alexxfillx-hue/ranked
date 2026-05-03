# cogs/ban.py
import re
import datetime
import discord
from discord.ext import commands, tasks

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
    "m": "min.",
    "h": "hr.",
    "d": "day(s)",
    "w": "week(s)",
}


_FOREVER_DATE = datetime.datetime(9999, 12, 31, 23, 59, 59)


def parse_duration(raw: str) -> datetime.timedelta | None:
    """Разбирает строку вида '15m', '2h', '3d', '1w' или 'forever' в timedelta.
    Возвращает None при ошибке."""
    if raw.strip().lower() == "forever":
        return _FOREVER_DATE - datetime.datetime.utcnow()
    m = _DURATION_RE.match(raw.strip())
    if not m:
        return None
    amount, unit = int(m.group(1)), m.group(2).lower()
    return datetime.timedelta(seconds=amount * _UNIT_SECONDS[unit])


def fmt_duration(raw: str) -> str:
    """Человекочитаемое описание для embed'а."""
    if raw.strip().lower() == "forever":
        return "♾️ Forever"
    m = _DURATION_RE.match(raw.strip())
    if not m:
        return raw
    amount, unit = m.group(1), m.group(2).lower()
    return f"{amount} {_UNIT_NAMES[unit]}"


def fmt_until(dt: datetime.datetime) -> str:
    """Форматирует дату окончания бана."""
    if dt >= _FOREVER_DATE.replace(year=9999):
        return "♾️ Forever"
    return dt.strftime("%d.%m.%Y %H:%M UTC")


# ── Хелпер: проверка роли модератора ──────────────────────────────────────────

def _is_moderator(member: discord.Member) -> bool:
    return any(r.name == Config.MODERATOR_ROLE_NAME for r in member.roles)


# ── Хелпер: снять ранговые роли и выдать/снять BANNED ─────────────────────────

async def _apply_ban_roles(guild: discord.Guild, member: discord.Member, ban: bool, bot=None):
    """
    ban=True  → снимает все ранговые роли, выдаёт BANNED
    ban=False → снимает BANNED и возвращает ранговую роль по текущему ELO
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

    # При снятии бана — сразу восстанавливаем ранговую роль по текущему ELO
    if not ban and bot is not None:
        try:
            from cogs.register import Register
            reg_cog: Register = bot.cogs.get("Register")
            if reg_cog:
                player = await bot.db.get_player(member.id)
                if player:
                    await reg_cog._sync_rank_role(member, player["elo"])
        except Exception:
            pass


# ── Cog ───────────────────────────────────────────────────────────────────────

class Ban(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ban_expiry_loop.start()

    def cog_unload(self):
        self.ban_expiry_loop.cancel()

    def _is_guild(self, ctx) -> bool:
        return bool(ctx.guild and ctx.guild.id == Config.GUILD_ID)

    # ── Фоновая задача: автоснятие истёкших банов ─────────────────────────────

    @tasks.loop(minutes=1)
    async def ban_expiry_loop(self):
        """Каждую минуту проверяет истёкшие баны и снимает их автоматически."""
        try:
            expired = await self.bot.db.get_expired_bans()
        except Exception:
            return

        if not expired:
            return

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            return

        for ban_row in expired:
            discord_id = ban_row["discord_id"]
            await self.bot.db.remove_ban(discord_id)

            member = guild.get_member(discord_id)
            if member:
                await _apply_ban_roles(guild, member, ban=False, bot=self.bot)
                # Уведомляем игрока в ЛС
                try:
                    embed = discord.Embed(
                        title="✅  Your ban has expired",
                        color=0x2ECC71,
                        description=(
                            "Your ban on this server has expired.\n"
                            "You can use all bot features again."
                        ),
                    )
                    await member.send(embed=embed)
                except (discord.Forbidden, discord.HTTPException):
                    pass

    @ban_expiry_loop.before_loop
    async def before_ban_expiry_loop(self):
        await self.bot.wait_until_ready()

    # ── !ban @игрок <длительность> ────────────────────────────────────────────

    @commands.command(name="ban")
    async def ban_cmd(self, ctx: commands.Context, member: discord.Member = None, duration: str = None):
        if not self._is_guild(ctx):
            return

        if not _is_moderator(ctx.author):
            await ctx.send("❌ Only moderators can use this command.")
            return

        if member is None or duration is None:
            await ctx.send(
                "⚠️ Usage: `!ban @player <duration>`\n"
                "Duration: `15m` (minutes), `2h` (hours), `3d` (days), `1w` (weeks), `forever`\n"
                "Example: `!ban @player 15m`  or  `!ban @player forever`"
            )
            return

        delta = parse_duration(duration)
        if delta is None:
            await ctx.send(
                "⚠️ Invalid duration format.\n"
                "Examples: `15m`, `2h`, `3d`, `1w`, `forever`"
            )
            return

        # Нельзя банить модератора
        if _is_moderator(member):
            await ctx.send("❌ You cannot ban a moderator.")
            return

        # Нельзя банить самого себя
        if member.id == ctx.author.id:
            await ctx.send("❌ You cannot ban yourself.")
            return

        player = await self.bot.db.get_player(member.id)
        if not player:
            await ctx.send(f"❌ Player {member.mention} is not registered.")
            return

        banned_until = datetime.datetime.utcnow() + delta

        # Записываем бан в БД
        await self.bot.db.set_ban(member.id, banned_until, ctx.author.id, duration)

        # Меняем роли
        await _apply_ban_roles(ctx.guild, member, ban=True, bot=self.bot)

        embed = discord.Embed(
            title="🔨  Player banned",
            color=0xFF0000,
        )
        embed.add_field(name="👤 Player", value=member.mention, inline=True)
        embed.add_field(name="⏱ Duration", value=fmt_duration(duration), inline=True)
        embed.add_field(name="📅 Until", value=fmt_until(banned_until), inline=True)
        embed.add_field(name="🛡 Moderator", value=ctx.author.mention, inline=True)
        embed.set_footer(text=f"ID: {member.id}")

        await ctx.send(embed=embed)

        # Уведомляем самого игрока в ЛС (если возможно)
        try:
            dm_embed = discord.Embed(
                title="🔨  You have been banned on this server",
                color=0xFF0000,
                description=(
                    f"**Duration:** {fmt_duration(duration)}\n"
                    f"**Until:** {fmt_until(banned_until)}\n\n"
                    "You cannot use bot features until your ban expires."
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
            await ctx.send("❌ Only moderators can use this command.")
            return

        if member is None:
            await ctx.send("⚠️ Usage: `!unban @player`")
            return

        player = await self.bot.db.get_player(member.id)
        if not player:
            await ctx.send(f"❌ Player {member.mention} is not registered.")
            return

        ban_info = await self.bot.db.get_ban(member.id)
        if not ban_info:
            await ctx.send(f"ℹ️ {member.mention} is not banned.")
            return

        await self.bot.db.remove_ban(member.id)
        await _apply_ban_roles(ctx.guild, member, ban=False, bot=self.bot)

        embed = discord.Embed(
            title="✅  Ban lifted",
            color=0x2ECC71,
        )
        embed.add_field(name="👤 Player", value=member.mention, inline=True)
        embed.add_field(name="🛡 Moderator", value=ctx.author.mention, inline=True)
        await ctx.send(embed=embed)

        # Уведомляем игрока
        try:
            await member.send("✅ Your ban on this server has been lifted. You can use the bot again.")
        except (discord.Forbidden, discord.HTTPException):
            pass


async def setup(bot):
    await bot.add_cog(Ban(bot))
