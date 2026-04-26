# utils/embeds.py
import discord
from config import get_rank


def room_embed(room_id: int, size: int, players, mode: str = "team") -> discord.Embed:
    """
    mode:
        'team'   — игроки сами выбирают команду через !pick
        'random' — бот раскидывает рандомно при заполнении
        'cap'    — капитанский пик
        'pick'   — алиас для 'cap' (обратная совместимость)
    """
    if mode == "pick":
        mode = "cap"

    team1 = [p for p in players if p["team"] == 1]
    team2 = [p for p in players if p["team"] == 2]
    unpicked = [p for p in players if p["team"] == 0]

    cap1 = next((p for p in team1 if p["is_captain"]), None)
    cap2 = next((p for p in team2 if p["is_captain"]), None)

    ready1 = False  # Ready step removed - start is direct
    ready2 = False

    def player_line(p, show_unpicked=False):
        rank, _ = get_rank(p["elo"])
        if show_unpicked:
            return f"• **{p['username']}** — {p['elo']} ({rank})"
        crown = "👑 " if p["is_captain"] else "• "
        return f"{crown}**{p['username']}** — {p['elo']} ({rank})"

    # ── Режим random: пока команды не сформированы — показываем всех в «ожидании» ──
    if mode == "random":
        total = len(players)
        total_slots = size * 2
        color = 0x9B59B6  # фиолетовый

        embed = discord.Embed(
            title=f"🎮  Комната **#{room_id}**  ·  {size}v{size}  ·  🎲 Рандомный",
            color=color,
        )

        if team1 or team2:
            # Команды уже сформированы
            def build_random_team(team, cap_ready, sz):
                lines = [player_line(p) for p in team]
                lines += ["• *Свободное место*"] * (sz - len(team))
                status = "✅ Сформирована"
                return "\n".join(lines), status

            t1_text, t1_status = build_random_team(team1, ready1, size)
            t2_text, t2_status = build_random_team(team2, ready2, size)
            embed.add_field(name=f"🔵 Команда 1  |  {t1_status}", value=t1_text, inline=True)
            embed.add_field(name=f"🔴 Команда 2  |  {t2_status}", value=t2_text, inline=True)
        else:
            # Ждём игроков — все в очереди
            if unpicked:
                waiting_lines = "\n".join(player_line(p) for p in unpicked)
                free = total_slots - len(unpicked)
                if free > 0:
                    waiting_lines += "\n" + "\n".join(["• *Ожидание игрока...*"] * free)
            else:
                waiting_lines = "\n".join(["• *Ожидание игрока...*"] * total_slots)
            embed.add_field(
                name=f"⏳ Ожидание игроков ({total}/{total_slots})",
                value=waiting_lines,
                inline=False,
            )
            embed.description = "🎲 Команды будут распределены **рандомно** после заполнения комнаты."

        embed.set_footer(text=f"Игроков: {total}/{total_slots}")
        return embed

    # ── Режим cap ─────────────────────────────────────────────────
    if mode == "cap":
        total = len(players)
        total_slots = size * 2
        color = 0xE67E22

        embed = discord.Embed(
            title=f"🎮  Комната **#{room_id}**  ·  {size}v{size}  ·  🎯 Капитанский пик",
            color=color,
        )

        if team1 or team2:
            def build_cap_team(team, cap_ready, sz):
                lines = [player_line(p) for p in team]
                lines += ["• *Ожидание пика...*"] * (sz - len(team))
                status = "✅ Сформирована"
                return "\n".join(lines), status

            t1_text, t1_status = build_cap_team(team1, ready1, size)
            t2_text, t2_status = build_cap_team(team2, ready2, size)
            embed.add_field(name=f"🔵 Команда 1  |  {t1_status}", value=t1_text, inline=True)
            embed.add_field(name=f"🔴 Команда 2  |  {t2_status}", value=t2_text, inline=True)

            if unpicked:
                unpicked_lines = "\n".join(player_line(p, show_unpicked=True) for p in unpicked)
                embed.add_field(name="⏳ Не распределены", value=unpicked_lines, inline=False)
        else:
            # Ждём игроков
            if unpicked:
                waiting_lines = "\n".join(player_line(p) for p in unpicked)
                free = total_slots - len(unpicked)
                if free > 0:
                    waiting_lines += "\n" + "\n".join(["• *Ожидание игрока...*"] * free)
            else:
                waiting_lines = "\n".join(["• *Ожидание игрока...*"] * total_slots)
            embed.add_field(
                name=f"⏳ Ожидание игроков ({total}/{total_slots})",
                value=waiting_lines,
                inline=False,
            )
            embed.description = (
                "🎯 Используй `!cap` чтобы стать капитаном (макс. 2) / Use `!cap` to become captain (max 2).\n"
                "Используй `!uncap` чтобы снять роль / `!uncap` to step down.\n"
                "Когда оба капитана выбраны и комната полна — начнётся пик игроков."
            )

        embed.set_footer(text=f"Игроков: {total}/{total_slots}")
        return embed

    # ── Режим team (по умолчанию) — без капитанов ────────────────
    def player_line_team(p):
        rank, _ = get_rank(p["elo"])
        return f"• **{p['username']}** — {p['elo']} ({rank})"

    def build_team(team, sz):
        lines = [player_line_team(p) for p in team]
        free = sz - len(team)
        lines += ["• *Свободное место*"] * free
        status = "✅ Сформирована" if (len(team) == sz) else "⏳ Набор"
        return "\n".join(lines), status

    t1_text, t1_status = build_team(team1, size)
    t2_text, t2_status = build_team(team2, size)

    total = len(players)
    color = 0x5865F2

    embed = discord.Embed(
        title=f"🎮  Комната **#{room_id}**  ·  {size}v{size}  ·  👥 Командный",
        color=color,
    )
    embed.description = (
        "Используй `!pick 1` или `!pick 2` (или кнопки) чтобы выбрать команду.\n"
        "Можно менять команду в любой момент до старта!"
    )
    embed.add_field(name=f"🔵 Команда 1  |  {t1_status}", value=t1_text, inline=True)
    embed.add_field(name=f"🔴 Команда 2  |  {t2_status}", value=t2_text, inline=True)
    embed.set_footer(text=f"Игроков: {total}/{size * 2}")
    return embed


def profile_embed(player, member: discord.Member) -> discord.Embed:
    rank_name, color = get_rank(player["elo"])
    total = player["wins"] + player["losses"] + player["draws"]
    wr = round(player["wins"] / total * 100, 1) if total else 0
    streak = player["win_streak"]

    embed = discord.Embed(
        title=f"📋  {member.display_name}",
        color=color,
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="🏆 Ранг", value=rank_name, inline=True)
    embed.add_field(name="📊 ELO", value=str(player["elo"]), inline=True)
    embed.add_field(name="🎮 Игр", value=str(total), inline=True)
    embed.add_field(name="✅ Победы", value=str(player["wins"]), inline=True)
    embed.add_field(name="❌ Поражения", value=str(player["losses"]), inline=True)
    embed.add_field(name="🤝 Ничьи", value=str(player["draws"]), inline=True)
    embed.add_field(name="📈 Винрейт", value=f"{wr}%", inline=True)
    if streak >= 2:
        embed.add_field(name="🔥 Вин-стрик", value=str(streak), inline=True)
    reports = player["report_count"] if "report_count" in player.keys() else 0
    embed.add_field(name="🚩 Репортов", value=str(reports), inline=True)
    if player["penalty_games"]:
        embed.add_field(
            name="⚠️ Штраф", value=f"Ещё {player['penalty_games']} игр", inline=True
        )
    return embed