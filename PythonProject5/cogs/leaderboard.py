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
        admin_channel = discord.utils.find(
            lambda c: Config.ADMIN_CHANNEL_NAME in c.name or c.name == Config.ADMIN_CHANNEL_NAME,
            ctx.guild.text_channels,
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

    @commands.command(name="ranks")
    async def ranks(self, ctx: commands.Context):
        if not self._is_guild(ctx):
            return

        from config import RANKS
        embed = discord.Embed(
            title="🏆  Система рангов",
            description="Список всех рангов и необходимое ELO:",
            color=0xFFD700,
        )
        rank_emojis = ["🥉", "🥉", "🥉", "🥈", "🥈", "🥇", "🥇", "💎", "💠", "👑"]
        for i, (min_e, max_e, name, color, _) in enumerate(RANKS):
            emoji = rank_emojis[i] if i < len(rank_emojis) else "🔹"
            max_str = str(max_e) if max_e < 99999 else "∞"
            embed.add_field(
                name=f"{emoji} {name}",
                value=f"`{min_e}` — `{max_str}` ELO",
                inline=True,
            )
        embed.set_footer(text="ELO начисляется за победы в матчах")
        await ctx.send(embed=embed)

    @commands.command(name="plus")
    async def mod_plus(self, ctx: commands.Context, member: discord.Member, amount: int):
        if not self._is_guild(ctx):
            return
        if not (ctx.author.guild_permissions.administrator or
                discord.utils.get(ctx.author.roles, name=self.bot.db.__class__.__module__) or
                any(r.name == "Модератор" for r in ctx.author.roles) or
                ctx.author.guild_permissions.administrator):
            from config import Config as _C
            if not any(r.name == _C.MODERATOR_ROLE_NAME for r in ctx.author.roles) and \
               not ctx.author.guild_permissions.administrator:
                await ctx.send("❌ Нет прав. Только модераторы могут изменять ELO.")
                return
        from config import Config as _C
        if not any(r.name == _C.MODERATOR_ROLE_NAME for r in ctx.author.roles) and \
           not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Нет прав.")
            return
        if amount <= 0:
            await ctx.send("❌ Укажи положительное число.")
            return
        new_elo = await self.bot.db.mod_adjust_elo(member.id, amount)
        if new_elo == -1:
            await ctx.send("❌ Игрок не зарегистрирован.")
            return
        from cogs.register import Register
        reg_cog: Register = self.bot.cogs.get("Register")
        if reg_cog and ctx.guild:
            m = ctx.guild.get_member(member.id)
            if m:
                await reg_cog._sync_rank_role(m, new_elo)
        await ctx.send(f"✅ **{member.display_name}** +{amount} ELO → **{new_elo}** ELO")

    @commands.command(name="minus")
    async def mod_minus(self, ctx: commands.Context, member: discord.Member, amount: int):
        if not self._is_guild(ctx):
            return
        from config import Config as _C
        if not any(r.name == _C.MODERATOR_ROLE_NAME for r in ctx.author.roles) and \
           not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ Нет прав. Только модераторы могут изменять ELO.")
            return
        if amount <= 0:
            await ctx.send("❌ Укажи положительное число.")
            return
        new_elo = await self.bot.db.mod_adjust_elo(member.id, -amount)
        if new_elo == -1:
            await ctx.send("❌ Игрок не зарегистрирован.")
            return
        from cogs.register import Register
        reg_cog: Register = self.bot.cogs.get("Register")
        if reg_cog and ctx.guild:
            m = ctx.guild.get_member(member.id)
            if m:
                await reg_cog._sync_rank_role(m, new_elo)
        await ctx.send(f"✅ **{member.display_name}** -{amount} ELO → **{new_elo}** ELO")


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



    @commands.command(name="streak")
    async def streak(self, ctx: commands.Context, member: discord.Member = None):
        """
        История результатов игр с форматом и изменением ELO.
        Включает ВСЕ игры — в том числе сыгранные до обновления бота.
        """
        if not self._is_guild(ctx):
            return

        target = member or ctx.author
        player = await self.bot.db.get_player(target.id)
        if not player:
            await ctx.send(f"{target.mention} не зарегистрирован.")
            return

        history = await self.bot.db.get_elo_history_simple(target.id)
        # Фильтруем: только записи от реальных игр (game_id не NULL; change != 0 или mode задан)
        # Записи от mod_adjust_elo (game_id=NULL) тоже оставляем, они нужны для ELO графика
        # но для streak берём только игровые (game_id IS NOT NULL)
        game_history = [r for r in history if r.get("game_id") is not None]
        if not game_history:
            await ctx.send(f"У **{player['username']}** пока нет сыгранных игр.")
            return

        # Последние 30 игр для детального списка
        recent = game_history[-30:]

        # Определяем результат по знаку change (для старых записей без поля result)
        def get_result(row) -> str:
            ch = row.get("change", 0)
            if ch > 0:
                return "win"
            elif ch < 0:
                return "lose"
            return "draw"

        # Строка-иконки: последние 50 игр
        icon_map = {"win": "🟢", "lose": "🔴", "draw": "🟡"}
        icons = [icon_map[get_result(r)] for r in game_history[-50:]]
        streak_line = "".join(icons)

        # Текущая серия
        current_result = get_result(game_history[-1])
        streak_count = 0
        for r in reversed(game_history):
            if get_result(r) == current_result:
                streak_count += 1
            else:
                break
        streak_labels = {
            "win":  f"🔥 {streak_count} побед подряд",
            "lose": f"❄️ {streak_count} поражений подряд",
            "draw": f"🤝 {streak_count} ничьих подряд",
        }
        streak_label = streak_labels[current_result]

        # Детальный список последних 30 игр
        mode_labels = {
            "team":   "👥 team",
            "random": "🎲 rand",
            "cap":    "🎯 cap",
            None:     "❓",
        }
        detail_lines = []
        for r in reversed(recent):
            res = get_result(r)
            icon = icon_map[res]
            ch = r.get("change", 0)
            sign = "+" if ch >= 0 else ""
            elo_str = f"{sign}{ch}" if ch != 0 else "±0"
            size = r.get("size")
            mode = r.get("mode")
            mode_label = mode_labels.get(mode, "❓")
            fmt = f"{size}v{size}" if size else "?v?"
            elo_after = r.get("elo_after", "?")
            detail_lines.append(
                f"{icon} `{fmt} {mode_label}` {elo_str} ELO → **{elo_after}**"
            )

        # Discord embed field value max 1024 chars — делаем страницами по 15
        total = len(game_history)
        wins = sum(1 for r in game_history if get_result(r) == "win")
        losses = sum(1 for r in game_history if get_result(r) == "lose")
        draws = total - wins - losses
        wr = round(wins / total * 100) if total else 0

        embed = discord.Embed(
            title=f"📊  История игр — {player['username']}",
            color=0x5865F2,
        )
        embed.add_field(
            name=f"Последние {min(50, total)} игр  (🟢 победа  🔴 поражение  🟡 ничья)",
            value=streak_line or "—",
            inline=False,
        )
        # Детали: режем по 15 записей чтобы не превысить лимит поля
        chunk = "\n".join(detail_lines[:15])
        embed.add_field(name="Последние игры (детально)", value=chunk or "—", inline=False)

        embed.add_field(name="Текущая серия", value=streak_label, inline=True)
        embed.add_field(name="Всего игр",     value=str(total),   inline=True)
        embed.add_field(name="В / П / Н",     value=f"{wins} / {losses} / {draws}", inline=True)
        embed.add_field(name="Винрейт",        value=f"{wr}%",    inline=True)
        await ctx.send(embed=embed)

    @commands.command(name="stat")
    async def stat(self, ctx: commands.Context, member: discord.Member = None):
        """Статистика против конкретных игроков: с кем больше побед/поражений"""
        if not self._is_guild(ctx):
            return

        target = member or ctx.author
        player = await self.bot.db.get_player(target.id)
        if not player:
            await ctx.send(f"{target.mention} не зарегистрирован.")
            return

        rows = await self.bot.db.get_stat_vs_players(target.id)
        if not rows:
            await ctx.send(f"У **{player['username']}** пока нет статистики против других игроков.")
            return

        embed = discord.Embed(
            title=f"⚔️  Статистика — {player['username']}",
            color=0xE67E22,
        )

        # Топ-5 по победам
        top_wins = [r for r in rows if r["wins"] > 0][:5]
        if top_wins:
            lines = []
            for r in top_wins:
                wr = round(r["wins"] / r["total"] * 100) if r["total"] else 0
                lines.append(f"• **{r['username']}** — {r['wins']}В / {r['losses']}П / {r['draws']}Н  (WR {wr}%)")
            embed.add_field(name="🏆 Больше всего побед против", value="\n".join(lines), inline=False)

        # Топ-5 по поражениям
        top_losses = sorted(rows, key=lambda r: r["losses"], reverse=True)
        top_losses = [r for r in top_losses if r["losses"] > 0][:5]
        if top_losses:
            lines = []
            for r in top_losses:
                wr = round(r["wins"] / r["total"] * 100) if r["total"] else 0
                lines.append(f"• **{r['username']}** — {r['wins']}В / {r['losses']}П / {r['draws']}Н  (WR {wr}%)")
            embed.add_field(name="💀 Больше всего поражений против", value="\n".join(lines), inline=False)

        # Лучший напарник (общие победы в одной команде — пока считаем через wins где были в одном game_id)
        # Топ по общему числу игр
        top_played = sorted(rows, key=lambda r: r["total"], reverse=True)[:3]
        if top_played:
            lines = []
            for r in top_played:
                lines.append(f"• **{r['username']}** — {r['total']} игр вместе")
            embed.add_field(name="🎮 Чаще всего встречался с", value="\n".join(lines), inline=False)

        embed.set_footer(text=f"Всего уникальных соперников: {len(rows)}")
        await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Leaderboard(bot))