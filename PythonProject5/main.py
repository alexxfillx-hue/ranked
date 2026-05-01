import asyncio
import logging

import discord
from discord.ext import commands

from config import Config
from database import Database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("bot")

# ── Проверка RapidOCR при старте ───────────────────────────────────────────────
try:
    from rapidocr_onnxruntime import RapidOCR as _RapidOCR
    log.info("RapidOCR (ONNX) доступен — OCR скриншотов активен.")
except ImportError:
    log.warning("rapidocr_onnxruntime не установлен — OCR скриншотов недоступен.")
# ───────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
bot.db = Database()  # type: ignore

COGS = [
    "cogs.register",
    "cogs.rooms",
    "cogs.profile",
    "cogs.leaderboard",
    "cogs.ban",
]


async def _register_persistent_views():
    """
    Регистрирует все persistent views ДО bot.start().

    Вызывается ПОСЛЕ загрузки когов (чтобы импорты работали) но ДО подключения к Discord.
    bot.add_view() регистрирует обработчики для кнопок из старых сообщений.
    Без этого любая кнопка из сообщения до рестарта даёт «Ошибка взаимодействия».

    ВАЖНО: все View передаваемые сюда должны иметь timeout=None — это требование discord.py
    для persistent views. View с timeout != None вызовет ValueError.
    """
    from cogs.register import LangSelectView, _PendingProxy
    from cogs.rooms import CreateRoomView, RoomView, JoinRoomView

    # 1. LangSelectView (custom_id: lang_ru, lang_en)
    bot.add_view(LangSelectView("_restore_", 0, _PendingProxy({}, 0)))

    # 2. CreateRoomView (custom_id: create_{size}_{mode}, create_1_team)
    bot.add_view(CreateRoomView())

    # 3. RoomView + JoinRoomView — по одному на каждую активную комнату в БД
    try:
        all_room_rows = await bot.db.pool.fetch(
            "SELECT * FROM rooms WHERE status IN ('waiting','full','picking','started','awaiting_screenshot')"
        )
    except Exception as e:
        log.warning(f"Could not fetch rooms for persistent view restore: {e}")
        all_room_rows = []

    count = 0
    for row in all_room_rows:
        room_id = row["room_id"]
        status = row["status"]
        mode = row["mode"] or "team"
        size = row["size"] or 4
        embed_msg_id = row["embed_message_id"]

        bot.add_view(
            RoomView(bot, room_id, room_status=status, room_mode=mode, room_size=size),
            message_id=embed_msg_id,
        )

        is_full = status != "waiting"
        bot.add_view(JoinRoomView(room_id, size, mode, is_full=is_full))

        count += 1

    log.info(f"Persistent views registered ({count} active rooms)")


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"Guild ID: {Config.GUILD_ID}")
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
        ("!streak [@игрок]", "История игр и текущая серия"),
        ("!stat [@игрок]", "Статистика личных встреч"),
        ("!eloinfo [ru/en]", "Как работает система ELO"),
        ("!commands", "Подробное описание команд игрока"),
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

@bot.tree.command(name="commands", description="Подробное описание команд для игроков: !commands")
async def slash_commands(interaction: discord.Interaction):
    await interaction.response.send_message("Используй: `!commands`", ephemeral=True)


async def main():
    async with bot:
        await bot.db.init()
        for cog in COGS:
            await bot.load_extension(cog)
            log.info(f"Loaded {cog}")
        await _register_persistent_views()
        await bot.start(Config.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
