import datetime
import discord
from discord.ext import commands

from config import Config
from utils.embeds import profile_embed
from utils.charts import build_elo_chart


TIME_MAP = {
    "day":   1,
    "week":  7,
    "month": 30,
    "all":   None,
}


class Profile(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _is_guild(self, ctx) -> bool:
        return ctx.guild and ctx.guild.id == Config.GUILD_ID

    @commands.command(name="profile")
    async def profile(self, ctx: commands.Context, member: discord.Member = None):
        if not self._is_guild(ctx):
            return

        target = member or ctx.author
        player = await self.bot.db.get_player(target.id)

        if not player:
            await ctx.send(
                f"{target.mention} is not registered."
                + (" Use `!register`." if target == ctx.author else "")
            )
            return

        ban_info = await self.bot.db.get_ban(target.id)
        embed = profile_embed(player, target, ban_info=ban_info)
        await ctx.send(embed=embed)

    @commands.command(name="elo")
    async def elo_chart(
        self,
        ctx: commands.Context,
        timeframe: str = "all",
        member: discord.Member = None,
    ):
        if not self._is_guild(ctx):
            return

        # гибкий порядок аргументов: !elo @user  или  !elo week @user
        if timeframe.startswith("<@"):
            # пользователь передал сначала пинг
            try:
                uid = int(timeframe.strip("<@!>"))
                member = ctx.guild.get_member(uid)
            except ValueError:
                pass
            timeframe = "all"

        if timeframe not in TIME_MAP:
            await ctx.send("Valid periods: `day`, `week`, `month`, `all`.")
            return

        target = member or ctx.author
        player = await self.bot.db.get_player(target.id)
        if not player:
            await ctx.send("Player is not registered.")
            return

        days = TIME_MAP[timeframe]
        since = (
            datetime.datetime.utcnow() - datetime.timedelta(days=days)
            if days else None
        )
        history = await self.bot.db.get_elo_history(target.id, since)

        if not history:
            await ctx.send("No data for the selected period.")
            return

        file = await build_elo_chart(history, target.display_name, timeframe)
        if file:
            await ctx.send(file=file)
        else:
            await ctx.send("Failed to build the chart.")


async def setup(bot):
    await bot.add_cog(Profile(bot))