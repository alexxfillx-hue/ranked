import discord
from discord.ext import commands

from config import Config, get_rank


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _is_guild(self, ctx) -> bool:
        return bool(ctx.guild and ctx.guild.id == Config.GUILD_ID)

    def _is_mod(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        return bool(discord.utils.get(member.roles, name=Config.MODERATOR_ROLE_NAME))

    async def _adjust_elo(self, ctx: commands.Context, member: discord.Member, delta: int):
        """Общая логика изменения ELO модератором."""
        db = self.bot.db
        player = await db.get_player(member.id)
        if not player:
            await ctx.send(f"❌ {member.mention} не зарегистрирован.")
            return

        old_elo = player["elo"]
        new_elo = max(0, old_elo + delta)

        # Обновляем ELO и пишем в историю (game_id=None — ручное изменение)
        await db.pool.execute(
            "UPDATE players SET elo=$1 WHERE discord_id=$2",
            new_elo, member.id,
        )
        await db.pool.execute(
            """INSERT INTO elo_history (discord_id, elo_before, elo_after, change, game_id, mode)
               VALUES ($1, $2, $3, $4, NULL, 'admin')""",
            member.id, old_elo, new_elo, delta,
        )

        # Синхронизируем ранговую роль
        old_rank, _ = get_rank(old_elo)
        new_rank, _ = get_rank(new_elo)

        reg_cog = self.bot.cogs.get("Register")
        if reg_cog:
            await reg_cog._sync_rank_role(member, new_elo)

        # Публичное объявление нового ранга в чат-канал (если ранг изменился)
        if old_rank != new_rank:
            rooms_cog = self.bot.cogs.get("Rooms")
            if rooms_cog:
                await rooms_cog._announce_rank_change(
                    ctx.guild, member, new_rank, new_elo, old_rank=old_rank
                )

        # Ответ в канал где вызвана команда
        sign = f"+{delta}" if delta >= 0 else str(delta)
        color = 0x57F287 if delta >= 0 else 0xED4245
        arrow = "📈" if delta >= 0 else "📉"

        embed = discord.Embed(
            title=f"{arrow} ELO скорректировано модератором",
            color=color,
        )
        embed.add_field(name="Игрок", value=member.mention, inline=True)
        embed.add_field(name="Изменение", value=f"`{sign}`", inline=True)
        embed.add_field(name="ELO", value=f"{old_elo} → **{new_elo}**", inline=True)
        if old_rank != new_rank:
            embed.add_field(name="Ранг", value=f"{old_rank} → **{new_rank}**", inline=False)
        embed.set_footer(text=f"Модератор: {ctx.author.display_name}")
        embed.timestamp = discord.utils.utcnow()
        await ctx.send(embed=embed)

    @commands.command(name="plus")
    async def plus_cmd(self, ctx: commands.Context, member: discord.Member = None, amount: int = None):
        """[Мод] Прибавить ELO игроку. Использование: !plus @игрок <кол-во>"""
        if not self._is_guild(ctx):
            return
        if not self._is_mod(ctx.author):
            await ctx.send("❌ Нет прав. Только для модераторов.")
            return
        if member is None or amount is None:
            await ctx.send("Использование: `!plus @игрок <кол-во>`  пример: `!plus @alekz 50`")
            return
        if amount <= 0:
            await ctx.send("❌ Количество должно быть положительным числом.")
            return
        await self._adjust_elo(ctx, member, +amount)

    @commands.command(name="minus")
    async def minus_cmd(self, ctx: commands.Context, member: discord.Member = None, amount: int = None):
        """[Мод] Отнять ELO у игрока. Использование: !minus @игрок <кол-во>"""
        if not self._is_guild(ctx):
            return
        if not self._is_mod(ctx.author):
            await ctx.send("❌ Нет прав. Только для модераторов.")
            return
        if member is None or amount is None:
            await ctx.send("Использование: `!minus @игрок <кол-во>`  пример: `!minus @alekz 50`")
            return
        if amount <= 0:
            await ctx.send("❌ Количество должно быть положительным числом.")
            return
        await self._adjust_elo(ctx, member, -amount)


async def setup(bot):
    await bot.add_cog(Admin(bot))
