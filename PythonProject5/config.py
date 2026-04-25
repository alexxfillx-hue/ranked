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


# (min_elo, max_elo, name, color, role_name)
RANKS: list[tuple] = [
    (0, 99, "Бронза 1", 0xCD7F32, "Бронза 1"),
    (100, 199, "Бронза 2", 0xCD7F32, "Бронза 2"),
    (200, 299, "Бронза 3", 0xCD7F32, "Бронза 3"),
    (300, 399, "Серебро I", 0xC0C0C0, "Серебро I"),
    (400, 499, "Серебро II", 0xC0C0C0, "Серебро II"),
    (500, 599, "Золото I", 0xFFD700, "Золото I"),
    (600, 699, "Золото II", 0xFFD700, "Золото II"),
    (700, 799, "Платина", 0x00CED1, "Платина"),
    (800, 899, "Алмаз", 0x00BFFF, "Алмаз"),
    (900, 99999, "Мастер", 0xFF6347, "Мастер"),
]


def get_rank(elo: int) -> tuple[str, int]:
    elo = max(0, elo)
    for min_e, max_e, name, color, _ in RANKS:
        if min_e <= elo <= max_e:
            return name, color
    return RANKS[-1][2], RANKS[-1][3]