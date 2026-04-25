import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    TOKEN: str = os.getenv("DISCORD_TOKEN", "")
    GUILD_ID: int = int(os.getenv("GUILD_ID", "0"))
    CATEGORY_NAME: str = os.getenv("CATEGORY_NAME", "Игровые комнаты")
    ADMIN_CHANNEL_NAME: str = os.getenv("ADMIN_CHANNEL_NAME", "📌・¦・admin")
    RESULTS_CHANNEL_NAME: str = os.getenv("RESULTS_CHANNEL_NAME", "✅・¦・match-results")
    LOBBY_CHANNEL_NAME: str = os.getenv("LOBBY_CHANNEL_NAME", "🔎・¦・search-game")
    MODERATOR_ROLE_NAME: str = os.getenv("MODERATOR_ROLE_NAME", "Модератор")
    PLAY_CHANNEL_NAME: str = os.getenv("PLAY_CHANNEL_NAME", "𝐏𝐋𝐀𝐘🟢")
    DB_PATH: str = os.getenv("DB_PATH", "bot.db")
    STARTING_ELO: int = 0
    NEWCOMER_GAMES: int = 10
    GAME_PING_MINUTES: int = 60  # через сколько минут пинговать
    GAME_DISBAND_MINUTES: int = 90  # через сколько минут расформировать


# (min_elo, max_elo, display_name, color, role_name)
# role_name должен ТОЧНО совпадать с названием роли в Discord
RANKS: list[tuple] = [
    (0,   99,    "Bronze Ⅰ",   0xCD7F32, "Bronze Ⅰ"),
    (100, 199,   "Bronze ⅠⅠ",  0xCD7F32, "Bronze ⅠⅠ"),
    (200, 299,   "Bronze ⅠⅠⅠ", 0xCD7F32, "Bronze ⅠⅠⅠ"),
    (300, 399,   "Silver Ⅰ",   0xC0C0C0, "Silver Ⅰ"),
    (400, 499,   "Silver ⅠⅠ",  0xC0C0C0, "Silver ⅠⅠ"),
    (500, 599,   "Gold Ⅰ",     0xFFD700, "Gold Ⅰ"),
    (600, 699,   "Gold ⅠⅠ",    0xFFD700, "Gold ⅠⅠ"),
    (700, 799,   "Platinum",    0x00CED1, "Platinum"),
    (800, 899,   "Diamond",     0x00BFFF, "Diamond"),
    (900, 99999, "Master",      0xFF6347, "Master"),
]


def get_rank(elo: int) -> tuple[str, int]:
    elo = max(0, elo)
    for min_e, max_e, name, color, _ in RANKS:
        if min_e <= elo <= max_e:
            return name, color
    return RANKS[-1][2], RANKS[-1][3]