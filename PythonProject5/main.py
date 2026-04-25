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

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
bot.db = Database()  # type: ignore

COGS = [
    "cogs.register",
    "cogs.rooms",
    "cogs.profile",
    "cogs.leaderboard",
]


@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")
    log.info(f"Guild ID: {Config.GUILD_ID}")


@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"⚠️ Не хватает аргументов. `!help {ctx.command}`")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("⚠️ Неверный тип аргумента.")
    elif isinstance(error, commands.CommandNotFound):
        await ctx.send("❓ Неизвестная команда. Используй `!help` для списка команд.")
    else:
        log.exception(f"Command error in {ctx.command}", exc_info=error)


@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    embed = discord.Embed(
        title="📖  Команды бота",
        color=0x5865F2,
    )
    cmds = [
        ("!register <ник>", "Зарегистрироваться (вписать игровой ник)"),
        ("!rename <новый_ник>", "Сменить игровой ник"),
        ("!create [1/2/3/4]", "Создать комнату (по умолч. 4v4)"),
        ("!create1 / !create2 / !create4", "Быстрое создание по размеру"),
        ("!queue [1/2/3/4]  или  !q", "Найти комнату в очереди"),
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
    ]
    for name, desc in cmds:
        embed.add_field(name=f"`{name}`", value=desc, inline=False)
    await ctx.send(embed=embed)


async def main():
    async with bot:
        await bot.db.init()
        for cog in COGS:
            await bot.load_extension(cog)
            log.info(f"Loaded {cog}")
        await bot.start(Config.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())