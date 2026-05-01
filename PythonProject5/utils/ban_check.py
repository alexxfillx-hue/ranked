# utils/ban_check.py
"""
Хелпер для проверки бана игрока.

Использование в любом коге:
    from utils.ban_check import check_ban
    ...
    if await check_ban(ctx, ctx.author):
        return
"""
import datetime
import discord
from discord.ext import commands


def _fmt_until(dt: datetime.datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M UTC")


async def check_ban(ctx: commands.Context, member: discord.Member) -> bool:
    """
    Проверяет, забанен ли игрок.

    Если забанен — отправляет сообщение в ctx и возвращает True.
    Если не забанен — возвращает False.

    Также снимает бан автоматически если время вышло.
    """
    ban_info = await ctx.bot.db.get_ban(member.id)
    if not ban_info:
        return False

    banned_until: datetime.datetime = ban_info["banned_until"]

    # Бан истёк — снимаем автоматически
    if datetime.datetime.utcnow() >= banned_until:
        await ctx.bot.db.remove_ban(member.id)
        # Снять BANNED роль, если есть
        try:
            banned_role = discord.utils.get(ctx.guild.roles, name="BANNED")
            if banned_role and banned_role in member.roles:
                await member.remove_roles(banned_role, reason="Ban expired")
        except discord.Forbidden:
            pass
        return False

    # Бан ещё активен
    embed = discord.Embed(
        title="🚫  Вы заблокированы",
        color=0xFF0000,
        description=(
            f"Вы не можете пользоваться функциями бота.\n"
            f"**Бан истекает:** {_fmt_until(banned_until)}"
        ),
    )
    await ctx.send(embed=embed)
    return True


async def is_banned(bot, discord_id: int) -> bool:
    """
    Быстрая проверка без отправки сообщения.
    Возвращает True если игрок сейчас забанен (и бан не истёк).
    """
    ban_info = await bot.db.get_ban(discord_id)
    if not ban_info:
        return False
    if datetime.datetime.utcnow() >= ban_info["banned_until"]:
        await bot.db.remove_ban(discord_id)
        return False
    return True
