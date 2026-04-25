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
            custom_id=f"lang_{lang}_{nickname[:20]}",
        )
        self.lang = lang
        self.nickname = nickname

    async def callback(self, interaction: discord.Interaction):
        db = interaction.client.db
        lang = self.lang
        nickname = self.nickname

        existing = await db.get_player(interaction.user.id)
        if existing:
            await interaction.response.edit_message(
                content=t("register_already", lang, username=existing["username"], elo=existing["elo"]),
                embed=None, view=None,
            )
            return

        taken = await db.get_player_by_username(nickname)
        if taken:
            await interaction.response.edit_message(
                content=t("register_nick_taken", lang, nick=nickname),
                embed=None, view=None,
            )
            return

        ok = await db.register(interaction.user.id, nickname)
        if ok:
            await db.set_lang(interaction.user.id, lang)
            try:
                await interaction.user.edit(nick=nickname, reason="Регистрация в боте")
            except discord.Forbidden:
                pass

            reg_cog = interaction.client.cogs.get("Register")
            if reg_cog:
                await reg_cog._sync_rank_role(interaction.user, Config.STARTING_ELO)

            embed = discord.Embed(
                title=t("register_ok_title", lang),
                description=t("register_ok_desc", lang, nick=nickname),
                color=0x57F287,
            )
            await interaction.response.edit_message(embed=embed, view=None, content=None)
        else:
            await interaction.response.edit_message(
                content=t("register_error", lang), embed=None, view=None,
            )


class LangSelectView(discord.ui.View):
    def __init__(self, nickname: str):
        super().__init__(timeout=120)
        self.add_item(LangButton("ru", nickname))
        self.add_item(LangButton("en", nickname))

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True


class Register(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

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
            else:
                if role in member.roles:
                    try:
                        await member.remove_roles(role, reason="ELO rank update")
                    except discord.Forbidden:
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
        view = LangSelectView(nickname)
        await ctx.send(embed=embed, view=view)

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


async def setup(bot):
    await bot.add_cog(Register(bot))