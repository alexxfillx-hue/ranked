# utils/elo.py

# (min_elo, max_elo, win_points, lose_points)
_ZONES = [
    (0, 300, 7, -3),
    (301, 600, 5, -4),
    (601, 800, 3, -5),
    (801, 1000, 2, -6),
]

# Коэффициент формата: размер одной команды → множитель
_FORMAT_MULT = {
    1: 0.7,
    2: 1.0,
    3: 1.05,
    4: 1.2,
}


def _base_points(avg_elo: float, result: str) -> int:
    """Базовые очки исходя из среднего рейтинга команды."""
    avg_elo = max(0.0, min(1000.0, avg_elo))
    for lo, hi, win_pts, lose_pts in _ZONES:
        if lo <= avg_elo <= hi:
            return win_pts if result == "win" else lose_pts
    # выше 1000 — как элита
    return 2 if result == "win" else -6


def calculate_elo(
        team_players: list,
        format_size: int,
        result: str,
) -> int:
    """
    Считает изменение ELO для игрока команды.

    team_players — список игроков команды (dict с ключом 'elo')
    format_size  — размер одной команды (1, 2, 3 или 4)
    result       — 'win' или 'lose'

    Алгоритм:
      1. Средний рейтинг команды
      2. Базовые очки по зоне среднего рейтинга
      3. Умножаем на коэффициент формата
      4. Округляем по стандартным правилам (0.5 → вверх)
      5. Ограничения: победа >= +1, поражение в диапазоне [-7, -1]
    """
    if not team_players:
        return 0

    avg = sum(p["elo"] for p in team_players) / len(team_players)
    base = _base_points(avg, result)
    mult = _FORMAT_MULT.get(format_size, 1.0)

    raw = base * mult

    # Округление по математическим правилам (0.5 → вверх для положительных)
    if raw >= 0:
        change = int(raw + 0.5)
    else:
        change = -int(-raw + 0.5)

    # Жёсткие ограничения
    if result == "win":
        change = max(change, 1)  # не меньше +1 за победу
    else:
        change = min(change, -1)  # не больше -1 (хотя бы -1)
        change = max(change, -7)  # не меньше -7 за поражение

    return change


def team_avg(players: list) -> float:
    """Средний рейтинг списка игроков."""
    if not players:
        return 0.0
    return sum(p["elo"] for p in players) / len(players)


# Алиас — используется в cogs/rooms.py
room_avg = team_avg
