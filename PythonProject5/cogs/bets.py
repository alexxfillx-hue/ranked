# cogs/bets.py
"""
Match betting system.

Lifecycle:
  1. rooms.py calls BetsCog.on_game_start(room_id, team1, team2, size, mode)
     → bot posts an embed in the 🎰-bets channel with "Bet Team 1" / "Bet Team 2" buttons
     → a 5-minute countdown timer starts

  2. Player clicks a button → ephemeral confirmation (Confirm Bet / Cancel)

  3. After 5 minutes the buttons are disabled and the message is updated

  4. rooms.py calls BetsCog.on_game_end(room_id, winner_team, elo_changes)
     → if the game finished before the 5-minute window — all bets are cancelled
     → otherwise — ELO is awarded/deducted and the embed is updated
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Optional

import discord
from discord.ext import commands

from config import Config, get_rank

log = logging.getLogger("bot.bets")

# How long bets are open after the game starts (minutes)
BET_WINDOW_MINUTES = 3

# ELO changes for correct / incorrect bet
BET_WIN_ELO  = +3
BET_LOSE_ELO = -5

# Minimum ELO required to place a bet
BET_MIN_ELO = 5

# Bets channel is found by checking if this string is contained in the channel name
BETS_CHANNEL_NAME = "bets"


# ─────────────────────────────────────────────────────────────────────────────
#  Confirmation view (ephemeral) — shown when a player clicks a Bet button
# ─────────────────────────────────────────────────────────────────────────────

class BetConfirmView(discord.ui.View):
    """Ephemeral view asking the user to confirm or cancel their bet."""

    def __init__(self, cog: "Bets", room_id: int, team: int, user_id: int):
        super().__init__(timeout=60)
        self.cog     = cog
        self.room_id = room_id
        self.team    = team
        self.user_id = user_id

    @discord.ui.button(label="✅ Confirm Bet", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This confirmation is not for you.", ephemeral=True)
            return
        self.stop()
        result = await self.cog._place_bet(interaction.user, self.room_id, self.team)
        await interaction.response.edit_message(content=result, view=None)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This confirmation is not for you.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="❌ Bet cancelled.", view=None)


# ─────────────────────────────────────────────────────────────────────────────
#  Main bet view — public buttons in the bets channel message
# ─────────────────────────────────────────────────────────────────────────────

class BetView(discord.ui.View):
    """Two buttons: 🔵 Bet Team 1 / 🔴 Bet Team 2. timeout=None for persistence."""

    def __init__(self, cog: "Bets", room_id: int, active: bool = True):
        super().__init__(timeout=None)
        self.cog     = cog
        self.room_id = room_id

        btn1 = discord.ui.Button(
            label="🔵 Bet Team 1",
            style=discord.ButtonStyle.primary,
            custom_id=f"bet_{room_id}_1",
            disabled=not active,
        )
        btn2 = discord.ui.Button(
            label="🔴 Bet Team 2",
            style=discord.ButtonStyle.danger,
            custom_id=f"bet_{room_id}_2",
            disabled=not active,
        )

        btn1.callback = self._make_callback(1)
        btn2.callback = self._make_callback(2)

        self.add_item(btn1)
        self.add_item(btn2)

    def _make_callback(self, team: int):
        async def callback(interaction: discord.Interaction):
            await self._handle_bet(interaction, team)
        return callback

    async def _handle_bet(self, interaction: discord.Interaction, team: int):
        cog = self.cog

        # Are bets still open?
        state = cog._active_bets.get(self.room_id)
        if not state or not state["open"]:
            await interaction.response.send_message(
                "⏰ Betting for this match is already closed.", ephemeral=True
            )
            return

        db  = interaction.client.db
        uid = interaction.user.id

        # Registered?
        player = await db.get_player(uid)
        if not player:
            await interaction.response.send_message(
                "❌ You are not registered. Use `!register`.", ephemeral=True
            )
            return

        # Enough ELO?
        if player["elo"] < BET_MIN_ELO:
            await interaction.response.send_message(
                f"❌ You need at least **{BET_MIN_ELO} ELO** to place a bet.\n"
                "Play a few games to earn ELO first!",
                ephemeral=True,
            )
            return

        # Already bet?
        if uid in state["bets"]:
            already = state["bets"][uid]
            team_str = "🔵 Team 1" if already == 1 else "🔴 Team 2"
            await interaction.response.send_message(
                f"⚠️ You already placed a bet on **{team_str}**. Bets cannot be cancelled.",
                ephemeral=True,
            )
            return

        # Can't bet on a game you are playing in (check both in-memory state and DB)
        if uid in state["all_player_ids"]:
            await interaction.response.send_message(
                "❌ You cannot bet on a match you are playing in.",
                ephemeral=True,
            )
            return
        # Also block players who are currently in ANY active room (waiting/full/picking/started)
        active_room = await db.get_player_room(uid)
        if active_room:
            await interaction.response.send_message(
                "❌ You cannot place bets while you are in an active room.",
                ephemeral=True,
            )
            return

        team_str = "🔵 Team 1" if team == 1 else "🔴 Team 2"
        confirm_text = (
            f"🎰 You are about to bet on **{team_str}**\n\n"
            f"✅ **If your team wins:** {BET_WIN_ELO:+} ELO\n"
            f"❌ **If your team loses:** {BET_LOSE_ELO} ELO\n\n"
            "⚠️ **Bets cannot be cancelled after confirmation!**"
        )
        view = BetConfirmView(cog, self.room_id, team, uid)
        await interaction.response.send_message(confirm_text, view=view, ephemeral=True)


# ─────────────────────────────────────────────────────────────────────────────
#  Cog
# ─────────────────────────────────────────────────────────────────────────────

class Bets(commands.Cog):
    """
    Stores active bets in memory:

    _active_bets = {
        room_id: {
            "open":           bool,
            "start_time":     datetime,
            "message_id":     int,
            "channel_id":     int,
            "team1":          [...],
            "team2":          [...],
            "all_player_ids": set,
            "bets":           {uid: team},   # team = 1 or 2
            "size":           int,
            "mode":           str,
        }
    }
    """

    def __init__(self, bot):
        self.bot = bot
        self._active_bets: dict[int, dict] = {}
        self._close_tasks: dict[int, asyncio.Task] = {}

    async def cog_load(self):
        """Добавляет колонку is_bet в elo_history если её нет."""
        try:
            await self.bot.db.pool.execute(
                "ALTER TABLE elo_history ADD COLUMN IF NOT EXISTS is_bet BOOLEAN DEFAULT FALSE"
            )
        except Exception as e:
            log.warning("Could not add is_bet column: %s", e)

    # ── Helpers ──────────────────────────────────────────────────────────────

    async def _get_bets_channel(self, guild: discord.Guild) -> Optional[discord.TextChannel]:
        return discord.utils.find(
            lambda c: BETS_CHANNEL_NAME in c.name.lower(),
            guild.text_channels,
        )

    def _build_bet_embed(
        self,
        room_id: int,
        team1: list,
        team2: list,
        size: int,
        mode: str,
        *,
        open_bets: bool = True,
        winner_team: Optional[int] = None,
        bets: Optional[dict] = None,
        elo_changes: Optional[dict] = None,  # uid → (old, new, delta)
        cancelled: bool = False,
        mod_cancelled: bool = False,
    ) -> discord.Embed:

        mode_labels = {"team": "👥 Team", "random": "🎲 Random", "cap": "🎯 Captain"}
        mode_label  = mode_labels.get(mode, mode)

        if cancelled:
            title = f"🎰  Match #{room_id}  ·  {size}v{size}  ·  {mode_label}  ·  ❌ Cancelled"
            color = 0x95A5A6
        elif winner_team is None:
            title = f"🎰  Match #{room_id}  ·  {size}v{size}  ·  {mode_label}  ·  🟢 In Progress"
            color = 0x57F287 if open_bets else 0x95A5A6
        elif winner_team == 0:
            title = f"🎰  Match #{room_id}  ·  {size}v{size}  ·  {mode_label}  ·  🤝 Draw"
            color = 0x95A5A6
        elif winner_team == 1:
            title = f"🎰  Match #{room_id}  ·  {size}v{size}  ·  {mode_label}  ·  🔵 Team 1 Wins!"
            color = 0x3498DB
        else:
            title = f"🎰  Match #{room_id}  ·  {size}v{size}  ·  {mode_label}  ·  🔴 Team 2 Wins!"
            color = 0xE74C3C

        embed = discord.Embed(title=title, color=color)

        # Team rosters — two columns with VS in the middle
        def team_text(team_list: list) -> str:
            lines = []
            for p in team_list:
                rank, _ = get_rank(p["elo"])
                lines.append(f"**{p['username']}** — {p['elo']} ({rank})")
            return "\n".join(lines) or "—"

        embed.add_field(name="🔵 Team 1", value=team_text(team1), inline=True)
        embed.add_field(name="** **",     value="**VS**",           inline=True)
        embed.add_field(name="🔴 Team 2", value=team_text(team2), inline=True)

        # Description depending on state
        if open_bets and winner_team is None and not cancelled:
            embed.description = (
                f"⏳ Bets are open for **{BET_WINDOW_MINUTES} minutes** after the game starts.\n"
                f"🏆 Correct bet → **{BET_WIN_ELO:+} ELO**  |  "
                f"💀 Wrong bet → **{BET_LOSE_ELO} ELO**\n"
                f"⚠️ Minimum **{BET_MIN_ELO} ELO** required to bet."
            )
        elif not open_bets and winner_team is None and not cancelled:
            embed.description = "⏰ Betting window has closed."

        # Show live bets while game is still in progress (open or closed window, before result)
        if winner_team is None and not cancelled and bets:
            team1_ids = {p["discord_id"] for p in team1}
            live_t1 = [f"<@{uid}>" for uid, t in bets.items() if t == 1]
            live_t2 = [f"<@{uid}>" for uid, t in bets.items() if t == 2]
            bets_line = (
                f"🔵 **{len(live_t1)}** — " + (", ".join(live_t1) if live_t1 else "—") +
                f"\n🔴 **{len(live_t2)}** — " + (", ".join(live_t2) if live_t2 else "—")
            )
            embed.add_field(name="🎲 Current bets", value=bets_line, inline=False)

        if cancelled:
            if mod_cancelled:
                embed.description = (
                    "🔨 The match was cancelled by a moderator — "
                    "all bet ELO changes have been rolled back."
                )
            else:
                embed.description = (
                    "❌ The game ended before the betting window closed — "
                    "all bets have been cancelled, no ELO was changed."
                )

        # Results after game ends
        if winner_team is not None and bets and not cancelled:
            won_lines:  list[str] = []
            lost_lines: list[str] = []

            for uid, bet_team in bets.items():
                if elo_changes and uid in elo_changes:
                    old_e, new_e, delta = elo_changes[uid]
                    sign = "+" if delta >= 0 else ""
                    line = f"<@{uid}>: {old_e} → **{new_e}** ({sign}{delta})"
                else:
                    line = f"<@{uid}>"

                if winner_team == 0:
                    won_lines.append(line)
                elif bet_team == winner_team:
                    won_lines.append(line)
                else:
                    lost_lines.append(line)

            if winner_team == 0:
                embed.add_field(
                    name="🤝 Draw — all bets cancelled",
                    value="\n".join(won_lines) or "—",
                    inline=False,
                )
            else:
                if won_lines:
                    embed.add_field(
                        name=f"🏆 Correct bets (+{BET_WIN_ELO} ELO)",
                        value="\n".join(won_lines),
                        inline=False,
                    )
                if lost_lines:
                    embed.add_field(
                        name=f"💀 Wrong bets ({BET_LOSE_ELO} ELO)",
                        value="\n".join(lost_lines),
                        inline=False,
                    )

        embed.set_footer(text=f"Match #{room_id}")
        embed.timestamp = discord.utils.utcnow()
        return embed

    # ── Public API (called from rooms.py) ────────────────────────────────────

    async def on_game_start(
        self,
        room_id: int,
        team1: list,
        team2: list,
        size: int,
        mode: str,
    ):
        """Called from _do_start in rooms.py when a game begins."""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            return

        bets_channel = await self._get_bets_channel(guild)
        if not bets_channel:
            log.warning("Bets channel not found (looking for name containing '%s')", BETS_CHANNEL_NAME)
            return

        all_ids = {p["discord_id"] for p in team1 + team2}

        state: dict = {
            "open":           True,
            "start_time":     datetime.datetime.utcnow(),
            "message_id":     None,
            "channel_id":     bets_channel.id,
            "team1":          team1,
            "team2":          team2,
            "all_player_ids": all_ids,
            "bets":           {},
            "size":           size,
            "mode":           mode,
        }
        self._active_bets[room_id] = state

        embed = self._build_bet_embed(room_id, team1, team2, size, mode, open_bets=True)
        view  = BetView(self, room_id, active=True)

        msg = await bets_channel.send(embed=embed, view=view)
        state["message_id"] = msg.id

        self.bot.add_view(view, message_id=msg.id)

        task = asyncio.create_task(self._close_bets_after(room_id, BET_WINDOW_MINUTES * 60))
        self._close_tasks[room_id] = task

    async def _close_bets_after(self, room_id: int, delay: float):
        await asyncio.sleep(delay)
        await self._close_bets(room_id)

    async def _close_bets(self, room_id: int):
        """Closes the betting window when the timer expires."""
        state = self._active_bets.get(room_id)
        if not state or not state["open"]:
            return

        state["open"] = False

        guild   = self.bot.get_guild(Config.GUILD_ID)
        channel = guild.get_channel(state["channel_id"]) if guild else None
        if not channel:
            return

        try:
            msg = await channel.fetch_message(state["message_id"])
        except (discord.NotFound, discord.HTTPException):
            return

        embed = self._build_bet_embed(
            room_id,
            state["team1"], state["team2"],
            state["size"],  state["mode"],
            open_bets=False,
            bets=state["bets"],
        )
        await msg.edit(
            content="⏰ Betting window closed.",
            embed=embed,
            view=BetView(self, room_id, active=False),
        )

    async def on_game_end(
        self,
        room_id: int,
        winner_team: int,           # 0 = draw, 1 = team1, 2 = team2
        player_elo_changes: dict,   # uid → (old_elo, new_elo, delta, result)
    ):
        """Called from _finalize_game in rooms.py when the match ends."""
        state = self._active_bets.pop(room_id, None)

        task = self._close_tasks.pop(room_id, None)
        if task:
            task.cancel()

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            return

        if state is None:
            return

        bets_channel = guild.get_channel(state["channel_id"])
        if not bets_channel:
            return

        try:
            msg = await bets_channel.fetch_message(state["message_id"])
        except (discord.NotFound, discord.HTTPException):
            msg = None

        # If betting window is still open — close it now, then settle bets normally
        if state["open"]:
            state["open"] = False
            if task:
                task.cancel()

        # Apply bet outcomes
        db = self.bot.db
        bet_elo_changes: dict[int, tuple] = {}  # uid → (old, new, delta)

        for uid, bet_team in state["bets"].items():
            player = await db.get_player(uid)
            if not player:
                continue

            old_elo = player["elo"]

            if winner_team == 0:
                # Draw — no ELO change
                delta   = 0
                new_elo = old_elo
            elif bet_team == winner_team:
                delta   = BET_WIN_ELO
                new_elo = old_elo + delta
            else:
                delta   = BET_LOSE_ELO
                new_elo = max(0, old_elo + delta)

            if delta != 0:
                await db.pool.execute(
                    "UPDATE players SET elo=$1 WHERE discord_id=$2",
                    new_elo, uid,
                )
                await db.pool.execute(
                    """
                    INSERT INTO elo_history (discord_id, elo_before, elo_after, change, game_id, is_bet)
                    VALUES ($1, $2, $3, $4, $5, TRUE)
                    """,
                    uid, old_elo, new_elo, delta, room_id,
                )
                # Sync rank role
                member = guild.get_member(uid)
                if member:
                    try:
                        from cogs.register import Register
                        reg_cog: Register = self.bot.cogs.get("Register")
                        if reg_cog:
                            await reg_cog._sync_rank_role(member, new_elo)
                    except Exception:
                        pass

            bet_elo_changes[uid] = (old_elo, new_elo, delta)

        embed = self._build_bet_embed(
            room_id,
            state["team1"], state["team2"],
            state["size"],  state["mode"],
            open_bets=False,
            winner_team=winner_team,
            bets=state["bets"],
            elo_changes=bet_elo_changes,
        )

        if msg:
            await msg.edit(embed=embed, view=BetView(self, room_id, active=False))

    async def on_game_cancelled(self, room_id: int):
        """
        Called from rooms.py when a moderator force-deletes an active room
        (!delete / !mod_end). Cancels the bet embed and, if the betting window
        had already closed and ELO was distributed, rolls it back.
        """
        state = self._active_bets.pop(room_id, None)

        task = self._close_tasks.pop(room_id, None)
        if task:
            task.cancel()

        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            return

        if state is None:
            return

        bets_channel = guild.get_channel(state["channel_id"])
        if not bets_channel:
            return

        try:
            msg = await bets_channel.fetch_message(state["message_id"])
        except (discord.NotFound, discord.HTTPException):
            msg = None

        # If betting window was still open — no ELO was ever issued, just update embed
        if state["open"]:
            state["open"] = False
            embed = self._build_bet_embed(
                room_id,
                state["team1"], state["team2"],
                state["size"],  state["mode"],
                cancelled=True,
                mod_cancelled=True,
            )
            if msg:
                await msg.edit(embed=embed, view=BetView(self, room_id, active=False))
            return

        # Betting window was already closed — check if ELO was actually distributed.
        # on_game_end applies ELO; if the room was deleted BEFORE game ended,
        # on_game_end was never called, so no ELO change occurred for bets.
        # But to be safe, check elo_history and roll back any bet rows found.
        db = self.bot.db
        rolled_back: list[int] = []

        for uid in state["bets"]:
            row = await db.pool.fetchrow(
                "SELECT id, elo_before FROM elo_history "
                "WHERE discord_id=$1 AND game_id=$2 ORDER BY id DESC LIMIT 1",
                uid, room_id,
            )
            if not row:
                continue

            await db.pool.execute(
                "UPDATE players SET elo=$1 WHERE discord_id=$2",
                row["elo_before"], uid,
            )
            await db.pool.execute("DELETE FROM elo_history WHERE id=$1", row["id"])

            member = guild.get_member(uid)
            if member:
                try:
                    from cogs.register import Register
                    reg_cog: Register = self.bot.cogs.get("Register")
                    if reg_cog:
                        await reg_cog._sync_rank_role(member, row["elo_before"])
                except Exception:
                    pass
            rolled_back.append(uid)

        log.info(
            "Mod-cancelled room #%s: rolled back bet ELO for %d user(s): %s",
            room_id, len(rolled_back), rolled_back,
        )

        embed = self._build_bet_embed(
            room_id,
            state["team1"], state["team2"],
            state["size"],  state["mode"],
            cancelled=True,
            mod_cancelled=True,
        )
        if msg:
            await msg.edit(embed=embed, view=BetView(self, room_id, active=False))

    async def on_bet_match_cancelled(self, game_id: int):
        """
        Called from rooms.py !cancel when a FINISHED match is cancelled by a mod.
        db.cancel_match() already rolls back all elo_history rows for game_id,
        which includes bet ELO rows (they share the same game_id). So no extra
        DB work is needed here — we just log for visibility.
        """
        log.info(
            "!cancel for finished match #%s: bet ELO already rolled back by cancel_match().",
            game_id,
        )

    # ── Internal: record the bet ──────────────────────────────────────────────

    async def _place_bet(self, user: discord.Member, room_id: int, team: int) -> str:
        state = self._active_bets.get(room_id)
        if not state or not state["open"]:
            return "⏰ Betting is already closed for this match."

        if user.id in state["bets"]:
            t = "🔵 Team 1" if state["bets"][user.id] == 1 else "🔴 Team 2"
            return f"⚠️ You already bet on **{t}**."

        state["bets"][user.id] = team
        team_str = "🔵 Team 1" if team == 1 else "🔴 Team 2"
        log.info("Bet placed: user=%s room=%s team=%s", user.id, room_id, team)

        # Update the public embed to show the new bet
        guild = self.bot.get_guild(Config.GUILD_ID)
        if guild:
            channel = guild.get_channel(state["channel_id"])
            if channel:
                try:
                    msg = await channel.fetch_message(state["message_id"])
                    embed = self._build_bet_embed(
                        room_id,
                        state["team1"], state["team2"],
                        state["size"],  state["mode"],
                        open_bets=state["open"],
                        bets=state["bets"],
                    )
                    await msg.edit(embed=embed)
                except (discord.NotFound, discord.HTTPException):
                    pass

        return (
            f"✅ Bet confirmed on **{team_str}**!\n"
            f"🏆 Win → **{BET_WIN_ELO:+} ELO**  |  💀 Loss → **{BET_LOSE_ELO} ELO**"
        )


async def setup(bot):
    await bot.add_cog(Bets(bot))
