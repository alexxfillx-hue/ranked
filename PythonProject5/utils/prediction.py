# utils/prediction.py
"""
Расчёт шанса победы команд на основе ELO и винрейта.

Формула:
  1. Рейтинг команды = среднее ELO * 0.65 + средний WR% * 3.5
     (ELO важнее, WR добавляет нюанс)
  2. Шанс победы команды 1 = rating1 / (rating1 + rating2) * 100
  3. Результат зеркалируется для команды 2.
"""
import discord
from config import get_rank


# ── Расчёт ────────────────────────────────────────────────────────────────────

def _player_wr(player: dict) -> float:
    """Винрейт игрока в процентах (0–100). Новичкам даётся нейтральный 50%."""
    decisive = player["wins"] + player["losses"]
    if decisive < 5:          # слишком мало игр — не наказываем новичка
        return 50.0
    return round(player["wins"] / decisive * 100, 1)


def _team_rating(players: list[dict]) -> float:
    """Комбинированный рейтинг команды: ELO × 0.65 + WR × 3.5."""
    if not players:
        return 0.0
    avg_elo = sum(p["elo"] for p in players) / len(players)
    avg_wr  = sum(_player_wr(p) for p in players) / len(players)
    return avg_elo * 0.65 + avg_wr * 3.5


def calculate_win_chance(team1: list[dict], team2: list[dict]) -> tuple[float, float]:
    """
    Возвращает (chance_team1, chance_team2) в процентах, сумма = 100.
    При равных командах возвращает (50.0, 50.0).
    """
    r1 = _team_rating(team1)
    r2 = _team_rating(team2)
    total = r1 + r2
    if total == 0:
        return 50.0, 50.0
    c1 = round(r1 / total * 100, 1)
    c2 = round(100 - c1, 1)
    return c1, c2


# ── Визуальная полоска прогресса ──────────────────────────────────────────────

def _bar(pct: float, width: int = 12) -> str:
    """
    Возвращает строку вида '████████░░░░' длиной width символов.
    pct — процент заполнения (0–100).
    """
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


# ── Embed ─────────────────────────────────────────────────────────────────────

def match_prediction_embed(
    room_id: int,
    team1: list[dict],
    team2: list[dict],
    size: int,
    mode: str,
) -> discord.Embed:
    """
    Строит embed с прогнозом матча.
    Вызывать после того, как игра началась (статус → started).
    """
    chance1, chance2 = calculate_win_chance(team1, team2)

    # ── Определяем фаворита ──────────────────────────────────────────────────
    if chance1 > chance2:
        color = 0x3498DB   # синий — фаворит команда 1
        fav = "🔵 Команда 1"
    elif chance2 > chance1:
        color = 0xE74C3C   # красный — фаворит команда 2
        fav = "🔴 Команда 2"
    else:
        color = 0x95A5A6   # серый — равные шансы
        fav = "⚖️ Равные шансы"

    # ── Строки игроков ───────────────────────────────────────────────────────
    def player_lines(players: list[dict]) -> str:
        lines = []
        for p in players:
            rank, _ = get_rank(p["elo"])
            wr = _player_wr(p)
            crown = "👑 " if p.get("is_captain") else "• "
            lines.append(
                f"{crown}**{p['username']}** — {p['elo']} ELO  |  WR {wr}%  |  {rank}"
            )
        return "\n".join(lines) if lines else "—"

    # ── Средние показатели команд ────────────────────────────────────────────
    def team_stats(players: list[dict]) -> str:
        if not players:
            return "—"
        avg_elo = round(sum(p["elo"] for p in players) / len(players))
        avg_wr  = round(sum(_player_wr(p) for p in players) / len(players), 1)
        return f"Avg ELO: **{avg_elo}**  |  Avg WR: **{avg_wr}%**"

    # ── Полоска шансов ───────────────────────────────────────────────────────
    bar1 = _bar(chance1)
    bar2 = _bar(chance2)
    progress = (
        f"🔵 `{bar1}` **{chance1}%**\n"
        f"🔴 `{bar2}` **{chance2}%**"
    )

    # ── Режим (читаемое название) ────────────────────────────────────────────
    mode_names = {
        "team":   "👥 Командный",
        "random": "🎲 Рандомный",
        "cap":    "🎯 Капитанский",
        "pick":   "🎯 Капитанский",
    }
    mode_label = mode_names.get(mode, mode)

    embed = discord.Embed(
        title=f"⚔️  Прогноз матча  ·  Комната #{room_id}  ·  {size}v{size}",
        color=color,
    )
    embed.add_field(
        name=f"🔵 Команда 1  |  {team_stats(team1)}",
        value=player_lines(team1),
        inline=False,
    )
    embed.add_field(
        name=f"🔴 Команда 2  |  {team_stats(team2)}",
        value=player_lines(team2),
        inline=False,
    )
    embed.add_field(
        name="📊 Шанс победы",
        value=progress,
        inline=False,
    )
    embed.set_footer(text=f"Фаворит: {fav}  ·  Режим: {mode_label}")
    return embed
