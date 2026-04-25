import asyncio
import logging

import discord
from discord.ext import commands
from discord import app_commands

from config import Config
from database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
bot.db = Database()  # type: ignore

COGS = [
    "cogs.register",
    "cogs.rooms",
    "cogs.profile",
    "cogs.leaderboard",
]


async def _register_persistent_views():
    """
    Регистрирует все persistent views при старте бота.

    Discord.py требует вызова bot.add_view() для каждого view с timeout=None
    ПЕРЕД тем как бот начнёт принимать interactions. Без этого нажатие на
    старые кнопки (из сообщений до рестарта) даёт «Ошибка взаимодействия».

    Что регистрируем:
      1. LangSelectView — кнопки выбора языка при !register
         (custom_id: lang_ru, lang_en)
      2. CreateRoomView — панель выбора режима/размера (!create без аргументов)
         (custom_id: create_{size}_{mode}, create_1_team)
      3. RoomView — кнопки внутри каждой активной комнаты
         (custom_id: start_btn_{id}, exit_room_{id}, vote_*_{id}, …)
         Для каждой комнаты нужен отдельный экземпляр с её room_id.
      4. JoinRoomView — кнопка «Присоединиться» в лобби
         (custom_id: join_room_{id})
    """
    from cogs.register import LangSelectView, _PendingProxy
    from cogs.rooms import CreateRoomView, RoomView, JoinRoomView

    # 1. LangSelectView — один экземпляр (static custom_ids: lang_ru, lang_en)
    #    nickname не важен при восстановлении — callback перечитывает его из message
    bot.add_view(LangSelectView("_restore_", 0, _PendingProxy({}, 0)))

    # 2. CreateRoomView — один экземпляр (static custom_ids: create_{size}_{mode})
    bot.add_view(CreateRoomView())

    # 3. RoomView и JoinRoomView — по одному экземпляру на каждую активную комнату
    active_rooms = await bot.db.get_open_rooms()
    started_rooms = await bot.db.get_started_rooms()
    all_rooms = {r["room_id"]: r for r in (active_rooms + started_rooms)}

    # Добавляем «full» и «picking» комнаты тоже
    try:
        import asyncpg  # noqa
        extra_rows = await bot.db.pool.fetch(
            "SELECT * FROM rooms WHERE status IN ('full','picking','awaiting_screenshot')"
        )
        for r in extra_rows:
            all_rooms[r["room_id"]] = dict(r)
    except Exception as e:
        log.warning(f"Could not fetch extra rooms for view restore: {e}")

    for room_id, room in all_rooms.items():
        status = room["status"]
        mode = room.get("mode", "team")
        size = room.get("size", 4)

        # RoomView для канала комнаты
        bot.add_view(
            RoomView(bot, room_id, room_status=status, room_mode=mode, room_size=size),
            message_id=room.get("embed_message_id"),
        )

        # JoinRoomView для лобби (только открытые комнаты)
        if status == "waiting":
            bot.add_view(JoinRoomView(room_id, size, mode, is_full=False))

    log.info(f"Persistent views registered ({len(all_rooms)} active rooms)")


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"Guild ID: {Config.GUILD_ID}")

    await _register_persistent_views()

    try:
        guild = discord.Object(id=Config.GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        log.info("Slash commands synced")
    except Exception as e:
        log.warning(f"Could not sync slash commands: {e}")


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Не хватает аргументов. `!help {ctx.command}`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("⚠️ Неверный тип аргумента.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        log.exception(f"Command error in {ctx.command}", exc_info=error)


@bot.command(name="cats")
async def cats_cmd(ctx: commands.Context):
    lines = []
    for cat in ctx.guild.categories:
        lines.append(f"`{repr(cat.name)}` — id:{cat.id}")
    await ctx.send("\n".join(lines) or "Нет категорий")


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(title="📖  Команды бота", color=0x5865F2)
    cmds = [
        ("!register <ник>", "Зарегистрироваться"),
        ("!rename <новый_ник>", "Сменить игровой ник"),
        ("!ranks", "Список всех рангов и ELO"),
        ("!create [1/2/3/4] [team/random/cap]", "Создать комнату"),
        ("!queue [1/2/3/4] [режим]  или  !q", "Найти комнату в очереди"),
        ("!exit", "Выйти из комнаты"),
        ("!kick @игрок", "Кикнуть игрока (капитан)"),
        ("!start", "Начать игру (оба капитана)"),
        ("!win / !lose / !draw", "Завершить игру (капитаны)"),
        ("!profile [@игрок]", "Профиль игрока"),
        ("!elo [day/week/month/all] [@игрок]", "График ELO"),
        ("!top", "Топ-10 по ELO"),
        ("!report @игрок причина", "Пожаловаться на игрока"),
        ("!rules", "Правила сервера"),
        ("!mod_kick @игрок", "[Мод] Кикнуть из комнаты"),
        ("!mod_end #room_id", "[Мод] Расформировать игру"),
        ("!mod_captain @игрок", "[Мод] Переназначить капитана"),
        ("!plus @игрок <кол-во>", "[Мод] Прибавить ELO"),
        ("!minus @игрок <кол-во>", "[Мод] Отнять ELO"),
    ]
    for name, desc in cmds:
        embed.add_field(name=f"`{name}`", value=desc, inline=False)
    await ctx.send(embed=embed)


# Slash-команды для подсказок
@bot.tree.command(name="register", description="Зарегистрироваться: !register <ник>")
async def slash_register(interaction: discord.Interaction):
    await interaction.response.send_message("Используй: `!register <твой_ник>`", ephemeral=True)

@bot.tree.command(name="ranks", description="Список всех рангов и ELO: !ranks")
async def slash_ranks(interaction: discord.Interaction):
    await interaction.response.send_message("Используй: `!ranks`", ephemeral=True)

@bot.tree.command(name="create", description="Создать игровую комнату: !create 4 team")
async def slash_create(interaction: discord.Interaction):
    await interaction.response.send_message("Используй: `!create [1/2/3/4] [team/random/cap]`", ephemeral=True)

@bot.tree.command(name="queue", description="Войти в очередь поиска игры: !q 4 random")
async def slash_queue(interaction: discord.Interaction):
    await interaction.response.send_message("Используй: `!q <размер> <режим>`", ephemeral=True)

@bot.tree.command(name="profile", description="Посмотреть профиль игрока: !profile")
async def slash_profile(interaction: discord.Interaction):
    await interaction.response.send_message("Используй: `!profile` или `!profile @игрок`", ephemeral=True)

@bot.tree.command(name="top", description="Топ-10 игроков по ELO: !top")
async def slash_top(interaction: discord.Interaction):
    await interaction.response.send_message("Используй: `!top`", ephemeral=True)

@bot.tree.command(name="help", description="Список всех команд бота: !help")
async def slash_help(interaction: discord.Interaction):
    await interaction.response.send_message("Используй: `!help`", ephemeral=True)


async def main():
    async with bot:
        await bot.db.init()
        for cog in COGS:
            await bot.load_extension(cog)
            log.info(f"Loaded {cog}")
        await bot.start(Config.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
