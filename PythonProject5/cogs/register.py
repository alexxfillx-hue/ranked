import discord
from discord.ext import commands
from config import Config, RANKS, get_rank
from utils.i18n import t


class LangButton(discord.ui.Button):
    """Кнопка выбора языка при регистрации."""

    def __init__(self, lang: str, nickname: str):
        labels = {"ru": "🇷🇺 Русский", "en": "🇬🇧 English"}
        styles = {"ru": discord.ButtonStyle.primary, "en": discord.ButtonStyle.secondary}
        super().__init__(
            label=labels[lang],
            style=styles[lang],
            # custom_id СТАТИЧЕСКИЙ — без timestamp/uid.
            # Это обязательно для persistent views (bot.add_view).
            custom_id=f"lang_{lang}",
        )
        self.lang = lang
        self.nickname = nickname

    async def callback(self, interaction: discord.Interaction):
        # Откладываем ответ сразу — операции с БД, ролями и ником занимают > 3 сек
        await interaction.response.defer(ephemeral=False)

        db = interaction.client.db
        lang = self.lang

        # Получаем nickname: либо из self (свежая кнопка), либо из embed (после рестарта бота).
        nickname = self.nickname
        if nickname == "_restore_":
            try:
                embeds = interaction.message.embeds
                if embeds and embeds[0].description:
                    import re
                    match = re.search(r"\*\*(.+?)\*\*", embeds[0].description)
                    if match:
                        nickname = match.group(1)
            except Exception:
                pass

        if not nickname or nickname == "_restore_":
            await interaction.edit_original_response(
                content="⚠️ Не удалось определить ник. Напиши `!register <ник>` заново.",
                embed=None, view=None,
            )
            return

        # Снимаем pending вне зависимости от исхода
        reg_cog = interaction.client.cogs.get("Register")
        if reg_cog:
            reg_cog._pending_registration.pop(interaction.user.id, None)

        existing = await db.get_player(interaction.user.id)
        if existing:
            await interaction.edit_original_response(
                content=t("register_already", lang, username=existing["username"], elo=existing["elo"]),
                embed=None, view=None,
            )
            return

        taken = await db.get_player_by_username(nickname)
        if taken:
            await interaction.edit_original_response(
                content=t("register_nick_taken", lang, nick=nickname),
                embed=None, view=None,
            )
            return

        ok = await db.register(interaction.user.id, nickname)
        if ok:
            await db.set_lang(interaction.user.id, lang)

            guild = interaction.guild
            member = None
            if guild:
                # fetch_member гарантирует актуальные данные даже без кэша
                try:
                    member = await guild.fetch_member(interaction.user.id)
                except (discord.NotFound, discord.HTTPException):
                    member = None

            # Меняем ник на сервере
            if member:
                try:
                    await member.edit(nick=nickname, reason="Регистрация в боте")
                except (discord.Forbidden, discord.HTTPException):
                    pass  # нет прав — не критично

            # Выдаём ранговую роль (Bronze I при ELO = 0)
            if reg_cog and member:
                try:
                    await reg_cog._sync_rank_role(member, Config.STARTING_ELO)
                except Exception:
                    pass

            embed = discord.Embed(
                title=t("register_ok_title", lang),
                description=t("register_ok_desc", lang, nick=nickname),
                color=0x57F287,
            )
            await interaction.edit_original_response(embed=embed, view=None, content=None)
        else:
            await interaction.edit_original_response(
                content=t("register_error", lang), embed=None, view=None,
            )


class LangSelectView(discord.ui.View):
    def __init__(self, nickname: str, uid: int, pending_set):
        # timeout=None ОБЯЗАТЕЛЕН для persistent view.
        # View с любым другим timeout вызовет ValueError в bot.add_view().
        super().__init__(timeout=None)
        self.uid = uid
        self._pending_set = pending_set
        self.add_item(LangButton("ru", nickname))
        self.add_item(LangButton("en", nickname))


