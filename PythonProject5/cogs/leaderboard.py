import discord
from discord.ext import commands

from config import Config, get_rank

PAGE_SIZE = 10


def _build_leaderboard_embed(players: list, page: int, total_players: int) -> discord.Embed:
    """Строит embed для страницы лидерборда."""
    total_pages = max(1, (total_players + PAGE_SIZE - 1) // PAGE_SIZE)
    start = page * PAGE_SIZE
    page_players = players[start:start + PAGE_SIZE]

    medals = ["🥇", "🥈", "🥉"] + ["🔹"] * (PAGE_SIZE - 3)
    lines = []
    for i, p in enumerate(page_players):
        rank_name, _ = get_rank(p["elo"])
        total = p["wins"] + p["losses"] + p["draws"]
        wr = round(p["wins"] / total * 100) if total else 0
        pos = start + i
        medal = medals[pos] if pos < len(medals) else "🔹"
        lines.append(
            f"{medal} **{pos + 1}.** {p['username']} — "
            f"**{p['elo']}** ELO  |  {rank_name}  |  WR {wr}%"
        )

    embed = discord.Embed(
        title="🏆  Player Leaderboard",
        description="\n".join(lines),
        color=0xFFD700,
    )
    embed.set_footer(
        text=f"Page {page + 1}/{total_pages}  ·  Total players: {total_players}"
    )
    return embed


class LeaderboardView(discord.ui.View):
    """View с кнопками листания лидерборда."""

    def __init__(self, players: list, page: int = 0):
        super().__init__(timeout=120)
        self.players = players
        self.page = page
        self.total_pages = max(1, (len(players) + PAGE_SIZE - 1) // PAGE_SIZE)
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total_pages - 1
        self.page_label.label = f"{self.page + 1} / {self.total_pages}"

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="lb_prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = max(0, self.page - 1)
        self._update_buttons()
        embed = _build_leaderboard_embed(self.players, self.page, len(self.players))
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="1 / 1", style=discord.ButtonStyle.secondary,
                       disabled=True, custom_id="lb_page")
    async def page_label(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="lb_next")
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page = min(self.total_pages - 1, self.page + 1)
        self._update_buttons()
        embed = _build_leaderboard_embed(self.players, self.page, len(self.players))
        await interaction.response.edit_message(embed=embed, view=self)

    async def on_timeout(self):
        # Убираем кнопки когда истекает timeout
        for item in self.children:
            item.disabled = True
        try:
            await self.message.edit(view=self)
        except Exception:
            pass


class Leaderboard(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def _is_guild(self, ctx) -> bool:
        return ctx.guild and ctx.guild.id == Config.GUILD_ID

    @commands.command(name="top")
    async def top(self, ctx: commands.Context):
        if not self._is_guild(ctx):
            return

        players = await self.bot.db.get_all_players_ranked()
        if not players:
            await ctx.send("No registered players yet.")
            return

        embed = _build_leaderboard_embed(players, page=0, total_players=len(players))
        view = LeaderboardView(players, page=0)
        # Если один лист — не показываем кнопки
        if view.total_pages <= 1:
            msg = await ctx.send(embed=embed)
        else:
            msg = await ctx.send(embed=embed, view=view)
            view.message = msg

    @commands.command(name="report")
    async def report(self, ctx: commands.Context, member: discord.Member, *, reason: str):
        if not self._is_guild(ctx):
            return

        if member.id == ctx.author.id:
            await ctx.send("You cannot report yourself.")
            return

        db = self.bot.db

        if await db.reports_today(ctx.author.id) >= 5:
            await ctx.send("You have reached the daily report limit (5).")
            return

        if await db.already_reported(ctx.author.id, member.id):
            await ctx.send("You have already reported this player.")
            return

        await db.add_report(ctx.author.id, member.id, reason)

        admin_channel = discord.utils.find(
            lambda c: Config.ADMIN_CHANNEL_NAME in c.name or c.name == Config.ADMIN_CHANNEL_NAME,
            ctx.guild.text_channels,
        )
        if admin_channel:
            embed = discord.Embed(title="🚨 Report", color=0xED4245)
            embed.add_field(name="From", value=f"{ctx.author.mention} (`{ctx.author}`)", inline=True)
            embed.add_field(name="Against", value=f"{member.mention} (`{member}`)", inline=True)
            embed.add_field(name="Reason", value=reason, inline=False)
            await admin_channel.send(embed=embed)

        await ctx.send("✅ Report sent to the administration. Thank you!", delete_after=10)
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
            title="🏆  Rank System",
            description="All ranks and required ELO:",
            color=0xFFD700,
        )
        rank_emojis = ["🥉", "🥉", "🥉", "🥈", "🥈", "🥇", "🥇", "💎", "💠", "👑"]
        for i, (min_e, max_e, name, color, _) in enumerate(RANKS):
            emoji = rank_emojis[i] if i < len(rank_emojis) else "🔹"
            max_str = str(max_e) if max_e < 99999 else "∞"
            embed.add_field(name=f"{emoji} {name}", value=f"`{min_e}` — `{max_str}` ELO", inline=True)
        embed.set_footer(text="ELO is earned by winning matches")
        await ctx.send(embed=embed)

    @commands.command(name="plus")
    async def mod_plus(self, ctx: commands.Context, member: discord.Member, amount: int):
        if not self._is_guild(ctx):
            return
        from config import Config as _C
        if not any(r.name == _C.MODERATOR_ROLE_NAME for r in ctx.author.roles) and \
           not ctx.author.guild_permissions.administrator:
            await ctx.send("❌ No permission. Moderators only.")
            return
        if amount <= 0:
            await ctx.send("❌ Please specify a positive number.")
            return
        new_elo = await self.bot.db.mod_adjust_elo(member.id, amount)
        if new_elo == -1:
            await ctx.send("❌ Player is not registered.")
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
            await ctx.send("❌ No permission. Moderators only.")
            return
        if amount <= 0:
            await ctx.send("❌ Please specify a positive number.")
            return
        new_elo = await self.bot.db.mod_adjust_elo(member.id, -amount)
        if new_elo == -1:
            await ctx.send("❌ Player is not registered.")
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

        embed = discord.Embed(title="👋 Команды и правила / Commands & Rules", color=0x5865F2)
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
                "`!start` — начать игру\n\n"
                "**Результат игры:**\n"
                "`!win` — победа  |  `!lose` — поражение  |  `!draw` — ничья\n\n"
                "**Профиль и статистика:**\n"
                "`!profile [@игрок]` — профиль  |  `!elo [day/week/month/all]` — график ELO\n"
                "`!top` — лидерборд (листается кнопками ◀ ▶)\n\n"
                "**Прочее:**\n"
                "`!report @игрок причина` — жалоба  |  `!help` — список команд\n"
            ),
            inline=False,
        )
        embed.add_field(
            name="🇬🇧 English — Bot Commands",
            value=(
                "**Registration:**\n"
                "`!register <nick>` — register (required!)\n"
                "`!rename <new_nick>` — change your in-game nickname\n\n"
                "**Rooms:**\n"
                "`!create [1/2/3/4] [team/random/cap]` — create a room\n"
                "  • `team` — players pick their own team\n"
                "  • `random` — bot assigns teams randomly\n"
                "  • `cap` — captains pick players one by one\n"
                "`!queue [size] [mode]` or `!q` — join a queue\n"
                "`!exit` — leave the room\n"
                "`!kick @player` — kick a player (captain only)\n"
                "`!start` — start the game\n\n"
                "**Game result:**\n"
                "`!win` — win  |  `!lose` — loss  |  `!draw` — draw\n\n"
                "**Profile & Stats:**\n"
                "`!profile [@player]` — profile  |  `!elo [day/week/month/all]` — ELO chart\n"
                "`!top` — leaderboard (navigate with ◀ ▶ buttons)\n\n"
                "**Other:**\n"
                "`!report @player reason` — report  |  `!help` — command list\n"
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
        if not self._is_guild(ctx):
            return
        target = member or ctx.author
        player = await self.bot.db.get_player(target.id)
        if not player:
            await ctx.send(f"{target.mention} is not registered.")
            return
        history = await self.bot.db.get_elo_history_simple(target.id)
        game_history = [r for r in history if r.get("game_id") is not None]
        if not game_history:
            await ctx.send(f"**{player['username']}** has no games played yet.")
            return

        def get_result(row) -> str:
            """
            Determine win/draw/loss.
            - If elo_before > 0 and change == 0  → draw (task 7: not a loss)
            - If elo_before == 0 and change == 0  → loss
            - change > 0 → win, change < 0 → loss
            """
            ch = row.get("change", 0)
            elo_before = row.get("elo_before", 0)
            if ch > 0:
                return "win"
            if ch < 0:
                return "loss"
            # change == 0
            if elo_before > 0:
                return "draw"
            return "loss"

        # Filter out draws from display (task 7: remove -0 draws from history if elo_before > 0)
        # We keep them in game_history for total count but skip them in the streak icons
        displayable = [r for r in game_history if get_result(r) != "draw"]

        icon_map = {"win": "🟢", "loss": "🔴", "draw": "🟡"}
        icons = [icon_map[get_result(r)] for r in game_history[-50:]]
        streak_line = "".join(icons)

        current_result = get_result(game_history[-1])
        streak_count = 0
        for r in reversed(game_history):
            if get_result(r) == current_result:
                streak_count += 1
            else:
                break

        streak_labels = {
            "win":  f"🔥 {streak_count} win streak",
            "loss": f"❄️ {streak_count} loss streak",
            "draw": f"🤝 {streak_count} draw streak",
        }
        streak_label = streak_labels[current_result]

        mode_labels = {"team": "👥 team", "random": "🎲 rand", "cap": "🎯 cap", None: "❓"}
        recent = game_history[-30:]
        detail_lines = []
        for r in reversed(recent):
            res = get_result(r)
            icon = icon_map[res]
            ch = r.get("change", 0)
            sign = "+" if ch > 0 else ""
            elo_str = f"{sign}{ch}" if ch != 0 else "±0"
            size = r.get("size")
            mode = r.get("mode")
            mode_label = mode_labels.get(mode, "❓")
            fmt = f"{size}v{size}" if size else "?v?"
            elo_after = r.get("elo_after", "?")
            detail_lines.append(f"{icon} `{fmt} {mode_label}` {elo_str} ELO → **{elo_after}**")

        total = len(game_history)
        wins   = sum(1 for r in game_history if get_result(r) == "win")
        losses = sum(1 for r in game_history if get_result(r) == "loss")
        draws  = sum(1 for r in game_history if get_result(r) == "draw")
        wr = round(wins / total * 100) if total else 0

        embed = discord.Embed(title=f"📊  Game History — {player['username']}", color=0x5865F2)
        embed.add_field(
            name=f"Last {min(50, total)} games  (🟢 win  🔴 loss  🟡 draw)",
            value=streak_line or "—",
            inline=False,
        )
        chunk = "\n".join(detail_lines[:15])
        embed.add_field(name="Recent games (detailed)", value=chunk or "—", inline=False)
        embed.add_field(name="Current streak", value=streak_label, inline=True)
        embed.add_field(name="Total games", value=str(total), inline=True)
        embed.add_field(name="W / L / D", value=f"{wins} / {losses} / {draws}", inline=True)
        embed.add_field(name="Winrate", value=f"{wr}%", inline=True)
        await ctx.send(embed=embed)

    @commands.command(name="eloinfo")
    async def eloinfo(self, ctx: commands.Context, lang: str = None):
        """
        Объясняет систему ELO. Язык: !eloinfo ru  или  !eloinfo en
        Без аргумента — отправляет оба языка.
        """
        if not self._is_guild(ctx):
            return

        # Нормализуем аргумент
        lang = (lang or "").lower().strip()
        if lang not in ("ru", "en", ""):
            await ctx.send("Укажи язык: `!eloinfo ru` или `!eloinfo en`")
            return

        embeds_to_send = []

        # ── Русский embed ──────────────────────────────────────────
        if lang in ("ru", ""):
            embed_ru = discord.Embed(
                title="📊  Система ELO — Как это работает",
                color=0xFFD700,
            )
            embed_ru.add_field(
                name="🏆 Базовые очки по зонам рейтинга",
                value=(
                    "```\n"
                    "Диапазон ELO │  Победа │ Поражение\n"
                    "─────────────┼─────────┼──────────\n"
                    "    0 – 300  │   +7    │    -3\n"
                    "  301 – 600  │   +5    │    -4\n"
                    "  601 – 800  │   +3    │    -5\n"
                    " 801 – 1000  │   +2    │    -6\n"
                    "   1001+     │   +2    │    -6\n"
                    "```"
                ),
                inline=False,
            )
            embed_ru.add_field(
                name="⚙️ Множитель формата",
                value=(
                    "```\n"
                    "Формат  │ Множитель\n"
                    "────────┼──────────\n"
                    "  1v1   │  × 0.7\n"
                    "  2v2   │  × 1.0\n"
                    "  3v3   │  × 1.05\n"
                    "  4v4   │  × 1.2\n"
                    "```"
                ),
                inline=False,
            )
            embed_ru.add_field(
                name="👥 Модификатор режима — Командный (team)",
                value=(
                    "В режиме **team** итоговое изменение ELO умножается на **× 0.7** "
                    "(снижено, т.к. игроки сами собирают команды).\n"
                    "Минимум: **+1** за победу, **-1** за поражение (не более **-7**)."
                ),
                inline=False,
            )
            embed_ru.add_field(
                name="🎲 Режимы Random / 🎯 Cap",
                value=(
                    "Штрафного множителя нет. Формула: `база × множитель_формата`.\n"
                    "Минимум: **+1** за победу, **-1** за поражение (не более **-7**)."
                ),
                inline=False,
            )
            embed_ru.add_field(
                name="⚠️ Штрафные игры",
                value=(
                    "Если у тебя активны **штрафные игры**:\n"
                    "• ELO за победу делится пополам (`÷ 2`).\n"
                    "• ELO за поражение удваивается (`× 2`).\n"
                    "Штраф снимается после отыгрывания нужного количества игр."
                ),
                inline=False,
            )
            embed_ru.add_field(
                name="🛡️ Минимум ELO",
                value="ELO не может упасть ниже **0**. Если ты на 0 и проиграл — остаёшься на 0.",
                inline=False,
            )
            embed_ru.add_field(
                name="📖 Пример — 4v4 Cap, ELO 350",
                value=(
                    "База (победа, зона 301–600): **+5**\n"
                    "Формат (4v4): × 1.2 → **+6**\n"
                    "Режим (cap): штрафа нет → **+6 ELO**\n\n"
                    "База (поражение, зона 301–600): **-4**\n"
                    "Формат (4v4): × 1.2 → **-4.8 → -5**\n"
                    "Режим (cap): штрафа нет → **-5 ELO**"
                ),
                inline=False,
            )
            embed_ru.set_footer(text="Используй !elo [day/week/month/all] чтобы посмотреть график ELO.")
            embeds_to_send.append(embed_ru)

        # ── English embed ──────────────────────────────────────────
        if lang in ("en", ""):
            embed_en = discord.Embed(
                title="📊  ELO System — How it works",
                color=0x5865F2,
            )
            embed_en.add_field(
                name="🏆 Base ELO by rating zone",
                value=(
                    "```\n"
                    "ELO range   │  Win   │  Loss\n"
                    "────────────┼────────┼──────\n"
                    "   0 – 300  │  +7    │  -3\n"
                    " 301 – 600  │  +5    │  -4\n"
                    " 601 – 800  │  +3    │  -5\n"
                    " 801 – 1000 │  +2    │  -6\n"
                    "  1001+     │  +2    │  -6\n"
                    "```"
                ),
                inline=False,
            )
            embed_en.add_field(
                name="⚙️ Format multiplier",
                value=(
                    "```\n"
                    "Format  │ Multiplier\n"
                    "────────┼───────────\n"
                    "  1v1   │  × 0.7\n"
                    "  2v2   │  × 1.0\n"
                    "  3v3   │  × 1.05\n"
                    "  4v4   │  × 1.2\n"
                    "```"
                ),
                inline=False,
            )
            embed_en.add_field(
                name="👥 Mode modifier — Team mode",
                value=(
                    "In **team** mode the final ELO change is multiplied by **× 0.7** "
                    "(reduced because players self-organize their teams).\n"
                    "Minimum: **+1** per win, **-1** per loss (capped at -7)."
                ),
                inline=False,
            )
            embed_en.add_field(
                name="🎲 Random / 🎯 Cap modes",
                value=(
                    "No mode penalty. Formula: `base × format_mult`.\n"
                    "Minimum: **+1** per win, **-1** per loss (capped at **-7**)."
                ),
                inline=False,
            )
            embed_en.add_field(
                name="⚠️ Penalty games",
                value=(
                    "If you have **penalty games** active:\n"
                    "• Win ELO is halved (`÷ 2`).\n"
                    "• Loss ELO is doubled (`× 2`).\n"
                    "Penalty is removed after completing the penalised games."
                ),
                inline=False,
            )
            embed_en.add_field(
                name="🛡️ ELO floor",
                value="Your ELO can never drop below **0**. If you're at 0 and lose, you stay at 0.",
                inline=False,
            )
            embed_en.add_field(
                name="📖 Example — 4v4 Cap, ELO 350",
                value=(
                    "Base (win, zone 301-600): **+5**\n"
                    "Format (4v4): × 1.2 → **+6**\n"
                    "Mode (cap): no penalty → **+6 ELO**\n\n"
                    "Base (loss, zone 301-600): **-4**\n"
                    "Format (4v4): × 1.2 → **-4.8 → -5**\n"
                    "Mode (cap): no penalty → **-5 ELO**"
                ),
                inline=False,
            )
            embed_en.set_footer(text="Use !elo [day/week/month/all] to see your ELO chart.")
            embeds_to_send.append(embed_en)

        await ctx.send(embeds=embeds_to_send)

    @commands.command(name="stat")
    async def stat(self, ctx: commands.Context, member: discord.Member = None):
        if not self._is_guild(ctx):
            return
        target = member or ctx.author
        player = await self.bot.db.get_player(target.id)
        if not player:
            await ctx.send(f"{target.mention} is not registered.")
            return
        rows = await self.bot.db.get_stat_vs_players(target.id)
        if not rows:
            await ctx.send(f"**{player['username']}** has no match-up stats yet.")
            return

        embed = discord.Embed(title=f"⚔️  Stats — {player['username']}", color=0xE67E22)
        top_wins = [r for r in rows if r["wins"] > 0][:5]
        if top_wins:
            lines = []
            for r in top_wins:
                wr = round(r["wins"] / r["total"] * 100) if r["total"] else 0
                lines.append(f"• **{r['username']}** — {r['wins']}W / {r['losses']}L / {r['draws']}D  (WR {wr}%)")
            embed.add_field(name="🏆 Most wins against", value="\n".join(lines), inline=False)
        top_losses = sorted(rows, key=lambda r: r["losses"], reverse=True)
        top_losses = [r for r in top_losses if r["losses"] > 0][:5]
        if top_losses:
            lines = []
            for r in top_losses:
                wr = round(r["wins"] / r["total"] * 100) if r["total"] else 0
                lines.append(f"• **{r['username']}** — {r['wins']}W / {r['losses']}L / {r['draws']}D  (WR {wr}%)")
            embed.add_field(name="💀 Most losses against", value="\n".join(lines), inline=False)
        top_played = sorted(rows, key=lambda r: r["total"], reverse=True)[:3]
        if top_played:
            lines = [f"• **{r['username']}** — {r['total']} games together" for r in top_played]
            embed.add_field(name="🎮 Most frequent opponents", value="\n".join(lines), inline=False)
        embed.set_footer(text=f"Total unique opponents: {len(rows)}")
        await ctx.send(embed=embed)

    @commands.command(name="commands")
    async def commands_info(self, ctx: commands.Context):
        """Объясняет доступные команды на русском и английском."""
        if not self._is_guild(ctx):
            return

        # ── Русский embed ──────────────────────────────────────────
        embed_ru = discord.Embed(
            title="📖  Команды для игроков",
            color=0x57F287,
        )
        embed_ru.add_field(
            name="👤  Профиль и статистика",
            value=(
                "**`!profile [@игрок]`**\n"
                "Показывает карточку игрока: ранг, ELO, количество игр, побед, поражений, ничьих, винрейт и серию побед.\n"
                "Без аргумента — твой профиль. С упоминанием — профиль другого игрока.\n\n"

                "**`!elo [day/week/month/all] [@игрок]`**\n"
                "График изменения ELO за выбранный период. По умолчанию — всё время.\n"
                "Пример: `!elo week` или `!elo month @игрок`\n\n"

                "**`!streak [@игрок]`**\n"
                "История последних игр в виде цветных кружков (🟢 победа · 🔴 поражение · 🟡 ничья), "
                "текущая серия и подробная таблица с изменениями ELO по каждой игре.\n\n"

                "**`!stat [@игрок]`**\n"
                "Статистика личных встреч: против кого больше всего побед, поражений и кто самый частый соперник."
            ),
            inline=False,
        )
        embed_ru.add_field(
            name="🎮  Игры и комнаты",
            value=(
                "**`!create [1/2/3/4] [team/random/cap]`**\n"
                "Создаёт игровую комнату.\n"
                "• `team` — каждый сам выбирает команду через `!pick 1` или `!pick 2`\n"
                "• `random` — бот раскидывает игроков по командам случайно при заполнении\n"
                "• `cap` — два капитана поочерёдно выбирают игроков\n"
                "Без аргументов — появится панель с кнопками для выбора."
            ),
            inline=False,
        )
        embed_ru.add_field(
            name="📊  Рейтинги и система",
            value=(
                "**`!top`**\n"
                "Таблица лидеров по ELO с листанием по страницам. Показывает ранг, ELO и винрейт каждого игрока.\n\n"

                "**`!ranks`**\n"
                "Таблица всех рангов и необходимый ELO для каждого: от Bronze I до Master.\n\n"

                "**`!eloinfo [ru/en]`**\n"
                "Подробное объяснение системы ELO: базовые очки по зонам рейтинга, множители формата (1v1–4v4), "
                "модификатор командного режима, штрафные игры и минимальный ELO."
            ),
            inline=False,
        )
        embed_ru.add_field(
            name="🚩  Жалобы",
            value=(
                "**`!report @игрок <причина>`**\n"
                "Отправляет жалобу на игрока администрации. Лимит — **5 жалоб в сутки**. "
                "Нельзя пожаловаться на самого себя или дважды на одного и того же игрока."
            ),
            inline=False,
        )
        embed_ru.set_footer(text="!commands — список команд для игроков")

        # ── English embed ──────────────────────────────────────────
        embed_en = discord.Embed(
            title="📖  Player Commands",
            color=0x5865F2,
        )
        embed_en.add_field(
            name="👤  Profile & Stats",
            value=(
                "**`!profile [@player]`**\n"
                "Shows a player card: rank, ELO, games played, wins, losses, draws, winrate and win streak.\n"
                "No argument — your own profile. With a mention — another player's profile.\n\n"

                "**`!elo [day/week/month/all] [@player]`**\n"
                "ELO change chart for the selected period. Defaults to all time.\n"
                "Example: `!elo week` or `!elo month @player`\n\n"

                "**`!streak [@player]`**\n"
                "Recent game history as coloured dots (🟢 win · 🔴 loss · 🟡 draw), "
                "current streak and a detailed table with ELO changes per game.\n\n"

                "**`!stat [@player]`**\n"
                "Head-to-head stats: most wins against, most losses against and most frequent opponents."
            ),
            inline=False,
        )
        embed_en.add_field(
            name="🎮  Games & Rooms",
            value=(
                "**`!create [1/2/3/4] [team/random/cap]`**\n"
                "Creates a game room.\n"
                "• `team` — each player picks their team with `!pick 1` or `!pick 2`\n"
                "• `random` — the bot randomly distributes players when the room fills up\n"
                "• `cap` — two captains take turns picking players\n"
                "No arguments — an interactive button panel will appear."
            ),
            inline=False,
        )
        embed_en.add_field(
            name="📊  Rankings & System",
            value=(
                "**`!top`**\n"
                "Full player leaderboard with page navigation. Shows rank, ELO and winrate for each player.\n\n"

                "**`!ranks`**\n"
                "Full rank table with required ELO for each tier: Bronze I through Master.\n\n"

                "**`!eloinfo [ru/en]`**\n"
                "Detailed explanation of the ELO system: base points by rating zone, format multipliers (1v1–4v4), "
                "team mode modifier, penalty games and the ELO floor."
            ),
            inline=False,
        )
        embed_en.add_field(
            name="🚩  Reports",
            value=(
                "**`!report @player <reason>`**\n"
                "Sends a report about a player to the administration. Limit — **5 reports per day**. "
                "You cannot report yourself or report the same player twice."
            ),
            inline=False,
        )
        embed_en.set_footer(text="!commands — player command reference")

        await ctx.send(embeds=[embed_ru, embed_en])


async def setup(bot):
    await bot.add_cog(Leaderboard(bot))
