import discord
from discord.ext import commands

from config import Config, get_rank


class Leaderboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _is_guild(self, ctx) -> bool:
        return ctx.guild and ctx.guild.id == Config.GUILD_ID

    @commands.command(name="top")
    async def top(self, ctx: commands.Context):
        if not self._is_guild(ctx):
            return

        players = await self.bot.db.get_top(limit=10)
        if not players:
            await ctx.send("Пока нет зарегистрированных игроков.")
            return

        embed = discord.Embed(
            title="🏆  Топ-10 игроков по ELO",
            color=0xFFD700,
        )

        medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
        lines = []
        for i, p in enumerate(players):
            rank_name, _ = get_rank(p["elo"])
            total = p["wins"] + p["losses"] + p["draws"]
            wr = round(p["wins"] / total * 100) if total else 0
            lines.append(
                f"{medals[i]} **{i+1}.** {p['username']} — "
                f"**{p['elo']}** ELO  |  {rank_name}  |  WR {wr}%"
            )

        embed.description = "\n".join(lines)
        embed.set_footer(text=f"Всего игроков: {len(players)}")
        await ctx.send(embed=embed)

    @commands.command(name="report")
    async def report(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        if not self._is_guild(ctx):
            return

        if member.id == ctx.author.id:
            await ctx.send("Нельзя жаловаться на себя.")
            return

        db = self.bot.db

        if await db.reports_today(ctx.author.id) >= 5:
            await ctx.send("Ты исчерпал лимит жалоб на сегодня (5 штук).")
            return

        if await db.already_reported(ctx.author.id, member.id):
            await ctx.send("Ты уже жаловался на этого игрока.")
            return

        await db.add_report(ctx.author.id, member.id, reason)

        # отправляем в канал администрации
        admin_channel = discord.utils.get(
            ctx.guild.text_channels, name=Config.ADMIN_CHANNEL_NAME
        )
        if admin_channel:
            embed = discord.Embed(
                title="🚨 Жалоба",
                color=0xED4245,
            )
            embed.add_field(
                name="От кого", value=f"{ctx.author.mention} (`{ctx.author}`)", inline=True
            )
            embed.add_field(
                name="На кого", value=f"{member.mention} (`{member}`)", inline=True
            )
            embed.add_field(name="Причина", value=reason, inline=False)
            await admin_channel.send(embed=embed)

        await ctx.send(
            "✅ Жалоба отправлена администрации. Спасибо!",
            delete_after=10,
        )
        try:
            await ctx.message.delete()
        except discord.Forbidden:
            pass

    @commands.command(name="rules")
    async def rules(self, ctx: commands.Context):
        if not self._is_guild(ctx):
            return

        embed = discord.Embed(
            title="👋 Команды и правила / Commands & Rules",
            color=0x5865F2,
        )

        embed.add_field(
            name="🇷🇺 Русский — Команды бота",
            value=(
                "**Регистрация:**\n"
                "`!register <ник>` — зарегистрироваться (обязательно!)\n"
                "`!rename <новый_ник>` — сменить игровой ник\n\n"
                "**Комнаты:**\n"
                "`!create [1/2/3/4] [team/random/cap]` — создать комнату\n"
                "  • `team` — игроки сами выбирают команду\n"
                "  • `random` — бот распределяет рандомно\n"
                "  • `cap` — капитаны пикают игроков\n"
                "`!queue [размер] [режим]` или `!q` — войти в очередь\n"
                "`!exit` — покинуть комнату\n"
                "`!kick @игрок` — кикнуть игрока (только капитан)\n"
                "`!start` — начать игру (оба капитана должны подтвердить)\n\n"
                "**Результат игры:**\n"
                "`!win` — заявить победу\n"
                "`!lose` — заявить поражение\n"
                "`!draw` — заявить ничью\n\n"
                "**Профиль и статистика:**\n"
                "`!profile [@игрок]` — посмотреть профиль\n"
                "`!elo [day/week/month/all] [@игрок]` — график ELO\n"
                "`!top` — топ-10 игроков\n\n"
                "**Прочее:**\n"
                "`!report @игрок причина` — пожаловаться на игрока\n"
                "`!help` — список всех команд\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="🇬🇧 English — Bot Commands",
            value=(
                "**Registration:**\n"
                "`!register <nickname>` — register yourself (required!)\n"
                "`!rename <new_nickname>` — change your in-game nickname\n\n"
                "**Rooms:**\n"
                "`!create [1/2/3/4] [team/random/cap]` — create a room\n"
                "  • `team` — players pick their own team\n"
                "  • `random` — bot assigns teams randomly\n"
                "  • `cap` — captains pick players one by one\n"
                "`!queue [size] [mode]` or `!q` — join a queue\n"
                "`!exit` — leave the room\n"
                "`!kick @player` — kick a player (captain only)\n"
                "`!start` — start the game (both captains must confirm)\n\n"
                "**Game result:**\n"
                "`!win` — report a win\n"
                "`!lose` — report a loss\n"
                "`!draw` — report a draw\n\n"
                "**Profile & Stats:**\n"
                "`!profile [@player]` — view player profile\n"
                "`!elo [day/week/month/all] [@player]` — ELO chart\n"
                "`!top` — top-10 leaderboard\n\n"
                "**Other:**\n"
                "`!report @player reason` — report a player\n"
                "`!help` — full command list\n"
            ),
            inline=False,
        )

        embed.add_field(
            name="⚠️ Правила / Rules",
            value=(
                "🇷🇺 Играя на этом сервере, вы **обязаны показать свою игровую консоль** "
                "по запросу администрации или модераторов в любое время.\n\n"
                "🇬🇧 By playing on this server, you are **required to show your game console** "
                "upon request by the administration or moderators at any time."
            ),
            inline=False,
        )

        embed.set_footer(text="Удачи! / Good luck! 🎮")
        await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(Leaderboard(bot))