class Register(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # dict: discord_id → message_id сообщения с выбором языка.
        # Позволяет удалить старое сообщение если игрок снова вызвал !register.
        self._pending_registration: dict[int, int] = {}

    def _guild_check(self, ctx):
        return ctx.guild and ctx.guild.id == Config.GUILD_ID

    async def _sync_rank_role(self, member: discord.Member, elo: int):
        guild = member.guild
        rank_name, _ = get_rank(elo)
        for _, _, name, _, role_name in RANKS:
            role = discord.utils.get(guild.roles, name=role_name)
            if role is None:
                continue
            if name == rank_name:
                if role not in member.roles:
                    try:
                        await member.add_roles(role, reason=f"ELO {elo} → {rank_name}")
                    except discord.Forbidden:
                        pass
                    except discord.HTTPException:
                        pass
            else:
                if role in member.roles:
                    try:
                        await member.remove_roles(role, reason="ELO rank update")
                    except discord.Forbidden:
                        pass
                    except discord.HTTPException:
                        pass

    async def _get_lang(self, discord_id: int) -> str:
        return await self.bot.db.get_lang(discord_id)

    @commands.command(name="register")
    async def register(self, ctx: commands.Context, *, nickname: str = None):
        if not self._guild_check(ctx):
            return

        if not nickname:
            embed = discord.Embed(
                title="⚠️ Укажи игровой ник / Enter your in-game nickname",
                description=(
                    "🇷🇺 Команда: `!register <твой_ник>`\n"
                    "🇬🇧 Command: `!register <your_nickname>`\n\n"
                    "Пример / Example: `!register alekz`"
                ),
                color=0xED4245,
            )
            await ctx.send(embed=embed)
            return

        nickname = nickname.strip()
        if len(nickname) < 2:
            await ctx.send(
                "⚠️ Ник слишком короткий. Минимум 2 символа. / "
                "Nickname too short. Minimum 2 characters."
            )
            return
        if len(nickname) > 32:
            await ctx.send(
                "⚠️ Ник слишком длинный. Максимум 32 символа. / "
                "Nickname too long. Maximum 32 characters."
            )
            return

        db = self.bot.db
        existing = await db.get_player(ctx.author.id)
        if existing:
            lang = await self._get_lang(ctx.author.id)
            await ctx.send(
                f"{ctx.author.mention} " +
                t("register_already", lang, username=existing["username"], elo=existing["elo"])
            )
            return

        # Если у игрока уже открыто сообщение с выбором языка — удаляем старое
        if ctx.author.id in self._pending_registration:
            old_msg_id = self._pending_registration[ctx.author.id]
            try:
                old_msg = await ctx.channel.fetch_message(old_msg_id)
                await old_msg.delete()
            except (discord.NotFound, discord.Forbidden):
                pass
            del self._pending_registration[ctx.author.id]

        taken = await db.get_player_by_username(nickname)
        if taken:
            await ctx.send(
                f"⚠️ Ник **{nickname}** уже занят. Выбери другой. / "
                f"Nickname **{nickname}** is already taken."
            )
            return

        embed = discord.Embed(
            title="🌐 Выбери язык / Choose your language",
            description=(
                f"Ник / Nickname: **{nickname}**\n\n"
                "🇷🇺 Все сообщения бота будут на **русском**.\n"
                "🇬🇧 All bot messages will be in **English**."
            ),
            color=0x5865F2,
        )
        proxy = _PendingProxy(self._pending_registration, ctx.author.id)
        view = LangSelectView(nickname, ctx.author.id, proxy)
        msg = await ctx.send(embed=embed, view=view)
        self._pending_registration[ctx.author.id] = msg.id

    @commands.command(name="debug_roles")
    @commands.has_permissions(administrator=True)
    async def debug_roles(self, ctx: commands.Context):
        """[Админ] Проверить существуют ли ранговые роли на сервере."""
        if not self._guild_check(ctx):
            return
        lines = []
        for _, _, name, _, role_name in RANKS:
            role = discord.utils.get(ctx.guild.roles, name=role_name)
            status = f"✅ найдена (id:{role.id})" if role else "❌ НЕ НАЙДЕНА"
            lines.append(f"`{role_name}` — {status}")
        embed = discord.Embed(title="🔍 Диагностика ранговых ролей", description="\n".join(lines), color=0x5865F2)
        await ctx.send(embed=embed)

    @commands.command(name="fix_role")
    async def fix_role(self, ctx: commands.Context, member: discord.Member = None):
        """Принудительно выдать ранговую роль себе или указанному игроку."""
        if not self._guild_check(ctx):
            return
        target = member or ctx.author
        # Только модератор может фиксить чужую роль
        if target != ctx.author and not (
            ctx.author.guild_permissions.administrator or
            discord.utils.get(ctx.author.roles, name=Config.MODERATOR_ROLE_NAME)
        ):
            await ctx.send("❌ Нет прав.")
            return
        player = await self.bot.db.get_player(target.id)
        if not player:
            await ctx.send("❌ Игрок не зарегистрирован.")
            return
        await self._sync_rank_role(target, player["elo"])
        from config import get_rank
        rank_name, _ = get_rank(player["elo"])
        await ctx.send(f"✅ Роль **{rank_name}** выдана для {target.mention} (ELO: {player['elo']})")


    @commands.command(name="rename")
    async def rename(self, ctx: commands.Context, *, new_nick: str = None):
        if not self._guild_check(ctx):
            return

        lang = await self._get_lang(ctx.author.id)

        if not new_nick:
            await ctx.send(t("rename_usage", lang))
            return

        new_nick = new_nick.strip()
        if len(new_nick) < 2 or len(new_nick) > 32:
            await ctx.send(t("rename_length", lang))
            return

        db = self.bot.db
        player = await db.get_player(ctx.author.id)
        if not player:
            await ctx.send(t("rename_not_registered", lang))
            return

        taken = await db.get_player_by_username(new_nick)
        if taken and taken["discord_id"] != ctx.author.id:
            await ctx.send(t("rename_taken", lang, nick=new_nick))
            return

        await db.update_username(ctx.author.id, new_nick)

        try:
            await ctx.author.edit(nick=new_nick, reason="Смена игрового ника")
            await ctx.send(t("rename_ok_server", lang, nick=new_nick))
        except discord.Forbidden:
            await ctx.send(t("rename_ok_manual", lang, nick=new_nick))


class _PendingProxy:
    """
    Прокси для совместимости LangSelectView с dict-хранилищем pending.
    Передаётся как pending_set в LangSelectView.
    """
    def __init__(self, mapping: dict, uid: int):
        self._mapping = mapping
        self._uid = uid

    def discard(self, uid: int):
        self._mapping.pop(uid, None)


async def setup(bot):
    await bot.add_cog(Register(bot))
