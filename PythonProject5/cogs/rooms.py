from __future__ import annotations

import asyncio
import datetime
import random
from typing import Optional

import discord
from discord.ext import commands, tasks

from config import Config, get_rank
from utils.elo import calculate_elo, team_avg
from utils.embeds import room_embed


# ────────────────────────────────────────────────────────────────
#  Views
# ────────────────────────────────────────────────────────────────

class CreateRoomButton(discord.ui.Button):
    """Кнопка создания комнаты с конкретным размером и режимом."""

    MODE_STYLES = {
        "team": (discord.ButtonStyle.primary, "👥"),
        "random": (discord.ButtonStyle.secondary, "🎲"),
        "cap": (discord.ButtonStyle.success, "🎯"),
    }
    MODE_LABELS = {
        "team": "Team / Команда",
        "random": "Random / Рандом",
        "cap": "Captain / Капитан",
    }

    def __init__(self, size: int, mode: str, row: int = 0):
        style, emoji = self.MODE_STYLES[mode]
        super().__init__(
            label=f"{size}v{size}  {self.MODE_LABELS[mode]}",
            style=style,
            emoji=emoji,
            custom_id=f"create_{size}_{mode}",
            row=row,
        )
        self.size = size
        self.mode = mode

    async def callback(self, interaction: discord.Interaction):
        cog: "Rooms" = interaction.client.cogs.get("Rooms")  # type: ignore
        if not cog:
            await interaction.response.send_message("Ошибка: cog не найден.", ephemeral=True)
            return

        db = interaction.client.db
        player = await db.get_player(interaction.user.id)
        if not player:
            await interaction.response.send_message(
                "❌ Ты не зарегистрирован. Используй `!register <ник>`.", ephemeral=True
            )
            return

        if await db.get_player_room(interaction.user.id):
            await interaction.response.send_message(
                "❌ Ты уже в комнате. Выйди через кнопку **🚪 Покинуть** или `!exit`.", ephemeral=True
            )
            return

        # Отвечаем сразу, потом создаём комнату
        await interaction.response.defer(ephemeral=True)

        # Используем _create_room через фейковый ctx-подобный объект
        # Проще — вызвать внутренний метод напрямую
        guild = interaction.guild
        category = await cog._get_or_create_category(guild)

        overwrite = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        mod_role = discord.utils.get(guild.roles, name=Config.MODERATOR_ROLE_NAME)
        if mod_role:
            overwrite[mod_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

        room_id = await db.create_room(0, self.size, interaction.user.id, mode=self.mode)
        channel = await guild.create_text_channel(
            f"room-{room_id}",
            category=category,
            overwrites=overwrite,
        )
        await db.update_channel_id(room_id, channel.id)

        if self.mode == "team":
            await db.add_to_room(room_id, interaction.user.id, team=1, is_captain=False)
        else:
            await db.add_to_room(room_id, interaction.user.id, team=0, is_captain=False)

        await cog._allow_channel(channel, interaction.user)

        players = await db.get_room_players(room_id)
        embed = room_embed(room_id, self.size, players, mode=self.mode)
        view = RoomView(interaction.client, room_id, room_status="waiting", room_mode=self.mode, room_size=self.size)
        msg = await channel.send(embed=embed, view=view)
        await msg.pin()
        await db.update_embed_id(room_id, msg.id)

        mode_labels = {
            "team": "👥 Team / Командный",
            "random": "🎲 Random / Рандомный",
            "cap": "🎯 Captain / Капитанский",
        }
        # Удаляем панель выбора из чата
        try:
            await interaction.message.delete()
        except (discord.Forbidden, discord.NotFound):
            pass

        # Объявляем в текущем канале кто и что создал
        await interaction.channel.send(
            f"🎮 {interaction.user.mention} создал комнату "
            f"**#{room_id}** · **{self.size}v{self.size}** · {mode_labels[self.mode]} → {channel.mention}"
        )

        await interaction.followup.send(
            f"✅ Комната **#{room_id}** создана! Иди в {channel.mention}",
            ephemeral=True,
        )
        await cog._refresh_lobby()


class Create1v1Button(discord.ui.Button):
    """Кнопка создания 1v1 комнаты — без выбора режима (всегда team)."""

    def __init__(self, row: int = 3):
        super().__init__(
            label="1v1",
            style=discord.ButtonStyle.primary,
            emoji="⚔️",
            custom_id="create_1_team",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        # Переиспользуем логику CreateRoomButton с size=1, mode=team
        btn = CreateRoomButton(size=1, mode="team", row=3)
        btn._view = self._view  # type: ignore
        await btn.callback(interaction)


class CreateRoomView(discord.ui.View):
    """
    Панель выбора режима и размера при вызове !create без аргументов.
    Строка 0: 4v4 — все три режима
    Строка 1: 3v3 — все три режима
    Строка 2: 2v2 — все три режима
    Строка 3: 1v1 — одна кнопка (режим team, без выбора)
    """

    def __init__(self):
        # timeout=None обязателен — это persistent view (регистрируется через bot.add_view при старте)
        super().__init__(timeout=None)
        for row_idx, size in enumerate([4, 3, 2]):
            for mode in ("team", "random", "cap"):
                self.add_item(CreateRoomButton(size, mode, row=row_idx))
        # 1v1 — только одна кнопка, режим team (капитаны = сами игроки)
        self.add_item(Create1v1Button(row=3))


class JoinButton(discord.ui.Button):
    """Кнопка «Присоединиться» в лобби."""

    def __init__(self, room_id: int, size: int, mode: str, is_full: bool):
        if is_full:
            super().__init__(
                label="Комната полная",
                style=discord.ButtonStyle.secondary,
                emoji="🔴",
                disabled=True,
                custom_id=f"join_full_{room_id}",
            )
        else:
            super().__init__(
                label="Присоединиться",
                style=discord.ButtonStyle.success,
                emoji="🎮",
                custom_id=f"join_room_{room_id}",
            )
        self.room_id = room_id
        self.size = size
        self.mode = mode
        self._is_full = is_full

    async def callback(self, interaction: discord.Interaction):
        if self._is_full:
            await interaction.response.send_message("❌ Комната уже полная.", ephemeral=True)
            return

        # Откладываем ответ немедленно — все дальнейшие операции займут > 3 сек
        await interaction.response.defer(ephemeral=True)

        bot = interaction.client
        db = bot.db

        player = await db.get_player(interaction.user.id)
        if not player:
            await interaction.followup.send(
                "❌ Ты не зарегистрирован. Используй `!register <ник>`.",
                ephemeral=True,
            )
            return

        existing = await db.get_player_room(interaction.user.id)
        if existing:
            await interaction.followup.send(
                "❌ Ты уже в комнате. Выйди через кнопку **Покинуть** или `!exit`.",
                ephemeral=True,
            )
            return

        room = await db.get_room(self.room_id)
        if not room or room["status"] not in ("waiting", "picking"):
            await interaction.followup.send(
                "❌ Эта комната больше недоступна.", ephemeral=True
            )
            return

        players = await db.get_room_players(self.room_id)
        if len(players) >= self.size * 2:
            await interaction.followup.send("❌ Комната уже заполнена.", ephemeral=True)
            return

        # Определяем команду
        mode = room["mode"]
        if mode in ("random", "cap"):
            team = 0
        else:
            team1 = [p for p in players if p["team"] == 1]
            team2 = [p for p in players if p["team"] == 2]
            team = 1 if len(team1) < self.size else 2

        await db.add_to_room(self.room_id, interaction.user.id, team=team)

        guild = interaction.guild
        rooms_cog = bot.cogs.get("Rooms")

        channel = guild.get_channel(room["channel_id"]) if guild else None
        if channel and rooms_cog:
            await rooms_cog._allow_channel(channel, interaction.user)
            await channel.send(
                f"👋 {interaction.user.mention} вошёл в комнату"
                + (f" (Команда {team})" if mode == "team" and team else "")
            )

        players = await db.get_room_players(self.room_id)
        total_slots = self.size * 2

        if len(players) == total_slots and rooms_cog:
            if mode == "random":
                await rooms_cog._randomize_teams(self.room_id, players, self.size, channel)
            elif mode == "cap":
                await rooms_cog._start_captain_pick(self.room_id, players, self.size, channel)
            elif mode == "team":
                await db.update_room_status(self.room_id, "full")
                if channel:
                    await channel.send("🎯 Комната заполнена! Нажмите **▶ Start** чтобы начать!")

        if rooms_cog:
            await rooms_cog._refresh_room_embed(self.room_id)
            await rooms_cog._refresh_lobby()

        await interaction.followup.send(
            f"✅ Ты в комнате **#{self.room_id}**! Перейди в {channel.mention if channel else 'канал комнаты'}.",
            ephemeral=True,
        )


class JoinRoomView(discord.ui.View):
    """View с кнопкой «Присоединиться» для лобби."""

    def __init__(self, room_id: int, size: int, mode: str, is_full: bool):
        super().__init__(timeout=None)
        self.add_item(JoinButton(room_id, size, mode, is_full))


# ── RoomView ──────────────────────────────────────────────────────

class ExitButton(discord.ui.Button):
    def __init__(self, room_id: int):
        super().__init__(
            label="Покинуть",
            style=discord.ButtonStyle.danger,
            emoji="🚪",
            custom_id=f"exit_room_{room_id}",
            row=1,
        )
        self.room_id = room_id

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        db = bot.db
        room = await db.get_player_room(interaction.user.id)
        if not room or room["room_id"] != self.room_id:
            await interaction.response.send_message("Ты не в этой комнате.", ephemeral=True)
            return

        players = await db.get_room_players(self.room_id)
        me = next((p for p in players if p["discord_id"] == interaction.user.id), None)
        was_captain = bool(me and me["is_captain"])
        my_team = me["team"] if me else None

        game_was_started = room["status"] == "started"

        await db.remove_from_room(self.room_id, interaction.user.id)

        guild = interaction.guild
        rooms_cog = bot.cogs.get("Rooms")
        channel = guild.get_channel(room["channel_id"]) if guild else None

        if channel and rooms_cog:
            await rooms_cog._deny_channel(channel, interaction.user)

        # Штраф за выход во время игры
        if game_was_started:
            new_elo = await db.deduct_elo_for_leave(interaction.user.id, 15)
            if channel:
                await channel.send(
                    f"⚠️ {interaction.user.mention} покинул игру и получает штраф **-15 ELO** "
                    f"(теперь {new_elo} ELO). Игра отменена, комната возобновляет набор игроков."
                )
            # Обновляем ранговую роль
            from cogs.register import Register
            reg_cog: Register = bot.cogs.get("Register")  # type: ignore
            if reg_cog and guild:
                member = guild.get_member(interaction.user.id)
                if member:
                    await reg_cog._sync_rank_role(member, new_elo)
        else:
            if channel:
                await channel.send(f"👋 {interaction.user.mention} покинул комнату.")

        players = await db.get_room_players(self.room_id)

        if not players:
            await interaction.response.send_message("✅ Ты покинул комнату. Она удалена (все ушли).", ephemeral=True)
            if channel:
                await channel.delete(reason="Комната пуста")
            await db.delete_room(self.room_id)
            if rooms_cog:
                await rooms_cog._refresh_lobby()
            return

        if was_captain and my_team and rooms_cog:
            team_left = [p for p in players if p["team"] == my_team]
            if team_left:
                new_cap = max(team_left, key=lambda p: p["elo"])
                await db.set_captain(self.room_id, new_cap["discord_id"], True)
                if channel:
                    m = guild.get_member(new_cap["discord_id"])
                    if m:
                        await channel.send(f"👑 {m.mention} назначен новым капитаном команды {my_team}.")

        # Сброс статуса в waiting (и при старте, и при full/picking)
        if room["status"] in ("full", "picking", "started"):
            await db.update_room_status(self.room_id, "waiting")
            await db.set_ready(self.room_id, 1, False)
            await db.set_ready(self.room_id, 2, False)
            # Сбрасываем end_vote всем игрокам комнаты
            for p in players:
                await db.set_end_vote(self.room_id, p["discord_id"], None)

        if rooms_cog:
            await rooms_cog._refresh_room_embed(self.room_id)
            await rooms_cog._refresh_lobby()

        await interaction.response.send_message(
            "✅ Ты покинул комнату." + (" **-15 ELO** за выход из активной игры." if game_was_started else ""),
            ephemeral=True,
        )


class ReadyButton(discord.ui.Button):
    def __init__(self, team: int, room_id: int):
        super().__init__(
            label=f"✅ Команда {team} Ready",
            style=discord.ButtonStyle.success,
            custom_id=f"ready_t{team}_{room_id}",
            row=0,
        )
        self.team = team
        self.room_id = room_id

    async def callback(self, interaction: discord.Interaction):
        db = interaction.client.db
        room = await db.get_room(self.room_id)
        if not room or room["status"] not in ("waiting", "full"):
            await interaction.response.send_message("Комната недоступна.", ephemeral=True)
            return
        players = await db.get_room_players(self.room_id)
        cap = next((p for p in players if p["team"] == self.team and p["is_captain"]), None)
        if not cap or cap["discord_id"] != interaction.user.id:
            await interaction.response.send_message(
                f"Только капитан Команды {self.team} может нажать Ready.", ephemeral=True
            )
            return
        new_val = not bool(cap["confirmed_start"])
        await db.set_ready(self.room_id, self.team, new_val)
        await interaction.response.defer()
        rooms_cog = interaction.client.cogs.get("Rooms")
        if rooms_cog:
            await rooms_cog._refresh_room_embed(self.room_id)


class StartButton(discord.ui.Button):
    def __init__(self, room_id: int):
        super().__init__(
            label="▶ Start Game",
            style=discord.ButtonStyle.primary,
            emoji="🚀",
            custom_id=f"start_btn_{room_id}",
            row=0,
        )
        self.room_id = room_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rooms_cog = interaction.client.cogs.get("Rooms")
        if rooms_cog:
            await rooms_cog._do_start(interaction.user, interaction.channel, self.room_id)


class PickTeamButton(discord.ui.Button):
    """Кнопка выбора команды в режиме team."""

    def __init__(self, team: int, room_id: int):
        super().__init__(
            label=f"🔵 Команда {team}" if team == 1 else f"🔴 Команда {team}",
            style=discord.ButtonStyle.primary if team == 1 else discord.ButtonStyle.danger,
            custom_id=f"pickteam_{team}_{room_id}",
            row=0,
        )
        self.team = team
        self.room_id = room_id

    async def callback(self, interaction: discord.Interaction):
        db = interaction.client.db
        room = await db.get_player_room(interaction.user.id)
        if not room or room["room_id"] != self.room_id:
            await interaction.response.send_message("Ты не в этой комнате.", ephemeral=True)
            return
        if room["mode"] != "team":
            await interaction.response.send_message("Кнопка доступна только в режиме team.", ephemeral=True)
            return
        if room["status"] == "started":
            await interaction.response.send_message("Игра уже началась.", ephemeral=True)
            return

        players = await db.get_room_players(self.room_id)
        me = next((p for p in players if p["discord_id"] == interaction.user.id), None)
        if not me:
            await interaction.response.send_message("Ты не найден в комнате.", ephemeral=True)
            return

        # Уже в этой команде
        if me["team"] == self.team:
            await interaction.response.send_message(f"Ты уже в Команде {self.team}.", ephemeral=True)
            return

        size = room["size"]
        team_players = [p for p in players if p["team"] == self.team]
        if len(team_players) >= size:
            await interaction.response.send_message(
                f"Команда {self.team} уже заполнена ({size}/{size}). Сначала кто-то должен перейти из неё.",
                ephemeral=True
            )
            return

        await db.set_player_team(self.room_id, interaction.user.id, self.team)

        guild = interaction.guild
        channel = guild.get_channel(room["channel_id"]) if guild else None
        if channel:
            await channel.send(f"🔀 {interaction.user.mention} перешёл в Команду {self.team}.")

        players = await db.get_room_players(self.room_id)
        t1 = [p for p in players if p["team"] == 1]
        t2 = [p for p in players if p["team"] == 2]

        rooms_cog = interaction.client.cogs.get("Rooms")
        if len(t1) == size and len(t2) == size:
            await db.update_room_status(self.room_id, "full")
            if channel:
                await channel.send("🎯 Команды сформированы! Нажмите **▶ Start** чтобы начать!")
                if rooms_cog:
                    await rooms_cog._announce_strong_side(channel, self.room_id)
        else:
            if room["status"] == "full":
                await db.update_room_status(self.room_id, "waiting")

        if rooms_cog:
            await rooms_cog._refresh_room_embed(self.room_id)

        await interaction.response.send_message(f"✅ Ты в Команде {self.team}!", ephemeral=True)


class VoteEndView(discord.ui.View):
    """View для голосования результата игры (Win/Lose/Draw)."""

    def __init__(self, room_id: int):
        super().__init__(timeout=None)
        self.room_id = room_id
        self.add_item(VoteButton("win", room_id))
        self.add_item(VoteButton("draw", room_id))
        self.add_item(VoteButton("lose", room_id))


class VoteButton(discord.ui.Button):
    STYLES = {
        "win": (discord.ButtonStyle.success, "🏆 Победа"),
        "draw": (discord.ButtonStyle.secondary, "🤝 Ничья"),
        "lose": (discord.ButtonStyle.danger, "💀 Поражение"),
    }

    def __init__(self, vote: str, room_id: int):
        style, label = self.STYLES[vote]
        super().__init__(
            label=label,
            style=style,
            custom_id=f"vote_{vote}_{room_id}",
        )
        self.vote = vote
        self.room_id = room_id

    async def callback(self, interaction: discord.Interaction):
        rooms_cog = interaction.client.cogs.get("Rooms")
        if not rooms_cog:
            await interaction.response.send_message("Ошибка.", ephemeral=True)
            return

        db = interaction.client.db

        # Базовые проверки ДО лока (быстро, без состояния гонки)
        room = await db.get_player_room(interaction.user.id)
        if not room or room["room_id"] != self.room_id or room["status"] not in ("started",):
            await interaction.response.send_message(
                "Ты не в этой игре или игра не активна.", ephemeral=True
            )
            return

        players = await db.get_room_players(self.room_id)
        me = next((p for p in players if p["discord_id"] == interaction.user.id), None)
        if not me:
            await interaction.response.send_message("Ты не в этой игре.", ephemeral=True)
            return

        # В cap/random режиме — только капитан голосует; в team-режиме — любой игрок
        if room["mode"] != "team" and not me["is_captain"]:
            await interaction.response.send_message("Только капитан может голосовать.", ephemeral=True)
            return

        # Проверяем что хотя бы один скрин загружен
        screenshots = await db.get_screenshots(self.room_id)
        if not screenshots:
            await interaction.response.send_message(
                "⚠️ Сначала загрузи скриншот результата в чат.", ephemeral=True
            )
            return

        # Проверяем — не проголосовал ли уже этот игрок
        if me["end_vote"]:
            await interaction.response.send_message(
                f"Ты уже проголосовал: **{me['end_vote']}**.", ephemeral=True
            )
            return

        await db.set_end_vote(self.room_id, interaction.user.id, self.vote)
        await interaction.response.send_message(
            f"✅ Твой голос: **{self.vote}**. Ждём голоса с другой команды.", ephemeral=True
        )

        # Получаем (или создаём) lock для этой комнаты
        lock = rooms_cog._finalize_locks.setdefault(self.room_id, asyncio.Lock())
        async with lock:
            # Перечитываем всё ВНУТРИ лока — это единственное место где принимается решение
            room = await db.get_room(self.room_id)
            if not room or room["status"] != "started":
                return  # другой вызов уже финализирует

            players = await db.get_room_players(self.room_id)
            await rooms_cog._try_resolve_votes(room, players, interaction.guild)


class RoomView(discord.ui.View):
    """View прикреплённая к эмбеду комнаты — кнопки меняются по статусу."""

    def __init__(self, bot, room_id: int, room_status: str = "waiting", room_mode: str = "team", room_size: int = 4):
        super().__init__(timeout=None)
        self.bot = bot
        self.room_id = room_id

        if room_status == "started":
            # Игра идёт — кнопки голосования + репорт
            self.add_item(VoteButton("win", room_id))
            self.add_item(VoteButton("draw", room_id))
            self.add_item(VoteButton("lose", room_id))
            self.add_item(ExitButton(room_id))
            self.add_item(ReportRoomButton(room_id, row=1))
        elif room_status == "awaiting_screenshot":
            # Ждём скрин — только репорт и выход
            self.add_item(ExitButton(room_id))
            self.add_item(ReportRoomButton(room_id, row=1))
        else:
            # До старта — Start + выбор команды (если team и не 1v1) + выход + репорт
            self.add_item(StartButton(room_id))
            if room_mode == "team" and room_size > 1:
                self.add_item(PickTeamButton(1, room_id))
                self.add_item(PickTeamButton(2, room_id))
            self.add_item(ExitButton(room_id))
            self.add_item(ReportRoomButton(room_id, row=2))


class ReportRoomButton(discord.ui.Button):
    """Кнопка вызова администрации из комнаты."""

    def __init__(self, room_id: int, row: int = 2):
        super().__init__(
            label="Вызвать админа",
            style=discord.ButtonStyle.danger,
            emoji="🚨",
            custom_id=f"report_room_{room_id}",
            row=row,
        )
        self.room_id = room_id

    async def callback(self, interaction: discord.Interaction):
        bot = interaction.client
        db = bot.db
        room = await db.get_player_room(interaction.user.id)
        if not room or room["room_id"] != self.room_id:
            await interaction.response.send_message("Ты не в этой комнате.", ephemeral=True)
            return

        guild = interaction.guild
        admin_channel = discord.utils.find(
            lambda c: Config.ADMIN_CHANNEL_NAME in c.name or c.name == Config.ADMIN_CHANNEL_NAME,
            guild.text_channels,
        )
        if admin_channel:
            mod_role = discord.utils.get(guild.roles, name=Config.MODERATOR_ROLE_NAME)
            mention = mod_role.mention if mod_role else "@Модератор"
            embed = discord.Embed(
                title="🚨 Вызов администрации из комнаты",
                color=0xED4245,
            )
            embed.add_field(
                name="Комната", value=f"**#{self.room_id}** {interaction.channel.mention}", inline=True
            )
            embed.add_field(
                name="Вызвал", value=f"{interaction.user.mention} (`{interaction.user}`)", inline=True
            )
            await admin_channel.send(f"{mention}", embed=embed)

        await interaction.response.send_message(
            "✅ Администрация уведомлена. Модератор скоро придёт.", ephemeral=True
        )


class PickView(discord.ui.View):
    """View для режима капитанского пика — кнопки с именами игроков."""

    def __init__(self, bot, room_id: int, unpicked: list):
        super().__init__(timeout=120)
        self.bot = bot
        self.room_id = room_id
        for p in unpicked:
            self.add_item(PickButton(p["discord_id"], p["username"], p["elo"]))


class PickButton(discord.ui.Button):
    def __init__(self, discord_id: int, username: str, elo: int):
        rank, _ = get_rank(elo)
        super().__init__(
            label=f"{username} ({elo} · {rank})",
            style=discord.ButtonStyle.secondary,
            custom_id=f"pick_{discord_id}",
        )
        self.target_id = discord_id

    async def callback(self, interaction: discord.Interaction):
        cog: Rooms = interaction.client.cogs["Rooms"]  # type: ignore
        await cog._handle_pick(interaction, self.target_id)


# ────────────────────────────────────────────────────────────────
#  Cog
# ────────────────────────────────────────────────────────────────

class Rooms(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.game_timeout_loop.start()
        # asyncio.Lock на каждую комнату — гарантирует что финализация
        # не запустится дважды даже при одновременных нажатиях кнопок
        self._finalize_locks: dict[int, asyncio.Lock] = {}

    def cog_unload(self):
        self.game_timeout_loop.cancel()

    # ── helpers ──────────────────────────────────────────────────

    def _is_guild(self, ctx) -> bool:
        return ctx.guild and ctx.guild.id == Config.GUILD_ID

    async def _get_or_create_category(self, guild: discord.Guild) -> discord.CategoryChannel:
        # Сначала ищем категорию PLAY🟢, потом fallback на CATEGORY_NAME
        cat = discord.utils.find(
            lambda c: "PLAY" in c.name.upper() or c.name == Config.CATEGORY_NAME,
            guild.categories,
        )
        if cat is None:
            cat = await guild.create_category(
                Config.CATEGORY_NAME,
                overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=False)},
            )
        return cat

    async def _get_or_create_results_channel(self, guild: discord.Guild) -> discord.TextChannel:
        channel = discord.utils.get(guild.text_channels, name=Config.RESULTS_CHANNEL_NAME)
        if channel is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=False,
                    add_reactions=False,
                ),
            }
            mod_role = discord.utils.get(guild.roles, name=Config.MODERATOR_ROLE_NAME)
            if mod_role:
                overwrites[mod_role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                )
            channel = await guild.create_text_channel(
                Config.RESULTS_CHANNEL_NAME,
                overwrites=overwrites,
                topic="📊 Результаты всех матчей. Только для чтения.",
                reason="Автосоздание канала результатов",
            )
        return channel

    def _is_mod(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True
        role = discord.utils.get(member.guild.roles, name=Config.MODERATOR_ROLE_NAME)
        return role in member.roles if role else False

    async def _deny_channel(self, channel: discord.TextChannel, member: discord.Member):
        await channel.set_permissions(member, overwrite=None)

    async def _allow_channel(self, channel: discord.TextChannel, member: discord.Member):
        await channel.set_permissions(
            member,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )

    async def _refresh_room_embed(self, room_id: int):
        room = await self.bot.db.get_room(room_id)
        if not room or not room["embed_message_id"]:
            return
        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            return
        channel = guild.get_channel(room["channel_id"])
        if not channel:
            return
        try:
            msg = await channel.fetch_message(room["embed_message_id"])
        except discord.NotFound:
            return
        players = await self.bot.db.get_room_players(room_id)
        embed = room_embed(room_id, room["size"], players, mode=room["mode"])
        view = RoomView(self.bot, room_id, room_status=room["status"], room_mode=room["mode"], room_size=room["size"])
        await msg.edit(embed=embed, view=view)

    async def _assign_team2_captain(self, room_id: int):
        """Назначает капитана команды 2, если его ещё нет."""
        players = await self.bot.db.get_room_players(room_id)
        team2 = [p for p in players if p["team"] == 2]
        if not team2:
            return
        # Если капитан уже есть — не трогаем
        if any(p["is_captain"] for p in team2):
            return
        best = max(team2, key=lambda p: p["elo"])
        await self.bot.db.set_captain(room_id, best["discord_id"], True)

    def _random_strong_side() -> str:
        """Случайно выбирает сильную сторону."""
        return random.choice(["🔵 Команда 1", "🔴 Команда 2"])

    async def _announce_strong_side(self, channel: discord.TextChannel, room_id: int):
        """Объявляет рандомную сильную сторону в канале комнаты."""
        strong = random.choice(["🔵 Команда 1", "🔴 Команда 2"])
        embed = discord.Embed(
            title="⚔️ Распределение сторон",
            description=f"**{strong}** играет за **СИЛЬНУЮ СТОРОНУ**!\n\nУдачи всем участникам!",
            color=0xE67E22,
        )
        await channel.send(embed=embed)

    # ── Lobby channel ─────────────────────────────────────────────

    async def _get_or_create_lobby_channel(self, guild: discord.Guild) -> discord.TextChannel:
        """Возвращает канал лобби — ищет по PLAY_CHANNEL_NAME, создаёт если нет."""
        channel = discord.utils.find(
            lambda c: c.name == Config.PLAY_CHANNEL_NAME or Config.LOBBY_CHANNEL_NAME in c.name,
            guild.text_channels,
        )
        if channel is None:
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=False,
                    add_reactions=False,
                ),
            }
            mod_role = discord.utils.get(guild.roles, name=Config.MODERATOR_ROLE_NAME)
            if mod_role:
                overwrites[mod_role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True
                )
            channel = await guild.create_text_channel(
                Config.LOBBY_CHANNEL_NAME,
                overwrites=overwrites,
                topic="🎮 Открытые комнаты. Используй !q <размер> <режим> чтобы войти.",
                reason="Автосоздание канала лобби",
            )
        return channel

    async def _refresh_lobby(self):
        """Обновляет канал лобби. Удаляет все старые сообщения и постит актуальные."""
        guild = self.bot.get_guild(Config.GUILD_ID)
        if not guild:
            return

        lobby = await self._get_or_create_lobby_channel(guild)
        db = self.bot.db
        all_rooms = await db.get_open_rooms()

        # Удаляем старые сообщения бота по одному (не требует Manage Messages)
        try:
            async for msg in lobby.history(limit=50):
                if msg.author == self.bot.user:
                    try:
                        await msg.delete()
                    except (discord.Forbidden, discord.NotFound):
                        pass
        except discord.Forbidden:
            pass

        if not all_rooms:
            embed = discord.Embed(
                title="🎮 Открытые комнаты",
                description=(
                    "Сейчас нет открытых игр.\n\n"
                    "Создай свою:\n"
                    "`!create 4 random` · `!create 4 team` · `!create 4 cap`"
                ),
                color=0x2C2F33,
            )
            embed.set_footer(text="Обновляется автоматически")
            await lobby.send(embed=embed)
            return

        header = discord.Embed(
            title="🎮 Открытые комнаты",
            description=(
                "Нажми `!q <размер> <режим>` чтобы войти в игру.\n"
                "Пример: `!q 4 random` · `!q 3 cap` · `!q 2 team`"
            ),
            color=0x5865F2,
        )
        header.set_footer(text="Обновляется автоматически при изменениях")
        await lobby.send(embed=header)

        mode_labels = {
            "team": ("👥 Командный", 0x5865F2),
            "random": ("🎲 Рандомный", 0x9B59B6),
            "cap": ("🎯 Капитанский пик", 0xE67E22),
        }

        for room in all_rooms:
            players = await db.get_room_players(room["room_id"])
            total_slots = room["size"] * 2
            filled = len(players)
            free = total_slots - filled

            mode_label, color = mode_labels.get(room["mode"], ("❓", 0x99AAB5))
            status_line = f"{'🟢' if free > 0 else '🔴'} {filled}/{total_slots} игроков"

            embed = discord.Embed(
                title=f"Комната #{room['room_id']}  ·  {room['size']}v{room['size']}  ·  {mode_label}",
                color=color,
            )

            if players:
                lines = []
                for p in players:
                    rank, _ = get_rank(p["elo"])
                    lines.append(f"• **{p['username']}** — {p['elo']} ELO ({rank})")
                embed.add_field(name="Игроки", value="\n".join(lines), inline=True)

            if free > 0:
                embed.add_field(
                    name="Свободно",
                    value="\n".join(["• *Место свободно*"] * free),
                    inline=True,
                )

            embed.set_footer(text=f"{status_line}  ·  !q {room['size']} {room['mode']}")
            view = JoinRoomView(
                room_id=room["room_id"],
                size=room["size"],
                mode=room["mode"],
                is_full=(free == 0),
            )
            await lobby.send(embed=embed, view=view)

    # ── _create_room ──────────────────────────────────────────────

    async def _create_room(self, ctx: commands.Context, size: int, mode: str = "team"):
        """Общая логика создания комнаты."""
        if not self._is_guild(ctx):
            return

        if size not in (1, 2, 3, 4):
            await ctx.send("Укажи размер: `1`, `2`, `3` или `4`.")
            return

        if mode not in ("team", "random", "cap"):
            await ctx.send("Укажи режим: `team`, `random` или `cap`.")
            return

        db = self.bot.db
        player = await db.get_player(ctx.author.id)
        if not player:
            await ctx.send("Сначала зарегистрируйся командой `!register`.")
            return

        if await db.get_player_room(ctx.author.id):
            await ctx.send("Ты уже находишься в комнате. Сначала выйди (`!exit`).")
            return

        guild = ctx.guild
        category = await self._get_or_create_category(guild)

        overwrite = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
        }
        mod_role = discord.utils.get(guild.roles, name=Config.MODERATOR_ROLE_NAME)
        if mod_role:
            overwrite[mod_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            )

        room_id = await db.create_room(0, size, ctx.author.id, mode=mode)

        channel = await guild.create_text_channel(
            f"room-{room_id}",
            category=category,
            overwrites=overwrite,
        )

        await db.update_channel_id(room_id, channel.id)

        # В режиме cap и random — создатель пока без команды (team=0),
        # в режиме team — создатель в team=1, без капитанства (в team-режиме капитанов нет)
        if mode == "team":
            await db.add_to_room(room_id, ctx.author.id, team=1, is_captain=False)
        else:
            await db.add_to_room(room_id, ctx.author.id, team=0, is_captain=False)

        await self._allow_channel(channel, ctx.author)

        players = await db.get_room_players(room_id)
        embed = room_embed(room_id, size, players, mode=mode)
        view = RoomView(self.bot, room_id, room_status="waiting", room_mode=mode, room_size=size)
        msg = await channel.send(embed=embed, view=view)
        await msg.pin()
        await db.update_embed_id(room_id, msg.id)

        mode_labels = {
            "team": "👥 Team / Командный",
            "random": "🎲 Random / Рандомный",
            "cap": "🎯 Captain / Капитанский",
        }
        await ctx.send(
            f"🎮 {ctx.author.mention} создал комнату "
            f"**#{room_id}** · **{size}v{size}** · {mode_labels[mode]} → {channel.mention}"
        )
        await self._refresh_lobby()

    # ── commands: create ─────────────────────────────────────────

    @commands.command(name="create", aliases=["create1", "create2", "create3", "create4"])
    async def create(self, ctx: commands.Context, size: Optional[str] = None, mode: Optional[str] = None):
        """
        Создать комнату.
        !create           — показать меню выбора размера и режима
        !create 4 team    — командный режим (игроки выбирают команду сами)
        !create 4 random  — рандомный режим (бот раскидывает по командам)
        !create 4 cap     — капитанский пик (капитаны выбирают игроков)
        По умолчанию режим: team, размер: 4
        """
        if not self._is_guild(ctx):
            return

        # Проверяем регистрацию и занятость сразу
        db = self.bot.db
        player = await db.get_player(ctx.author.id)
        if not player:
            await ctx.send("❌ Сначала зарегистрируйся командой `!register <ник>`.")
            return
        if await db.get_player_room(ctx.author.id):
            await ctx.send("❌ Ты уже в комнате. Выйди через кнопку **🚪 Покинуть** или `!exit`.")
            return

        resolved_size = 0
        alias = ctx.invoked_with or ""
        if alias and alias[-1].isdigit():
            resolved_size = int(alias[-1])
        elif size is not None:
            if size.isdigit():
                resolved_size = int(size)
            elif size in ("team", "random", "cap"):
                mode = size
                resolved_size = 4
            else:
                await ctx.send("Использование: `!create [1/2/3/4] [team/random/cap]`")
                return

        resolved_mode = mode if mode in ("team", "random", "cap") else None

        # Если размер И режим указаны — создаём сразу
        if resolved_size != 0 and resolved_mode is not None:
            await self._create_room(ctx, resolved_size, resolved_mode)
            return

        # Если указан только размер — показываем кнопки с фильтрацией по размеру
        # Если ничего не указано — показываем все кнопки
        embed = discord.Embed(
            title="🎮 Создать комнату",
            color=0x5865F2,
        )

        if resolved_size != 0:
            embed.description = (
                f"🇷🇺 Выбери **режим** для комнаты **{resolved_size}v{resolved_size}**:\n"
                f"🇬🇧 Choose a **mode** for the **{resolved_size}v{resolved_size}** room:\n\n"
                "👥 **Team / Команда** — 🇷🇺 игроки выбирают команду сами · 🇬🇧 players pick their team\n"
                "🎲 **Random / Рандом** — 🇷🇺 бот распределяет случайно · 🇬🇧 bot assigns randomly\n"
                "🎯 **Captain / Капитан** — 🇷🇺 капитаны пикают игроков · 🇬🇧 captains pick players"
            )
            view = discord.ui.View(timeout=None)
            for mode_key in ("team", "random", "cap"):
                view.add_item(CreateRoomButton(resolved_size, mode_key, row=0))
        else:
            embed.description = (
                "🇷🇺 Выбери **размер** и **режим** комнаты:\n"
                "🇬🇧 Choose room **size** and **mode**:\n\n"
                "👥 **Team / Команда** — 🇷🇺 игроки выбирают команду сами · 🇬🇧 players pick their team\n"
                "🎲 **Random / Рандом** — 🇷🇺 бот распределяет случайно · 🇬🇧 bot assigns randomly\n"
                "🎯 **Captain / Капитан** — 🇷🇺 капитаны пикают игроков · 🇬🇧 captains pick players\n\n"
                "🇷🇺 Или быстрые команды · 🇬🇧 Or quick commands:\n"
                "`!create 4 team` · `!create 4 random` · `!create 4 cap`"
            )
            view = CreateRoomView()

        embed.set_footer(text="🇷🇺 Панель закроется через 60 сек · 🇬🇧 Panel closes in 60 sec")
        await ctx.send(embed=embed, view=view)

    # ── commands: queue ──────────────────────────────────────────

    @commands.command(name="queue", aliases=["q"])
    async def queue(self, ctx: commands.Context, size: Optional[str] = None, mode: Optional[str] = None):
        """
        Войти в очередь.
        !q 4 team   — ищет командную комнату на 4v4
        !q 4 random — ищет рандомную комнату на 4v4
        !q 4 cap    — ищет комнату капитанского пика на 4v4
        """
        if not self._is_guild(ctx):
            return

        db = self.bot.db
        player = await db.get_player(ctx.author.id)
        if not player:
            await ctx.send("Сначала зарегистрируйся командой `!register`.")
            return

        if await db.get_player_room(ctx.author.id):
            await ctx.send("Ты уже в комнате. Выйди из неё (`!exit`) перед поиском.")
            return

        # Разбираем аргументы гибко: !q 4, !q random, !q 4 random
        resolved_size = 0
        resolved_mode = None

        if size is not None:
            if size.isdigit():
                resolved_size = int(size)
            elif size in ("team", "random", "cap"):
                resolved_mode = size
            else:
                resolved_size = 0

        if mode in ("team", "random", "cap"):
            resolved_mode = mode

        if resolved_size not in (1, 2, 3, 4) or resolved_mode is None:
            embed = discord.Embed(
                title="🎮 Выбери режим и размер",
                description=(
                    "**Рандомный** (бот раскидывает по командам):\n"
                    "`!q 1 random` / `!q 2 random` / `!q 3 random` / `!q 4 random`\n\n"
                    "**Командный** (выбираешь команду сам через `!pick`):\n"
                    "`!q 1 team` / `!q 2 team` / `!q 3 team` / `!q 4 team`\n\n"
                    "**Капитанский** (капитаны пикают игроков):\n"
                    "`!q 1 cap` / `!q 2 cap` / `!q 3 cap` / `!q 4 cap`\n\n"
                    "Сокращение: `!q 4 random`"
                ),
                color=0x5865F2,
            )
            await ctx.send(embed=embed)
            return

        rooms = await db.get_available_rooms(size=resolved_size, mode=resolved_mode)

        if not rooms:
            await ctx.send(
                f"Нет доступных комнат ({resolved_size}v{resolved_size} · {resolved_mode}). "
                f"Создай свою: `!create {resolved_size} {resolved_mode}`"
            )
            return

        # Выбираем комнату с наиболее близким средним ELO
        best_room = None
        best_diff = float("inf")

        for room in rooms:
            rp = await db.get_room_players(room["room_id"])
            total_slots = room["size"] * 2
            if len(rp) >= total_slots:
                continue
            if rp:
                avg = sum(p["elo"] for p in rp) / len(rp)
                diff = abs(avg - player["elo"])
            else:
                diff = 0
            if diff < best_diff:
                best_diff = diff
                best_room = room

        if not best_room:
            await ctx.send("Нет подходящих комнат.")
            return

        await self._join_room(ctx, best_room, player)

    async def _join_room(self, ctx, room, player):
        db = self.bot.db
        room_id = room["room_id"]
        size = room["size"]
        mode = room["mode"]

        players = await db.get_room_players(room_id)
        total_slots = size * 2

        if len(players) >= total_slots:
            await ctx.send("Комната уже заполнена.")
            return

        # Для random и cap — игрок пока без команды (team=0)
        # Для team — первый заполняет команду 1, потом 2
        if mode in ("random", "cap"):
            team = 0
        else:
            team1 = [p for p in players if p["team"] == 1]
            team2 = [p for p in players if p["team"] == 2]
            if len(team1) < size:
                team = 1
            elif len(team2) < size:
                team = 2
            else:
                await ctx.send("Комната уже заполнена.")
                return

        await db.add_to_room(room_id, ctx.author.id, team=team)

        # В team-режиме капитанов нет

        guild = ctx.guild
        channel = guild.get_channel(room["channel_id"])
        if channel:
            await self._allow_channel(channel, ctx.author)
            if mode == "team":
                await channel.send(f"👋 {ctx.author.mention} вошёл в комнату (Команда {team})")
            else:
                await channel.send(f"👋 {ctx.author.mention} вошёл в комнату")

        players = await db.get_room_players(room_id)

        if len(players) == total_slots:
            # Комната полная — запускаем соответствующую логику
            if mode == "random":
                await self._randomize_teams(room_id, players, size, channel)
            elif mode == "cap":
                await self._start_captain_pick(room_id, players, size, channel)
            elif mode == "team":
                await db.update_room_status(room_id, "full")
                if channel:
                    await channel.send("🎯 Комната заполнена! Нажмите **▶ Start** чтобы начать!")

        await self._refresh_room_embed(room_id)
        if channel:
            await ctx.send(f"✅ Ты добавлен в комнату **#{room_id}**! Иди в {channel.mention}")
        else:
            await ctx.send(f"✅ Ты добавлен в комнату **#{room_id}**!")
        await self._refresh_lobby()

    # ── Random mode ──────────────────────────────────────────────

    async def _randomize_teams(self, room_id: int, players: list, size: int, channel):
        """Раскидывает всех игроков по командам случайно и назначает капитанов."""
        db = self.bot.db

        shuffled = list(players)
        random.shuffle(shuffled)

        team1 = shuffled[:size]
        team2 = shuffled[size:]

        for p in team1:
            await db.set_player_team(room_id, p["discord_id"], 1)
        for p in team2:
            await db.set_player_team(room_id, p["discord_id"], 2)

        # Назначаем капитанов — рандомно из каждой команды
        cap1 = random.choice(team1)
        cap2 = random.choice(team2)
        await db.set_captain(room_id, cap1["discord_id"], True)
        await db.set_captain(room_id, cap2["discord_id"], True)

        await db.update_room_status(room_id, "full")
        await self._refresh_lobby()

        if channel:
            t1_mentions = " ".join(f"<@{p['discord_id']}>" for p in team1)
            t2_mentions = " ".join(f"<@{p['discord_id']}>" for p in team2)
            embed = discord.Embed(
                title="🎲 Команды сформированы рандомно!",
                color=0x9B59B6,
            )
            embed.add_field(name=f"🔵 Команда 1 (капитан: <@{cap1['discord_id']}>)", value=t1_mentions, inline=False)
            embed.add_field(name=f"🔴 Команда 2 (капитан: <@{cap2['discord_id']}>)", value=t2_mentions, inline=False)
            await channel.send(embed=embed)
            await self._announce_strong_side(channel, room_id)
            await channel.send("✅ Капитаны, нажмите **▶ Start** чтобы начать!")

    # ── Cap mode ─────────────────────────────────────────────────

    async def _start_captain_pick(self, room_id: int, players: list, size: int, channel):
        """Комната заполнена в cap-режиме — предлагаем выбрать способ назначения капитанов."""
        db = self.bot.db
        await db.update_room_status(room_id, "full")
        await self._refresh_lobby()

        if channel:
            mentions = " ".join(f"<@{p['discord_id']}>" for p in players)
            embed = discord.Embed(
                title="🎯 Комната заполнена! Выберите способ назначения капитанов",
                description=(
                    f"{mentions}\n\n"
                    "**`!random`** — 🎲 Бот выбирает двух капитанов случайно\n"
                    "**`!cap`** — 👑 Два игрока назначают себя капитанами сами\n\n"
                    "После того как оба капитана выбраны, напишите **`!start`** чтобы начать пик.\n"
                    "Любой игрок может написать `!uncap` чтобы снять с себя роль капитана (до начала пика)."
                ),
                color=0xE67E22,
            )
            await channel.send(embed=embed)

    async def _do_random_captains(self, room_id: int, channel):
        """Выбирает двух капитанов рандомно и запускает пик."""
        db = self.bot.db
        players = await db.get_room_players(room_id)
        room = await db.get_room(room_id)
        if not room or room["status"] not in ("full", "waiting"):
            return

        # Сбрасываем всех текущих капитанов и команды перед переназначением
        for p in players:
            if p["is_captain"]:
                await db.set_captain(room_id, p["discord_id"], False)
            # Сбрасываем команды у всех (кроме уже назначенных капитанами)
            await db.set_player_team(room_id, p["discord_id"], 0)

        # Перечитываем после сброса
        players = await db.get_room_players(room_id)
        all_players = list(players)
        random.shuffle(all_players)
        cap1 = all_players[0]
        cap2 = all_players[1]

        await db.set_player_team(room_id, cap1["discord_id"], 1)
        await db.set_captain(room_id, cap1["discord_id"], True)
        await db.set_player_team(room_id, cap2["discord_id"], 2)
        await db.set_captain(room_id, cap2["discord_id"], True)

        first_pick_team = random.choice([1, 2])
        await db.set_pick_turn(room_id, first_pick_team)
        # Сильная сторона — тот кто пикует ВТОРЫМ
        second_pick_team_r = 2 if first_pick_team == 1 else 1
        await db.set_strong_side(room_id, second_pick_team_r)
        await db.update_room_status(room_id, "picking")
        await self._refresh_lobby()

        if channel:
            first_cap = cap1 if first_pick_team == 1 else cap2
            second_cap = cap2 if first_pick_team == 1 else cap1
            strong_side = "🔵 Команда 1" if second_pick_team_r == 1 else "🔴 Команда 2"
            embed = discord.Embed(
                title="🎯 Капитанский пик начался!",
                description=(
                    f"👑 Капитан команды 1: <@{cap1['discord_id']}>\n"
                    f"👑 Капитан команды 2: <@{cap2['discord_id']}>\n\n"
                    f"**Первым пикует: <@{first_cap['discord_id']}>** (Команда {first_pick_team})\n\n"
                    f"⚔️ **Сильная сторона: {strong_side}** (пикует вторым — <@{second_cap['discord_id']}>)"
                ),
                color=0xE67E22,
            )
            await channel.send(embed=embed)
            await self._send_pick_message(room_id, channel, first_pick_team)

    async def _send_pick_message(self, room_id: int, channel, picking_team: int):
        """Отправляет сообщение с кнопками для пика."""
        db = self.bot.db
        players = await db.get_room_players(room_id)
        unpicked = [p for p in players if p["team"] == 0]

        if not unpicked:
            # Пик завершён
            await self._finalize_cap_pick(room_id, channel)
            return

        room = await db.get_room(room_id)
        cap = next((p for p in players if p["team"] == picking_team and p["is_captain"]), None)

        embed = discord.Embed(
            title=f"🎯 Пик — Команда {picking_team}",
            description=f"<@{cap['discord_id']}>, выбери игрока для своей команды:",
            color=0x3498DB if picking_team == 1 else 0xE74C3C,
        )
        view = PickView(self.bot, room_id, unpicked)
        await channel.send(embed=embed, view=view)

    async def _handle_pick(self, interaction: discord.Interaction, target_id: int):
        """Обработчик нажатия кнопки пика."""
        db = self.bot.db
        room = await db.get_player_room(interaction.user.id)
        if not room or room["mode"] != "cap":
            await interaction.response.send_message("Ошибка: ты не в режиме капитанского пика.", ephemeral=True)
            return

        room_id = room["room_id"]
        players = await db.get_room_players(room_id)
        me = next((p for p in players if p["discord_id"] == interaction.user.id), None)

        if not me or not me["is_captain"]:
            await interaction.response.send_message("Только капитан может пикать игроков.", ephemeral=True)
            return

        if room["pick_turn"] != me["team"]:
            await interaction.response.send_message("Сейчас не твой ход пика.", ephemeral=True)
            return

        target = next((p for p in players if p["discord_id"] == target_id), None)
        if not target or target["team"] != 0:
            await interaction.response.send_message("Этот игрок уже в команде или не найден.", ephemeral=True)
            return

        # Добавляем игрока в команду капитана
        await db.set_player_team(room_id, target_id, me["team"])

        # Проверяем сколько ещё нужно пикнуть
        size = room["size"]
        players = await db.get_room_players(room_id)
        unpicked = [p for p in players if p["team"] == 0]

        await interaction.response.send_message(
            f"✅ <@{target_id}> добавлен в Команду {me['team']}!", ephemeral=False
        )

        # Передаём ход другому капитану
        next_team = 2 if me["team"] == 1 else 1
        await db.set_pick_turn(room_id, next_team)

        await self._refresh_room_embed(room_id)

        channel = interaction.channel
        if unpicked:
            await self._send_pick_message(room_id, channel, next_team)
        else:
            await self._finalize_cap_pick(room_id, channel)

    async def _finalize_cap_pick(self, room_id: int, channel):
        """Завершает пик и запускает комнату."""
        db = self.bot.db
        await db.update_room_status(room_id, "full")
        await self._refresh_lobby()
        players = await db.get_room_players(room_id)
        room = await db.get_room(room_id)

        if channel:
            t1 = [p for p in players if p["team"] == 1]
            t2 = [p for p in players if p["team"] == 2]
            t1_mentions = " ".join(f"<@{p['discord_id']}>" for p in t1)
            t2_mentions = " ".join(f"<@{p['discord_id']}>" for p in t2)
            embed = discord.Embed(
                title="✅ Пик завершён! Команды сформированы.",
                color=0x57F287,
            )
            embed.add_field(name="🔵 Команда 1", value=t1_mentions, inline=False)
            embed.add_field(name="🔴 Команда 2", value=t2_mentions, inline=False)
            await channel.send(embed=embed)
            await channel.send("✅ Капитаны, нажмите **▶ Start** чтобы начать!")

        await self._refresh_room_embed(room_id)


    # ── Cap mode: !random / !cap / !uncap ──────────────────────────

    @commands.command(name="random")
    async def random_captains(self, ctx: commands.Context):
        """
        [Только режим cap] Бот случайно выбирает двух капитанов и запускает пик.
        Доступно только когда комната полная и пик ещё не начат.
        """
        if not self._is_guild(ctx):
            return

        db = self.bot.db
        room = await db.get_player_room(ctx.author.id)
        if not room:
            await ctx.send("❌ Ты не в комнате.")
            return

        if room["mode"] != "cap":
            await ctx.send("❌ Команда `!random` доступна только в режиме **капитанского пика**.")
            return

        if room["status"] in ("started", "awaiting_screenshot"):
            await ctx.send("❌ Игра уже идёт.")
            return

        if room["status"] not in ("waiting", "full", "picking"):
            await ctx.send("❌ Комната не в нужном состоянии.")
            return

        players = await db.get_room_players(room["room_id"])
        total_slots = room["size"] * 2
        if len(players) < total_slots:
            await ctx.send(f"❌ Комната ещё не заполнена ({len(players)}/{total_slots}).")
            return

        # Если пик уже шёл — сбрасываем обратно в full и переназначаем
        if room["status"] == "picking":
            await db.update_room_status(room["room_id"], "full")

        guild = ctx.guild
        channel = guild.get_channel(room["channel_id"])
        await ctx.message.delete(delay=2)
        await self._do_random_captains(room["room_id"], channel)
        await self._refresh_room_embed(room["room_id"])

    @commands.command(name="cap")
    async def become_captain(self, ctx: commands.Context, member: discord.Member = None):
        """
        [Режим cap] Стать капитаном (без аргумента) или назначить капитана (мод + @игрок).
        Максимум 2 капитана. Доступно пока пик не начался.
        """
        if not self._is_guild(ctx):
            return

        db = self.bot.db
        is_mod = self._is_mod(ctx.author)

        # Если передан аргумент @игрок — это модераторская форма
        if member is not None:
            if not is_mod:
                await ctx.send("❌ Только модераторы могут назначать капитана другому игроку.")
                return
            # Ищем комнату цели
            room = await db.get_player_room(member.id)
            if not room:
                await ctx.send("❌ Этот игрок не в комнате.")
                return
            target_id = member.id
            target_mention = member.mention
        else:
            # Обычный игрок назначает себя
            room = await db.get_player_room(ctx.author.id)
            if not room:
                await ctx.send("❌ Ты не в комнате.")
                return
            target_id = ctx.author.id
            target_mention = ctx.author.mention
            member = ctx.author

        if room["mode"] != "cap":
            await ctx.send("❌ Команда `!cap` доступна только в режиме **капитанского пика**.")
            return

        # Модератор может назначать капитана в любой статус кроме started
        # Обычный игрок — только до picking
        if room["status"] == "started":
            await ctx.send("❌ Игра уже идёт — менять капитана нельзя.")
            return

        # При статусе picking — сбрасываем обратно в full чтобы переназначить
        if room["status"] == "picking":
            players_tmp = await db.get_room_players(room["room_id"])
            for p in players_tmp:
                if p["is_captain"]:
                    await db.set_captain(room["room_id"], p["discord_id"], False)
                await db.set_player_team(room["room_id"], p["discord_id"], 0)
            await db.update_room_status(room["room_id"], "full")

        # ── Атомарная секция: защита от гонки (два !cap одновременно) ──
        lock = self._finalize_locks.setdefault(room["room_id"], asyncio.Lock())
        async with lock:
            players = await db.get_room_players(room["room_id"])
            target = next((p for p in players if p["discord_id"] == target_id), None)
            if not target:
                await ctx.send("❌ Игрок не найден в комнате.")
                return

            if target["is_captain"]:
                await ctx.send(f"⚠️ {member.display_name} уже капитан. Сначала `!uncap @{member.display_name}`.")
                return

            # Строгая проверка: не больше 2 капитанов суммарно
            current_caps = [p for p in players if p["is_captain"]]
            if len(current_caps) >= 2:
                cap_names = " и ".join(f"**{p['username']}**" for p in current_caps)
                await ctx.send(
                    f"❌ Уже 2 капитана: {cap_names}.\n"
                    f"Сначала убери одного через `!uncap @капитан`."
                )
                return

            taken_teams = {p["team"] for p in current_caps}
            new_team = 1 if 1 not in taken_teams else 2

            await db.set_player_team(room["room_id"], target_id, new_team)
            await db.set_captain(room["room_id"], target_id, True)

        # ── Вне lock: уведомления ──
        guild = ctx.guild
        channel = guild.get_channel(room["channel_id"])
        players = await db.get_room_players(room["room_id"])
        caps = [p for p in players if p["is_captain"]]
        total_slots = room["size"] * 2

        if channel:
            prefix = "🔨 [Мод] " if is_mod and target_id != ctx.author.id else ""
            await channel.send(f"{prefix}👑 {target_mention} стал капитаном **Команды {new_team}**!")
            if len(caps) == 2 and len(players) == total_slots:
                await channel.send(
                    "✅ Оба капитана выбраны и комната полная!\n"
                    "Напишите **`!start`** чтобы начать пик игроков."
                )

        await self._refresh_room_embed(room["room_id"])
        try:
            await ctx.message.delete(delay=2)
        except (discord.Forbidden, discord.NotFound):
            pass

    @commands.command(name="uncap")
    async def remove_captain(self, ctx: commands.Context, member: discord.Member = None):
        """
        [Режим cap] Снять роль капитана с себя (без аргумента) или с другого игрока (мод + @игрок).
        До начала пика (waiting / full). Модератор может в любой статус.
        """
        if not self._is_guild(ctx):
            return

        db = self.bot.db
        is_mod = self._is_mod(ctx.author)

        if member is not None:
            if not is_mod:
                await ctx.send("❌ Только модераторы могут снимать капитана с другого игрока.")
                return
            room = await db.get_player_room(member.id)
            if not room:
                await ctx.send("❌ Этот игрок не в комнате.")
                return
            target_id = member.id
            target_mention = member.mention
        else:
            room = await db.get_player_room(ctx.author.id)
            if not room:
                await ctx.send("❌ Ты не в комнате.")
                return
            target_id = ctx.author.id
            target_mention = ctx.author.mention
            member = ctx.author

        if room["mode"] != "cap":
            await ctx.send("❌ Команда `!uncap` доступна только в режиме **капитанского пика**.")
            return

        # Обычный игрок не может uncap себя во время пика/игры
        if not is_mod and room["status"] in ("picking", "started"):
            await ctx.send("❌ Пик уже начался — снять капитана нельзя.")
            return

        players = await db.get_room_players(room["room_id"])
        target = next((p for p in players if p["discord_id"] == target_id), None)
        if not target:
            await ctx.send("❌ Игрок не найден в комнате.")
            return

        if not target["is_captain"]:
            await ctx.send(f"⚠️ {member.display_name} не является капитаном.")
            return

        await db.set_captain(room["room_id"], target_id, False)
        await db.set_player_team(room["room_id"], target_id, 0)

        # Если пик шёл — сбрасываем статус в full
        if room["status"] == "picking":
            await db.update_room_status(room["room_id"], "full")

        guild = ctx.guild
        channel = guild.get_channel(room["channel_id"])
        if channel:
            prefix = "🔨 [Мод] " if is_mod and target_id != ctx.author.id else ""
            await channel.send(
                f"{prefix}🔓 {target_mention} снят с роли капитана.\n"
                "Используй `!cap` чтобы назначить нового, или `!random` чтобы бот выбрал."
            )

        await self._refresh_room_embed(room["room_id"])
        try:
            await ctx.message.delete(delay=2)
        except (discord.Forbidden, discord.NotFound):
            pass

    # ── Team mode: !pick ─────────────────────────────────────────

    @commands.command(name="pick")
    async def pick_team(self, ctx: commands.Context, team: int):
        """
        [Только режим team] Выбрать команду: !pick 1 или !pick 2
        """
        if not self._is_guild(ctx):
            return

        if team not in (1, 2):
            await ctx.send("Укажи команду: `!pick 1` или `!pick 2`")
            return

        db = self.bot.db
        room = await db.get_player_room(ctx.author.id)
        if not room:
            await ctx.send("Ты не в комнате.")
            return

        if room["mode"] != "team":
            await ctx.send("Команда `!pick` доступна только в командном режиме (`team`).")
            return

        if room["status"] == "started":
            await ctx.send("Игра уже началась.")
            return

        players = await db.get_room_players(room["room_id"])
        me = next((p for p in players if p["discord_id"] == ctx.author.id), None)

        if not me:
            await ctx.send("Ты не найден в комнате.")
            return

        size = room["size"]

        # Нельзя встать в ту же команду
        if me["team"] == team:
            await ctx.send(f"Ты уже в Команде {team}.")
            return

        team_players = [p for p in players if p["team"] == team]
        if len(team_players) >= size:
            await ctx.send(f"Команда {team} уже заполнена ({size}/{size}). Сначала кто-то должен перейти из неё.")
            return

        await db.set_player_team(room["room_id"], ctx.author.id, team)

        guild = ctx.guild
        channel = guild.get_channel(room["channel_id"])
        if channel:
            await channel.send(f"🔀 {ctx.author.mention} перешёл в Команду {team}.")

        # Проверяем полноту
        players = await db.get_room_players(room["room_id"])
        t1 = [p for p in players if p["team"] == 1]
        t2 = [p for p in players if p["team"] == 2]

        if len(t1) == size and len(t2) == size:
            await db.update_room_status(room["room_id"], "full")
            if channel:
                await channel.send("🎯 Команды сформированы! Нажмите **▶ Start** чтобы начать!")
                await self._announce_strong_side(channel, room["room_id"])
        else:
            if room["status"] == "full":
                await db.update_room_status(room["room_id"], "waiting")

        await self._refresh_room_embed(room["room_id"])
        await ctx.send(f"✅ Ты выбрал Команду {team}.")

    # ── exit ─────────────────────────────────────────────────────

    @commands.command(name="exit")
    async def exit_room(self, ctx: commands.Context):
        if not self._is_guild(ctx):
            return

        db = self.bot.db
        room = await db.get_player_room(ctx.author.id)
        if not room:
            await ctx.send("Ты не находишься ни в одной комнате.")
            return

        players = await db.get_room_players(room["room_id"])
        me = next((p for p in players if p["discord_id"] == ctx.author.id), None)
        was_captain = bool(me and me["is_captain"])
        my_team = me["team"] if me else None
        game_was_started = room["status"] == "started"

        await db.remove_from_room(room["room_id"], ctx.author.id)

        guild = ctx.guild
        channel = guild.get_channel(room["channel_id"])
        if channel:
            await self._deny_channel(channel, ctx.author)

        if game_was_started:
            new_elo = await db.deduct_elo_for_leave(ctx.author.id, 15)
            if channel:
                await channel.send(
                    f"⚠️ {ctx.author.mention} покинул игру и получает штраф **-15 ELO** "
                    f"(теперь {new_elo} ELO). Игра отменена, комната возобновляет набор."
                )
            from cogs.register import Register
            reg_cog: Register = self.bot.cogs.get("Register")  # type: ignore
            if reg_cog:
                await reg_cog._sync_rank_role(ctx.author, new_elo)
        else:
            if channel:
                await channel.send(f"👋 {ctx.author.mention} покинул комнату.")

        players = await db.get_room_players(room["room_id"])

        if not players:
            await db.delete_room(room["room_id"])
            if channel:
                await channel.delete(reason="Комната пуста")
            await self._refresh_lobby()
            return

        if was_captain and my_team:
            team_left = [p for p in players if p["team"] == my_team]
            if team_left:
                new_cap = max(team_left, key=lambda p: p["elo"])
                await db.set_captain(room["room_id"], new_cap["discord_id"], True)
                if channel:
                    new_cap_member = guild.get_member(new_cap["discord_id"])
                    if new_cap_member:
                        await channel.send(
                            f"👑 {new_cap_member.mention} назначен новым капитаном команды {my_team}."
                        )

        if room["status"] in ("full", "picking", "started"):
            await db.update_room_status(room["room_id"], "waiting")
            await db.set_ready(room["room_id"], 1, False)
            await db.set_ready(room["room_id"], 2, False)
            for p in players:
                await db.set_end_vote(room["room_id"], p["discord_id"], None)

        await self._refresh_room_embed(room["room_id"])
        await ctx.send(
            "✅ Ты покинул комнату." + (" **-15 ELO** за выход из активной игры." if game_was_started else "")
        )
        await self._refresh_lobby()

    # ── kick ─────────────────────────────────────────────────────

    @commands.command(name="kick")
    async def kick(self, ctx: commands.Context, member: discord.Member = None):
        """
        Капитан: кикнуть игрока своей команды (до старта).
        Модератор: кикнуть любого игрока в любое время, включая во время игры.
        Команду можно писать прямо в канале комнаты.
        """
        if not self._is_guild(ctx):
            return

        if member is None:
            await ctx.send("Укажи игрока: `!kick @игрок`")
            return

        db = self.bot.db
        is_mod = self._is_mod(ctx.author)

        # Модератор может писать команду откуда угодно — ищем комнату по цели.
        # Обычный капитан ищет свою комнату по себе.
        if is_mod:
            room = await db.get_player_room(member.id)
            if not room:
                await ctx.send("❌ Этот игрок не в комнате.")
                return
        else:
            room = await db.get_player_room(ctx.author.id)
            if not room:
                await ctx.send("❌ Ты не в комнате.")
                return

            if room["status"] == "started":
                await ctx.send("❌ Игра уже началась. Модератор может кикнуть командой `!kick @игрок` из канала комнаты.")
                return

        players = await db.get_room_players(room["room_id"])
        target = next((p for p in players if p["discord_id"] == member.id), None)

        if not target:
            await ctx.send("❌ Игрок не найден в комнате.")
            return

        if not is_mod:
            me = next((p for p in players if p["discord_id"] == ctx.author.id), None)
            if not me or not me["is_captain"]:
                await ctx.send("❌ Только капитан может кикать.")
                return
            if target["team"] != me["team"] and target["team"] != 0:
                await ctx.send("❌ Ты можешь кикать только игроков своей команды.")
                return
            if target["is_captain"]:
                await ctx.send("❌ Нельзя кикнуть другого капитана.")
                return

        was_cap = bool(target["is_captain"])
        target_team = target["team"]
        game_was_started = room["status"] == "started"

        await db.remove_from_room(room["room_id"], member.id)

        guild = ctx.guild
        channel = guild.get_channel(room["channel_id"])
        if channel:
            await self._deny_channel(channel, member)
            prefix = "🔨 [Мод]" if is_mod else "🦵"
            await channel.send(f"{prefix} {member.mention} кикнут из комнаты.")

        players = await db.get_room_players(room["room_id"])

        if not players:
            if channel:
                await channel.delete(reason="Комната пуста после кика")
            await db.delete_room(room["room_id"])
            await ctx.send(f"✅ {member.display_name} кикнут. Комната удалена (все ушли).")
            await self._refresh_lobby()
            return

        # Если кикнули капитана — снимаем флаг (авто-переназначение не делаем,
        # чтобы мод мог вручную поставить нового через !cap)
        if was_cap:
            # Флаг уже сброшен через remove_from_room; если нет — сбрасываем явно
            pass

        # Сброс статуса
        if game_was_started:
            # Останавливаем игру, комната уходит в waiting
            await db.update_room_status(room["room_id"], "waiting")
            await db.set_ready(room["room_id"], 1, False)
            await db.set_ready(room["room_id"], 2, False)
            for p in players:
                await db.set_end_vote(room["room_id"], p["discord_id"], None)
            if channel:
                await channel.send(
                    "⏸️ Игра приостановлена из-за кика игрока. "
                    "Ожидается замена или используйте `!mod_end` чтобы расформировать."
                )
        elif room["status"] in ("full", "picking"):
            await db.update_room_status(room["room_id"], "waiting")
            await db.set_ready(room["room_id"], 1, False)
            await db.set_ready(room["room_id"], 2, False)

        await self._refresh_room_embed(room["room_id"])
        await ctx.send(f"✅ {member.display_name} кикнут.")
        await self._refresh_lobby()

    # ── start ─────────────────────────────────────────────────────

    @commands.command(name="start")
    async def start(self, ctx: commands.Context):
        if not self._is_guild(ctx):
            return

        room = await self.bot.db.get_player_room(ctx.author.id)
        if not room:
            await ctx.send("Ты не в комнате.")
            return

        # Проверяем что команда написана в канале комнаты
        if room["channel_id"] != ctx.channel.id:
            guild = ctx.guild
            room_channel = guild.get_channel(room["channel_id"]) if guild else None
            if room_channel:
                await ctx.send(
                    f"❌ Команду `!start` нужно писать в канале твоей комнаты: {room_channel.mention}",
                    delete_after=10,
                )
            return

        await self._do_start(ctx.author, ctx.channel, room["room_id"])

    async def _do_start(
            self,
            user: discord.User | discord.Member,
            channel,
            room_id: int,
    ):
        try:
            db = self.bot.db
            room = await db.get_room(room_id)
            if not room:
                return
    
            # Всегда отправляем сообщения об ошибках в канал комнаты
            guild = self.bot.get_guild(Config.GUILD_ID)
            room_channel = guild.get_channel(room["channel_id"]) if guild else channel
            reply_channel = room_channel if room_channel else channel
    
            if room["status"] == "started":
                if hasattr(reply_channel, "send"):
                    await reply_channel.send("Игра уже идёт.")
                return
    
            if room["status"] == "picking":
                if hasattr(reply_channel, "send"):
                    await reply_channel.send("Пик ещё не завершён.")
                return
    
            if room["status"] not in ("waiting", "full"):
                return
    
            players = await db.get_room_players(room_id)
            size = room["size"]
    
            me = next((p for p in players if p["discord_id"] == user.id), None)
            if not me:
                if hasattr(reply_channel, "send"):
                    await reply_channel.send("Ты не в этой комнате.")
                return
    
            # ── cap-режим: !start запускает пик, а после пика — игру ─────
            if room["mode"] == "cap":
                # Только капитан может запустить
                if not me["is_captain"]:
                    if hasattr(reply_channel, "send"):
                        await reply_channel.send("Только капитаны могут запустить пик.")
                    return
                caps = [p for p in players if p["is_captain"]]
                if len(caps) < 2:
                    if hasattr(reply_channel, "send"):
                        await reply_channel.send("❌ Нужно два капитана. Используйте `!cap` или `!random`.")
                    return
                total_slots = size * 2
                if len(players) < total_slots:
                    if hasattr(reply_channel, "send"):
                        await reply_channel.send(f"❌ Комната ещё не заполнена ({len(players)}/{total_slots}).")
                    return
    
                # Если пик уже завершён (нет нераспределённых) — запускаем игру (продолжаем ниже)
                unpicked = [p for p in players if p["team"] == 0]
                if unpicked:
                    # Пик ещё не начат — запускаем пик
                    cap1 = next((p for p in caps if p["team"] == 1), None)
                    cap2 = next((p for p in caps if p["team"] == 2), None)
                    if not cap1 or not cap2:
                        if hasattr(reply_channel, "send"):
                            await reply_channel.send(
                                "❌ Ошибка: не удалось найти капитанов. "
                                "Убедитесь что оба капитана назначены через `!cap` или `!random`, "
                                "затем попробуйте снова."
                            )
                        return
                    first_pick_team = random.choice([1, 2])
                    second_pick_team = 2 if first_pick_team == 1 else 1
                    await db.set_pick_turn(room_id, first_pick_team)
                    # Сильная сторона — тот кто пикует ВТОРЫМ (компенсация за выбор последним)
                    await db.set_strong_side(room_id, second_pick_team)
                    await db.update_room_status(room_id, "picking")
                    await self._refresh_lobby()
                    first_cap = cap1 if first_pick_team == 1 else cap2
                    second_cap = cap2 if first_pick_team == 1 else cap1
                    if room_channel:
                        strong_side = "🔵 Команда 1" if second_pick_team == 1 else "🔴 Команда 2"
                        strong_embed = discord.Embed(
                            title="🎯 Капитанский пик начался!",
                            description=(
                                f"👑 Капитан команды 1: <@{cap1['discord_id']}>\n"
                                f"👑 Капитан команды 2: <@{cap2['discord_id']}>\n\n"
                                f"**Первым пикует: <@{first_cap['discord_id']}>** (Команда {first_pick_team})\n\n"
                                f"⚔️ **Сильная сторона: {strong_side}** (пикует вторым — <@{second_cap['discord_id']}>)"
                            ),
                            color=0xE67E22,
                        )
                        await room_channel.send(embed=strong_embed)
                        await self._send_pick_message(room_id, room_channel, first_pick_team)
                    await self._refresh_room_embed(room_id)
                    return
                # unpicked пуст — пик завершён, все в командах, запускаем игру (продолжаем выполнение)
    
    
            # ── team / random режим: проверяем команды и запускаем игру ─
            team1 = [p for p in players if p["team"] == 1]
            team2 = [p for p in players if p["team"] == 2]
    
            if len(team1) < size or len(team2) < size:
                if hasattr(reply_channel, "send"):
                    await reply_channel.send("Обе команды должны быть полностью заполнены.")
                return
    
            # В team-режиме старт может нажать любой игрок; в random — только капитан
            if room["mode"] != "team" and not me["is_captain"]:
                if hasattr(reply_channel, "send"):
                    await reply_channel.send("Только капитаны могут запустить игру.")
                return
    
            await db.update_room_status(room_id, "started")
            await self._refresh_lobby()
    
            if room_channel:
                # Объявляем сильную сторону сразу при старте
                strong = random.choice(["🔵 Команда 1 / Team 1", "🔴 Команда 2 / Team 2"])
                strong_embed = discord.Embed(
                    title="⚔️ СИЛЬНАЯ СТОРОНА / STRONG SIDE",
                    description=f"**{strong}** — сильная сторона!\n**{strong}** — strong side!",
                )
                await room_channel.send(embed=strong_embed)
    
                mentions = " ".join(f"<@{p['discord_id']}>" for p in players)
                if room["mode"] == "team":
                    screenshot_note = "📸 **По окончании игры** любой игрок должен прислать скриншот результата прямо в этот канал.\n\nПосле этого **минимум по одному игроку с каждой команды** должны нажать кнопку результата."
                else:
                    screenshot_note = "📸 **По окончании игры** капитан должен прислать скриншот результата прямо в этот канал.\n\nПосле получения скриншота появятся кнопки **Победа / Ничья / Поражение**."
                start_embed = discord.Embed(
                    title="🚀 ИГРА НАЧАЛАСЬ!",
                    description=f"{mentions}\n\n{screenshot_note}",
                    color=0x57F287,
                )
                await room_channel.send(embed=start_embed)
    
            await self._refresh_room_embed(room_id)
        except Exception as e:
            import traceback
            traceback.print_exc()
            try:
                if hasattr(reply_channel, 'send'):
                    await reply_channel.send(f'❌ Внутренняя ошибка при запуске: {e}')
            except Exception:
                pass

    # ── win / lose / draw ─────────────────────────────────────────

    @commands.command(name="win")
    async def win(self, ctx: commands.Context):
        await self._vote_end(ctx, "win")

    @commands.command(name="lose")
    async def lose(self, ctx: commands.Context):
        await self._vote_end(ctx, "lose")

    @commands.command(name="draw")
    async def draw(self, ctx: commands.Context):
        await self._vote_end(ctx, "draw")

    async def _vote_end(self, ctx: commands.Context, vote: str):
        if not self._is_guild(ctx):
            return

        db = self.bot.db
        room = await db.get_player_room(ctx.author.id)
        if not room or room["status"] != "started":
            await ctx.send("Игра не началась или ты не в комнате.")
            return

        players = await db.get_room_players(room["room_id"])
        me = next((p for p in players if p["discord_id"] == ctx.author.id), None)
        if not me:
            await ctx.send("Ты не найден в комнате.")
            return

        # В cap/random режиме — только капитан; в team — любой игрок
        if room["mode"] != "team" and not me["is_captain"]:
            await ctx.send("Только капитаны могут завершить игру.")
            return

        # Проверяем что хотя бы один скрин загружен
        screenshots = await db.get_screenshots(room["room_id"])
        if not screenshots:
            await ctx.send("⚠️ Сначала загрузи скриншот результата в чат.")
            return

        if me["end_vote"]:
            await ctx.send(f"Ты уже проголосовал: **{me['end_vote']}**.")
            return

        await db.set_end_vote(room["room_id"], ctx.author.id, vote)
        await ctx.send(f"✅ Голос принят: **{vote}**. Ждём голоса с другой команды.")

        # Тот же lock что и в VoteButton — единая точка финализации
        lock = self._finalize_locks.setdefault(room["room_id"], asyncio.Lock())
        async with lock:
            current_room = await db.get_room(room["room_id"])
            if not current_room or current_room["status"] != "started":
                return
            players = await db.get_room_players(room["room_id"])
            await self._try_resolve_votes(current_room, players, ctx.guild)

    async def _try_resolve_votes(self, room, players, guild):
        """
        Проверяет состояние голосования и финализирует игру если условия выполнены.
        team-режим: нужен хотя бы 1 голос от каждой команды, и они должны совпадать.
        cap/random-режим: нужен голос от обоих капитанов.
        Вызывается ВНУТРИ finalize_lock.
        """
        db = self.bot.db
        room_id = room["room_id"]
        mode = room["mode"]

        team1 = [p for p in players if p["team"] == 1]
        team2 = [p for p in players if p["team"] == 2]

        if mode == "team":
            voter1 = next((p for p in team1 if p["end_vote"]), None)
            voter2 = next((p for p in team2 if p["end_vote"]), None)
            vote1 = voter1["end_vote"] if voter1 else None
            vote2 = voter2["end_vote"] if voter2 else None
        else:
            voter1 = next((p for p in players if p["team"] == 1 and p["is_captain"]), None)
            voter2 = next((p for p in players if p["team"] == 2 and p["is_captain"]), None)
            vote1 = voter1["end_vote"] if voter1 else None
            vote2 = voter2["end_vote"] if voter2 else None

        if not (vote1 and vote2):
            return  # ещё не все проголосовали

        valid = (
            (vote1 == "win" and vote2 == "lose")
            or (vote1 == "lose" and vote2 == "win")
            or (vote1 == "draw" and vote2 == "draw")
        )
        if not valid:
            for p in players:
                if p["end_vote"]:
                    await db.set_end_vote(room_id, p["discord_id"], None)
            channel = guild.get_channel(room["channel_id"]) if guild else None
            if channel:
                await channel.send(
                    "⚠️ Голоса команд не совпадают. "
                    "Допустимо: (🏆 Победа + 💀 Поражение) или (🤝 Ничья + 🤝 Ничья). "
                    "Проголосуйте заново."
                )
            return

        await self._finalize_game(room, players, voter1, voter2, vote1, vote2)

    async def _finalize_game(self, room, players, cap1, cap2, v1, v2):
        db = self.bot.db
        room_id = room["room_id"]

        # Второй барьер — атомарный UPDATE в БД.
        # Lock выше защищает от asyncio-гонки, этот UPDATE защищает
        # от любых других теоретических путей (например, game_timeout_loop).
        if not await db.try_finalize_room(room_id):
            return

        # Чистим lock после завершения (комната удаляется, lock больше не нужен)
        self._finalize_locks.pop(room_id, None)

        team1 = [p for p in players if p["team"] == 1]
        team2 = [p for p in players if p["team"] == 2]

        guild = self.bot.get_guild(Config.GUILD_ID)
        from cogs.register import Register
        reg_cog: Register = self.bot.cogs.get("Register")  # type: ignore

        elo_changes = {}

        for p in players:
            pl = await db.get_player(p["discord_id"])
            if not pl:
                continue

            my_team = p["team"]
            team_players = team1 if my_team == 1 else team2

            if v1 == "draw":
                result = "draw"
            elif (my_team == 1 and v1 == "win") or (my_team == 2 and v2 == "win"):
                result = "win"
            else:
                result = "lose"

            if result == "draw":
                delta = 0
            else:
                delta = calculate_elo(
                    team_players=team_players,
                    format_size=room["size"],
                    result=result,
                )
                # В режиме team игроки собирают свои постоянные составы — это проще,
                # поэтому ELO за такие игры снижается на 30%
                if room["mode"] == "team":
                    if delta > 0:
                        delta = max(1, int(delta * 0.7))
                    elif delta < 0:
                        delta = min(-1, int(delta * 0.7))

            if pl["penalty_games"] > 0:
                if result == "win":
                    delta = max(0, delta // 2)
                elif result == "lose":
                    delta = delta * 2

            if pl["elo"] == 0 and delta < 0:
                delta = 0

            new_elo = max(0, pl["elo"] + delta)
            elo_changes[p["discord_id"]] = (pl["elo"], new_elo, delta, result)

            await db.update_after_game(p["discord_id"], new_elo, result, room_id, mode=room["mode"], size=room["size"])

            if member := guild.get_member(p["discord_id"]):
                if reg_cog:
                    await reg_cog._sync_rank_role(member, new_elo)

        # Сохраняем результаты матча для !streak и !stat
        # result для save_game_results — результат команды 1
        t1_result = "draw" if v1 == "draw" else ("win" if v1 == "win" else "lose")
        await db.save_game_results(room_id, team1, team2, t1_result)

        channel = guild.get_channel(room["channel_id"])
        if channel:
            lines = []
            for p in players:
                if p["discord_id"] in elo_changes:
                    old_elo, new_elo, delta, result = elo_changes[p["discord_id"]]
                    sign = "+" if delta >= 0 else ""
                    result_emoji = {"win": "🏆", "lose": "💀", "draw": "🤝"}.get(result, "")
                    lines.append(
                        f"{result_emoji} <@{p['discord_id']}> "
                        f"{old_elo} → **{new_elo}** ELO ({sign}{delta})"
                    )

            embed = discord.Embed(
                title=f"🏁 Игра #{room_id} завершена!",
                description="\n".join(lines),
                color=0x57F287,
            )
            await channel.send(embed=embed)

        # Формируем results_embed для канала результатов
        if v1 == "draw":
            winner_label = "🤝 Ничья!"
            winner_color = 0x95A5A6
        elif v1 == "win":
            winner_label = "🔵 Победила **Команда 1**!"
            winner_color = 0x3498DB
        else:
            winner_label = "🔴 Победила **Команда 2**!"
            winner_color = 0xE74C3C

        def team_lines(team_list):
            lines = []
            for p in team_list:
                if p["discord_id"] in elo_changes:
                    old_elo, new_elo, delta, result = elo_changes[p["discord_id"]]
                    sign = "+" if delta >= 0 else ""
                    rank_name, _ = get_rank(new_elo)
                    lines.append(
                        f"<@{p['discord_id']}> • {rank_name} • "
                        f"{old_elo} → **{new_elo}** ({sign}{delta})"
                    )
            return "\n".join(lines) if lines else "—"

        mode_labels_r = {"team": "👥 Командный", "random": "🎲 Рандомный", "cap": "🎯 Капитанский"}
        mode_label_r = mode_labels_r.get(room["mode"], "")

        results_embed = discord.Embed(
            title=f"📋 Матч #{room_id}  ·  {room['size']}v{room['size']}  ·  {mode_label_r}  ·  {winner_label}",
            color=winner_color,
        )
        results_embed.add_field(name="🔵 Команда 1", value=team_lines(team1), inline=False)
        results_embed.add_field(name="🔴 Команда 2", value=team_lines(team2), inline=False)
        results_embed.set_footer(text="Матч завершён")
        results_embed.timestamp = discord.utils.utcnow()

        # Удаляем комнату из БД и обновляем лобби сразу — не ждём удаления канала
        # Сначала забираем скриншоты, потом удаляем
        screenshots = await db.get_screenshots(room_id)
        await db.delete_screenshots(room_id)
        await db.delete_room(room_id)
        await self._refresh_lobby()

        results_channel = await self._get_or_create_results_channel(guild)
        await results_channel.send(embed=results_embed)

        # Пересылаем скриншоты в канал результатов
        if channel and screenshots:
            for ss in screenshots:
                team_label = "🔵 Команда 1" if ss["team"] == 1 else "🔴 Команда 2"
                # Ищем оригинальное сообщение со скрином в канале комнаты
                # (они уже в истории канала — просто сообщаем об этом)
            await results_channel.send(
                f"📸 Скриншоты матча **#{room_id}** были загружены капитанами в канале комнаты."
            )

        if channel:
            await asyncio.sleep(10)
            await channel.delete(reason="Игра завершена")

    # ── Screenshot listener ───────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Слушает скриншоты от капитанов в каналах комнат во время игры."""
        if message.author.bot:
            return
        if not message.guild or message.guild.id != Config.GUILD_ID:
            return
        # Только если есть вложения (картинки)
        if not message.attachments:
            return

        db = self.bot.db
        room = await db.get_player_room(message.author.id)
        if not room or room["status"] != "started":
            return

        # Проверяем что сообщение в канале этой комнаты
        if room["channel_id"] != message.channel.id:
            return

        players = await db.get_room_players(room["room_id"])
        me = next((p for p in players if p["discord_id"] == message.author.id), None)
        if not me:
            return

        # В team-режиме скрин может загрузить любой игрок; в cap/random — только капитан
        if room["mode"] != "team" and not me["is_captain"]:
            return

        # Проверяем что вложение — изображение
        has_image = any(
            att.content_type and att.content_type.startswith("image/")
            for att in message.attachments
        )
        if not has_image:
            return

        room_id = room["room_id"]
        my_team = me["team"]

        # Сохраняем скрин (только первый от каждой команды)
        await db.add_screenshot(room_id, my_team, message.author.id)
        await message.add_reaction("✅")

        # Достаточно одного скрина — сразу показываем кнопки
        screenshots = await db.get_screenshots(room_id)
        if len(screenshots) == 1:
            # Первый скрин — пересылаем в канал результатов
            results_channel = await self._get_or_create_results_channel(message.guild)
            team_label = "🔵 Команда 1" if my_team == 1 else "🔴 Команда 2"
            files = [await att.to_file() for att in message.attachments if att.content_type and att.content_type.startswith("image/")]
            if files:
                await results_channel.send(
                    f"📸 **Матч #{room_id}** · {team_label} · {message.author.mention}",
                    files=files,
                )

            if room["mode"] == "team":
                vote_desc = (
                    f"Скрин от {message.author.mention} принят.\n\n"
                    "**Минимум по одному игроку с каждой команды** должны нажать кнопку результата:\n"
                    "**(🏆 Победа + 💀 Поражение)** или **(🤝 Ничья + 🤝 Ничья)**"
                )
            else:
                vote_desc = (
                    f"Скрин от {message.author.mention} принят.\n\n"
                    "Капитаны — нажмите кнопку с вашим результатом:\n"
                    "**(🏆 Победа + 💀 Поражение)** или **(🤝 Ничья + 🤝 Ничья)**"
                )
            vote_embed = discord.Embed(
                title="📸 Скриншот получен! Голосуйте за результат",
                description=vote_desc,
                color=0xF1C40F,
            )
            await message.channel.send(embed=vote_embed, view=VoteEndView(room_id))

    # ── Таймаут игры ──────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild.id != Config.GUILD_ID:
            return

        # Embed 1: приветствие + команды
        embed1 = discord.Embed(
            title="👋 Добро пожаловать на сервер! / Welcome to the server!",
            color=0x5865F2,
        )
        embed1.add_field(
            name="🇷🇺 Команды бота",
            value=(
                "`!register <ник>` — зарегистрироваться\n"
                "`!rename <ник>` — сменить ник\n"
                "`!create [1-4] [team/random/cap]` — создать комнату\n"
                "`!q [размер] [режим]` — войти в очередь\n"
                "`!exit` — покинуть комнату\n"
                "`!start` — начать игру\n"
                "`!win` / `!lose` / `!draw` — результат\n"
                "`!profile` / `!elo` / `!top` — статистика\n"
                "`!report @игрок причина` — жалоба\n"
                "`!help` — все команды"
            ),
            inline=True,
        )
        embed1.add_field(
            name="🇬🇧 Bot Commands",
            value=(
                "`!register <nick>` — register\n"
                "`!rename <nick>` — change nickname\n"
                "`!create [1-4] [team/random/cap]` — create room\n"
                "`!q [size] [mode]` — join queue\n"
                "`!exit` — leave room\n"
                "`!start` — start game\n"
                "`!win` / `!lose` / `!draw` — report result\n"
                "`!profile` / `!elo` / `!top` — stats\n"
                "`!report @player reason` — report\n"
                "`!help` — all commands"
            ),
            inline=True,
        )

        # Embed 2: правила (отдельный embed — нет ограничений по полям)
        embed2 = discord.Embed(
            title="📜 Правила / Rules",
            color=0xE67E22,
        )
        embed2.add_field(
            name="🇷🇺 Правила",
            value=(
                "**1.** По запросу администрации вы **обязаны показать игровую консоль**.\n"
                "**2.** Запрещены оскорбления, токсичность и читы.\n"
                "**3.** Оба капитана честно указывают результат. Фальсификация = бан.\n"
                "**4.** Выход из начатой игры = штраф (-ELO на 3 игры).\n"
                "**5.** Жалобы: `!report @игрок причина`. Лимит — 5/сутки."
            ),
            inline=False,
        )
        embed2.add_field(
            name="🇬🇧 Rules",
            value=(
                "**1.** You must **show your game console** on request by admins.\n"
                "**2.** Insults, toxic behaviour and cheating are prohibited.\n"
                "**3.** Both captains must report the result honestly. Falsifying = ban.\n"
                "**4.** Leaving a started game results in a penalty (-ELO for 3 games).\n"
                "**5.** Reports: `!report @player reason`. Limit — 5/day."
            ),
            inline=False,
        )
        embed2.set_footer(text="Удачи! / Good luck! 🎮")

        # Отправляем ТОЛЬКО в DM. Если DM закрыты — ничего не делаем.
        try:
            await member.send(embeds=[embed1, embed2])
        except discord.Forbidden:
            pass

    @tasks.loop(minutes=1)
    async def game_timeout_loop(self):
        await self.bot.wait_until_ready()
        db = self.bot.db
        rooms = await db.get_started_rooms()
        now = datetime.datetime.utcnow()

        for room in rooms:
            if not room["started_at"]:
                continue

            started = datetime.datetime.fromisoformat(str(room["started_at"]))
            elapsed = (now - started).total_seconds() / 60

            guild = self.bot.get_guild(Config.GUILD_ID)
            if not guild:
                continue
            channel = guild.get_channel(room["channel_id"])

            players = await db.get_room_players(room["room_id"])
            caps = [p for p in players if p["is_captain"]]

            cap1 = next((p for p in caps if p["team"] == 1), None)
            cap2 = next((p for p in caps if p["team"] == 2), None)
            both_voted = cap1 and cap2 and cap1["end_vote"] and cap2["end_vote"]
            if both_voted:
                continue

            ping_minutes = Config.GAME_PING_MINUTES
            disband_minutes = Config.GAME_DISBAND_MINUTES

            if elapsed >= disband_minutes:
                for cap in caps:
                    if not cap["end_vote"]:
                        await db.apply_penalty(cap["discord_id"])
                        m = guild.get_member(cap["discord_id"])
                        if channel and m:
                            await channel.send(
                                f"⚠️ {m.mention} получает штраф (-ELO на 3 игры) за незавершённую игру."
                            )
                if channel:
                    await channel.send("🔴 Игра расформирована из-за отсутствия подтверждения.")
                    await asyncio.sleep(5)
                    await channel.delete(reason="Таймаут игры")
                await db.delete_room(room["room_id"])

            elif elapsed >= ping_minutes and not room["pinged_at"]:
                if channel:
                    mentions = " ".join(
                        f"<@{cap['discord_id']}>"
                        for cap in caps
                        if not cap["end_vote"]
                    )
                    await channel.send(
                        f"⏰ {mentions} Прошёл час с начала игры! "
                        f"Подтвердите результат нажав кнопку ниже.\n"
                        f"Если через 30 минут не будет ответа — игра расформируется со штрафом.",
                        view=VoteEndView(room["room_id"]),
                    )
                await db.set_pinged(room["room_id"])

    # ── Модераторские команды ─────────────────────────────────────

    @commands.command(name="mod_kick")
    async def mod_kick(self, ctx: commands.Context, member: discord.Member = None):
        """[Мод] Кикнуть игрока из комнаты в любое время. Алиас для !kick @игрок."""
        if not self._is_guild(ctx):
            return
        if not self._is_mod(ctx.author):
            await ctx.send("Нет прав.")
            return
        if member is None:
            await ctx.send("Укажи игрока: `!mod_kick @игрок`")
            return
        # Передаём в единую логику !kick (мод-ветка)
        ctx2 = ctx
        await self.kick(ctx2, member)

    @commands.command(name="mod_end")
    async def mod_end(self, ctx: commands.Context, room_id: int):
        if not self._is_guild(ctx):
            return
        if not self._is_mod(ctx.author):
            await ctx.send("Нет прав.")
            return

        db = self.bot.db
        room = await db.get_room(room_id)
        if not room:
            await ctx.send("Комната не найдена.")
            return

        guild = ctx.guild
        channel = guild.get_channel(room["channel_id"])
        if channel:
            await channel.send("🔨 [Мод] Игра принудительно расформирована.")
            await asyncio.sleep(3)
            await channel.delete()
        await db.delete_room(room_id)
        await ctx.send(f"✅ Комната #{room_id} расформирована.")
        await self._refresh_lobby()

    @commands.command(name="mod_captain")
    async def mod_captain(self, ctx: commands.Context, member: discord.Member = None):
        """[Мод] Назначить игрока капитаном. Алиас для !cap @игрок."""
        if not self._is_guild(ctx):
            return
        if not self._is_mod(ctx.author):
            await ctx.send("Нет прав.")
            return
        if member is None:
            await ctx.send("Укажи игрока: `!mod_captain @игрок`")
            return
        await self.become_captain(ctx, member)


async def setup(bot):
    await bot.add_cog(Rooms(bot))