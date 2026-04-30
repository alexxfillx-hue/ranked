# utils/screenshot_ocr.py
"""
Анализ скриншотов результатов игры через Tesseract OCR (pytesseract).

Логика:
    1. Скачиваем изображение по URL вложения Discord.
    2. OCR -> получаем текст.
    3. СТРОГАЯ ВАЛИДАЦИЯ по формату комнаты:
         1v1 → должно быть ровно 2 ника (оба игрока)
         2v2 → должно быть ровно 4 ника (все 4 игрока)
         3v3 → должно быть ровно 6 ников (все 6 игроков)
         4v4 → должно быть ровно 8 ников (все 8 игроков)
       Ники на скрине могут содержать теги вида [TAG] или {TAG} перед именем —
       бот вырезает теги и сравнивает только чистый ник.
       Если хотя бы один ник не найден — скрин не принимается.
    4. Ищем ПОБЕДА / ПОРАЖЕНИЕ -> определяем победителя.
    5. Возвращаем ScreenshotResult, ValidationError, или None.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("bot.ocr")

# Ленивая загрузка зависимостей
try:
    from rapidocr_onnxruntime import RapidOCR
    from PIL import Image
    import numpy as np
    _rapid = RapidOCR()   # singleton — инициализируем один раз при старте
    _OCR_AVAILABLE = True
    log.info("RapidOCR (ONNX) загружен успешно.")
except ImportError as _e:
    _OCR_AVAILABLE = False
    log.warning("rapidocr_onnxruntime / Pillow / numpy не установлены: %s", _e)

try:
    import aiohttp
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False
    log.warning("aiohttp не установлен.")


# ── Результаты ──────────────────────────────────────────────────────────────────

@dataclass
class ScreenshotResult:
    """OCR успешно распознал результат матча."""
    winner_team: int
    confidence: str             # 'high' | 'low'
    raw_verdict: str            # 'win_top' | 'win_bottom'
    matched_players: list[str] = field(default_factory=list)


@dataclass
class ValidationError:
    """Скрин не прошёл валидацию — не те игроки / не тот формат."""
    reason: str
    found_players: list[str] = field(default_factory=list)
    missing_players: list[str] = field(default_factory=list)
    expected_count: int = 0
    found_count: int = 0


@dataclass
class ManualVoteNeeded:
    """
    OCR нашёл всех игроков на скрине, но не смог определить победителя
    (ПОБЕДА/ПОРАЖЕНИЕ не распознаны). Требуется ручное голосование.
    """
    matched_players: list[str] = field(default_factory=list)
    found_count: int = 0
    expected_count: int = 0


# ── Вспомогательные функции ─────────────────────────────────────────────────────

# Кириллица → латиница: гомоглифы (символы выглядят одинаково, но разный Unicode).
# Применяется ДО нормализации, чтобы «2х2» (кир. х = U+0445) == «2x2» (лат. x).
_CYRILLIC_TO_LATIN: dict[int, int] = str.maketrans(
    "аеорсухАВСЕНКМОРТХ",
    "aeopсухАВСЕНКМОРТХ".translate(  # заглушка — строим через явный словарь ниже
        {}
    ),
)
# Строим корректно через явный dict[int, int] (str.maketrans принимает это напрямую)
_CYRILLIC_TO_LATIN = {
    ord("а"): ord("a"),  ord("е"): ord("e"),  ord("о"): ord("o"),
    ord("р"): ord("p"),  ord("с"): ord("c"),  ord("у"): ord("y"),
    ord("х"): ord("x"),  ord("А"): ord("A"),  ord("В"): ord("B"),
    ord("С"): ord("C"),  ord("Е"): ord("E"),  ord("Н"): ord("H"),
    ord("К"): ord("K"),  ord("М"): ord("M"),  ord("О"): ord("O"),
    ord("Р"): ord("P"),  ord("Т"): ord("T"),  ord("Х"): ord("X"),
}

# Типовые OCR-подмены (многосимвольные → сначала, порядок важен).
# Применяются ПОСЛЕ гомоглифной замены (уже всё в латинице).
_OCR_SUBSTITUTIONS: list[tuple[str, str]] = [
    ("sl", "a"),   # Tesseract читает кирилл. «а» как «sl»
    ("s1", "a"),   # ... или «s1» (цифра 1)
    ("cl", "a"),
    ("rn", "m"),
    ("vv", "w"),
    ("li", "h"),
    ("ii", "u"),
]


def _normalize(s: str) -> str:
    """Приводим к нижнему регистру, убираем диакритику и всё кроме букв/цифр/_.

    Шаги:
      1. Гомоглифная замена: кирилл. х/о/с/е/… → лат. x/o/c/e/…
         Это решает проблему «2х2» (кирилл.) vs «2x2» (лат.) в никах.
      2. NFKD-нормализация + lowercase.
      3. OCR-подмены для типичных ошибок Tesseract (sl→a, rn→m и т.д.).
      4. Удаляем всё кроме \w.
    """
    s = s.translate(_CYRILLIC_TO_LATIN)
    s = unicodedata.normalize("NFKD", s.lower())
    for wrong, right in _OCR_SUBSTITUTIONS:
        s = s.replace(wrong, right)
    return re.sub(r"[^\w]", "", s, flags=re.UNICODE)


# Паттерн тегов — оставляем для _strip_tag (используется в других местах)
_TAG_RE = re.compile(
    r"^(?:"
    r"\[.*?\]"
    r"|\{.*?\}"
    r"|\(.*?\)"
    r"|[^\s]*\]"
    r"|[^\s]*\}"
    r"|[^\s]*\)"
    r")\s*",
    re.UNICODE,
)

# Паттерн строки игрока в таблице результатов.
# Структура: [ИКОНКА(мусор)]  [ТЕГ]  НИК  GS_цифра ...
#
# Примеры реального OCR-вывода:
#   "+> [Rove] psykos 9999 620 ..."  -> psykos
#   "~'y [D.3s] alekz 9999 592 ..."  -> alekz
#   "+r [ide] 2x2 575 40 ..."        -> 2x2
#   "ty Focus 506 37 ..."            -> Focus
#   "© TEST2 7392 0 ..."             -> TEST2
#   "Test 7392 0 ..."                -> Test  (иконка не распознана OCR)
#
# Иконка необязательна — OCR иногда не читает символ звания вообще.
# Якорь «пробел + цифра» после ника гарантирует что это строка игрока, а не заголовок.
_PLAYER_LINE_RE = re.compile(
    r"^"
    r"(?:(?:[^\w\u0400-\u04FF\[{(]|\b\w{1,3}\b)*\s+)?"  # иконка-мусор (необязательна)
    r"(?:[\[{(][^\]})]*[\]})]\s*)?"                       # необязательный [TAG]
    r"(\S+)"                                               # НИК (захватываем)
    r"\s+\d",                                              # якорь: пробел + цифра (GS)
    re.UNICODE,
)


def _strip_tag(name: str) -> str:
    """
    Убирает клановый тег из начала имени игрока.
    Применяет паттерн повторно — на случай если тегов несколько
    или OCR склеил несколько слов в одно поле.
    """
    # Применяем паттерн до тех пор пока он что-то убирает (макс. 3 итерации)
    for _ in range(3):
        cleaned = _TAG_RE.sub("", name).strip()
        if cleaned == name:
            break
        name = cleaned
    return name


def _levenshtein(a: str, b: str) -> int:
    """Расстояние Левенштейна. Быстрый отказ при большой разнице длин."""
    if abs(len(a) - len(b)) > 3:
        return 999
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[:]
        dp[0] = i
        for j in range(1, n + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            dp[j] = min(dp[j] + 1, dp[j - 1] + 1, prev[j - 1] + cost)
    return dp[n]


_SERVICE_WORDS = {
    "gs", "имя", "name", "счёт", "счет", "score",
    "убийства", "смерти", "помощь", "kills", "deaths", "assists",
    "kda", "yd", "уп", "yn", "пн", "dl", "nickname",
    "победа", "поражение", "nopakehme", "nobeda",
    "victory", "defeat", "win", "lose", "loss",
    "draw", "ничья", "ctf", "песочница", "sandbox",
    "team", "команда", "rating", "рейтинг", "place", "место",
    "all", "total", "necouhmlia", "necoyhula", "sandboxctf",
}

# Паттерн для «ячеечного» вывода RapidOCR:
# строка содержит только [TAG]ник (без чисел после), а цифры GS идут следующей строкой.
# Захватываем: необязательный тег + токен без пробелов.
_CELL_LINE_RE = re.compile(
    r"^"
    r"(?:[\[{(][^\]})]*[\]})]\s*)?"  # необязательный [TAG]
    r"([^\s\[\]{}\(\)]+)"            # токен-ник
    r"\s*$",                          # до конца строки (никаких цифр!)
    re.UNICODE,
)


def _extract_ocr_names(ocr_text: str) -> list[str]:
    """
    Извлекает «чистые» ники из OCR-текста двумя стратегиями:

    Стратегия A — Tesseract-формат (вся строка игрока в одну линию):
        [иконка] [TAG] НИК GS SCORE ...
        Якорь: пробел + цифра после ника.

    Стратегия B — RapidOCR/ячеечный формат (каждая ячейка — отдельная строка):
        [TAG]НИК          ← строка заканчивается на нике, без цифр
        9640              ← следующая строка — число (GS)
        Признак: следующая строка является целым числом ≥ 100.

    Обе стратегии применяются одновременно, результаты объединяются.
    Минимальная длина ника после нормализации — 2 символа.
    """
    candidates: set[str] = set()
    lines = [ln.strip() for ln in ocr_text.splitlines()]

    for i, line in enumerate(lines):
        if not line:
            continue

        # ── Стратегия A: якорная строка (Tesseract) ──────────────────────────
        m = _PLAYER_LINE_RE.match(line)
        if m:
            tok_norm = _normalize(m.group(1))
            if len(tok_norm) >= 2 and tok_norm not in _SERVICE_WORDS:
                candidates.add(tok_norm)
            continue  # если A сработала — B для этой строки не нужна

        # ── Стратегия B: ячеечный формат (RapidOCR) ──────────────────────────
        # Строка — просто [TAG]ник без чисел.
        # Следующая строка должна быть числом (GS ≥ 100 или любое целое ≥ 0).
        m2 = _CELL_LINE_RE.match(line)
        if m2:
            tok_raw = m2.group(1)
            # Пропускаем строки состоящие только из цифр/точек — это числовые ячейки (GS, score)
            if re.match(r"^[\d.,/]+$", tok_raw):
                continue
            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            next_is_number = bool(re.match(r"^\d+(\.\d+)?$", next_line))
            tok_norm = _normalize(tok_raw)
            if next_is_number and len(tok_norm) >= 2 and tok_norm not in _SERVICE_WORDS:
                candidates.add(tok_norm)

    return list(candidates)


def _nick_found_in_ocr(nick: str, ocr_candidates: list[str], ocr_full_norm: str) -> bool:
    """
    Проверяет, найден ли ник игрока Discord в OCR-тексте.

    Шаги (от строгого к мягкому):
      1. Точное совпадение с одним из «кандидатов» (токенов строк OCR).
         Это защищает от того что "test" найдётся внутри "test2".
      2. Вхождение ника как отдельного слова (word-boundary) в полный нормализованный текст.
      3. Расстояние Левенштейна ≤ 1 (опечатка OCR в 1 символ) только для ников ≥ 5 символов
         и только если кандидат той же длины ± 1 (не допускаем совпадения коротких ников).
    """
    nick_norm = _normalize(nick)
    if not nick_norm or len(nick_norm) < 2:
        return False

    # 1. Точное совпадение с токеном строки (самый надёжный путь)
    if nick_norm in ocr_candidates:
        return True

    # 2. Ник как отдельное слово в полном тексте (word-boundary через regex)
    #    Используем \b-подобный подход: ник должен быть окружён не-словесными символами
    #    или началом/концом строки.
    pattern = r"(?<![a-zA-Z0-9_\u0400-\u04FF])" + re.escape(nick_norm) + r"(?![a-zA-Z0-9_\u0400-\u04FF])"
    if re.search(pattern, ocr_full_norm):
        return True

    # 3. Расстояние Левенштейна ≤ 2 — только для длинных ников (≥ 5 символов)
    #    Порог 2 нужен для OCR-ошибок вида «а» → «sl» (2 символа вместо 1).
    #    Разница длин ограничена 2 символами чтобы не слить короткие разные ники.
    if len(nick_norm) >= 5:
        for cand in ocr_candidates:
            if abs(len(cand) - len(nick_norm)) <= 2 and _levenshtein(nick_norm, cand) <= 2:
                return True

    return False


def _match_players(ocr_text: str, players: list[dict]) -> list[str]:
    """
    Возвращает список ников из players, найденных в ocr_text.
    Теги вида [TAG] игнорируются.
    """
    ocr_candidates = _extract_ocr_names(ocr_text)
    ocr_full_norm  = _normalize(ocr_text)

    matched = []
    for p in players:
        nick = p["username"]
        if _nick_found_in_ocr(nick, ocr_candidates, ocr_full_norm):
            matched.append(nick)

    return matched


def _find_verdict(text: str) -> Optional[str]:
    upper = text.upper()
    win_patterns  = [r"ПОБЕДА", r"П0БЕДА", r"VICTORY", r"\bWIN\b"]
    lose_patterns = [r"ПОРАЖЕНИЕ", r"П0РАЖЕНИЕ", r"DEFEAT", r"\bLOSS\b", r"\bLOSE\b"]

    found_win  = any(re.search(p, upper) for p in win_patterns)
    found_lose = any(re.search(p, upper) for p in lose_patterns)

    if found_win and not found_lose:
        return "win_top"
    if found_lose and not found_win:
        return "win_bottom"
    if found_win and found_lose:
        win_pos  = min(
            (m.start() for p in win_patterns  for m in re.finditer(p, upper)), default=9999
        )
        lose_pos = min(
            (m.start() for p in lose_patterns for m in re.finditer(p, upper)), default=9999
        )
        return "win_top" if win_pos < lose_pos else "win_bottom"
    return None



def _count_nicks_on_screenshot(ocr_text: str, all_known_players: list[dict]) -> int:
    """
    Считает количество ников игроков на скрине, сравнивая OCR-строки со ВСЕМИ
    зарегистрированными игроками комнаты (независимо от команды).

    Алгоритм:
      1. Для каждой строки OCR убираем тег и получаем «чистый» токен.
      2. Проверяем совпадает ли токен (точно или через Левенштейна) с ником
         любого из известных игроков комнаты.
      3. Считаем уникальные совпадения — каждый ник считается только раз.

    Возвращает количество уникальных ников комнаты найденных на скрине.
    Если ни одного совпадения — возвращает 0 (проверку количества пропускаем).
    """
    ocr_candidates = _extract_ocr_names(ocr_text)
    ocr_full_norm = _normalize(ocr_text)

    found_nicks = set()
    for p in all_known_players:
        nick = p["username"]
        if _nick_found_in_ocr(nick, ocr_candidates, ocr_full_norm):
            found_nicks.add(nick)

    return len(found_nicks)


def _validate_players(players: list[dict], matched: list[str], ocr_text: str | None = None) -> Optional[ValidationError]:
    """
    СТРОГАЯ проверка скрина:

    Шаг 1 — Считаем сколько ников из комнаты есть на скрине.
             Если их БОЛЬШЕ чем нужно для формата (size*2) — чужой скрин, отклоняем.
             Если их МЕНЬШЕ — не все игроки найдены, отклоняем.

    Шаг 2 — Проверяем что найдены игроки ОБЕИХ команд.

    Шаг 3 — Проверяем что ВСЕ игроки комнаты присутствуют на скрине.

    Формат NvN → на скрине должно быть ровно N*2 ников этого матча, все совпавшие.
    """
    team1 = [p for p in players if p["team"] == 1]
    team2 = [p for p in players if p["team"] == 2]
    total_expected = len(team1) + len(team2)
    size = len(team1)  # размер одной команды (1, 2, 3 или 4)

    if total_expected == 0:
        return ValidationError(
            reason="Команды в комнате не сформированы.",
            expected_count=0,
            found_count=0,
        )

    # ── Шаг 1: проверка количества ников на скрине ──────────────────────────
    # Считаем сколько ников из этой комнаты OCR нашёл на скрине.
    # Если больше чем нужно → скрин от другого матча (больше игроков).
    # Например: 1v1 комната (2 игрока), но на скрине найдены 3 ника из комнаты
    # (такое возможно если в комнате были все 3 как known_players).
    # Ключевой случай: все игроки комнаты + чужой игрок → matched == total_expected,
    # но _count_nicks_on_screenshot вернёт total_expected т.к. чужой не в списке.
    # Поэтому главная защита — сравнение matched с total_expected СТРОГО (==).
    if ocr_text is not None:
        nicks_found = _count_nicks_on_screenshot(ocr_text, players)
        ocr_candidates = _extract_ocr_names(ocr_text)
        nick_like = [c for c in ocr_candidates if len(c) >= 2]
        total_on_screen = len(nick_like)

        # Считаем сколько из найденных OCR-токенов совпадают с игроками комнаты
        known_norms = {_normalize(p["username"]) for p in players}
        known_on_screen = sum(1 for c in nick_like if c in known_norms)
        unknown_on_screen = total_on_screen - known_on_screen

        # Отклоняем только если нашли НУЛЕВЫХ известных игроков и при этом
        # на экране куча чужих ников — значит скрин явно от другого матча.
        # Если хотя бы один известный игрок распознан, валидацию по количеству
        # пропускаем: OCR-мусор (иконки, теги) легко даёт +1-2 лишних токена.
        if known_on_screen == 0 and unknown_on_screen > total_expected:
            return ValidationError(
                reason=(
                    f"❌ Формат {size}v{size}: на скрине найдено **{total_on_screen}** ников, "
                    f"а в этом матче должно быть ровно **{total_expected}**. "
                    f"Загрузи скрин именно этой игры ({size}v{size})."
                ),
                expected_count=total_expected,
                found_count=nicks_found,
            )

    matched_set = set(matched)
    t1_found = [p for p in team1 if p["username"] in matched_set]
    t2_found = [p for p in team2 if p["username"] in matched_set]
    found_count = len(t1_found) + len(t2_found)

    missing_t1 = [p["username"] for p in team1 if p["username"] not in matched_set]
    missing_t2 = [p["username"] for p in team2 if p["username"] not in matched_set]
    missing_all = missing_t1 + missing_t2

    fmt = f"{size}v{size}"

    # Все игроки одной из команд отсутствуют — явно чужой скрин
    if not t1_found:
        return ValidationError(
            reason=(
                f"❌ Формат {fmt}: на скрине не найдено ни одного игрока из Команды 1.\n"
                f"Не найдены: {', '.join(missing_t1)}.\n"
                f"Это не тот матч — загрузи скрин именно этой игры."
            ),
            found_players=list(matched_set),
            missing_players=missing_all,
            expected_count=total_expected,
            found_count=found_count,
        )
    if not t2_found:
        return ValidationError(
            reason=(
                f"❌ Формат {fmt}: на скрине не найдено ни одного игрока из Команды 2.\n"
                f"Не найдены: {', '.join(missing_t2)}.\n"
                f"Это не тот матч — загрузи скрин именно этой игры."
            ),
            found_players=list(matched_set),
            missing_players=missing_all,
            expected_count=total_expected,
            found_count=found_count,
        )

    # Главное правило: все игроки должны присутствовать на скрине
    if missing_all:
        missing_t1_str = (", ".join(missing_t1)) if missing_t1 else "все найдены"
        missing_t2_str = (", ".join(missing_t2)) if missing_t2 else "все найдены"
        return ValidationError(
            reason=(
                f"❌ Формат {fmt}: на скрине должны быть все {total_expected} игрока.\n"
                f"Найдено: {found_count}/{total_expected}.\n"
                f"Команда 1 — не найдены: {missing_t1_str}\n"
                f"Команда 2 — не найдены: {missing_t2_str}\n"
                f"Убедись, что скрин показывает таблицу результатов именно этого матча."
            ),
            found_players=list(matched_set),
            missing_players=missing_all,
            expected_count=total_expected,
            found_count=found_count,
        )

    # Все найдены — ок
    return None


def _find_team_first_position(team_players: list[dict], ocr_text: str) -> int:
    """
    Возвращает позицию (символьный индекс) первого упоминания любого ника из команды в OCR-тексте.
    Если никто не найден — возвращает 999999.
    Используется для определения какая команда стоит ВЫШЕ в таблице результатов.
    """
    ocr_norm = _normalize(ocr_text)
    earliest = 999999
    for p in team_players:
        nick_norm = _normalize(p["username"])
        if not nick_norm:
            continue
        # Ищем точное вхождение как отдельное слово
        pattern = r"(?<![a-zA-Z0-9_\u0400-\u04FF])" + re.escape(nick_norm) + r"(?![a-zA-Z0-9_\u0400-\u04FF])"
        m = re.search(pattern, ocr_norm)
        if m and m.start() < earliest:
            earliest = m.start()
    return earliest


def _determine_winner_team(verdict: str, players: list[dict], matched: list[str], ocr_text: str = "") -> tuple[int, str]:
    """
    Определяет победившую команду исходя из вердикта и РЕАЛЬНОГО расположения команд на скрине.

    Алгоритм:
      1. Находим позицию первого ника каждой команды в OCR-тексте.
      2. Та команда, чьи ники встречаются раньше — «верхняя» на скрине.
      3. win_top  → верхняя команда победила (ПОБЕДА/VICTORY стоит раньше).
      4. win_bottom → верхняя команда проиграла.

    Если позиции определить невозможно (нет OCR-текста) — используем team1 как «верхнюю».
    """
    matched_set = set(matched)
    t1_found = [p for p in players if p["team"] == 1 and p["username"] in matched_set]
    t2_found = [p for p in players if p["team"] == 2 and p["username"] in matched_set]

    confidence = "high"

    # Определяем какая команда реально стоит выше на скрине
    top_team = 1  # дефолт если не можем определить
    if ocr_text and t1_found and t2_found:
        pos1 = _find_team_first_position(t1_found, ocr_text)
        pos2 = _find_team_first_position(t2_found, ocr_text)
        log.debug(
            "OCR team positions: team1_first=%d team2_first=%d → top_team=%d",
            pos1, pos2, 1 if pos1 <= pos2 else 2,
        )
        if pos1 <= pos2:
            top_team = 1  # Команда 1 стоит выше на скрине
        else:
            top_team = 2  # Команда 2 стоит выше на скрине
    else:
        log.warning(
            "OCR: не удалось определить позиции команд на скрине "
            "(t1_found=%d t2_found=%d ocr_len=%d) — используем team1 как верхнюю",
            len(t1_found), len(t2_found), len(ocr_text),
        )

    bottom_team = 2 if top_team == 1 else 1

    # win_top: ПОБЕДА/VICTORY встретилась РАНЬШЕ чем ПОРАЖЕНИЕ/DEFEAT
    # → верхняя команда победила
    if verdict == "win_top":
        winner_team = top_team
    else:
        # win_bottom: ПОРАЖЕНИЕ встретилось раньше → верхняя проиграла
        winner_team = bottom_team

    log.info(
        "OCR winner determination: verdict=%s top_team=%d bottom_team=%d → winner_team=%d",
        verdict, top_team, bottom_team, winner_team,
    )
    return winner_team, confidence


# ── Основная публичная функция ──────────────────────────────────────────────────

async def analyze_screenshot(
    image_url: str,
    players: list[dict],
) -> "ScreenshotResult | ValidationError | ManualVoteNeeded | None":
    """
    Возвращает:
      ScreenshotResult  — скрин верный, результат определён автоматически
      ValidationError   — скрин не от этой игры (не те / не все игроки)
      ManualVoteNeeded  — все игроки найдены, но ПОБЕДА/ПОРАЖЕНИЕ не распознаны
      None              — OCR недоступен или не смог прочитать изображение
    """
    if not _OCR_AVAILABLE or not _AIOHTTP_AVAILABLE:
        return None

    try:
        image_data = await _download_image(image_url)
    except Exception as e:
        log.warning("OCR: не удалось скачать изображение: %s", e)
        return None

    try:
        import functools
        ocr_text = await asyncio.get_event_loop().run_in_executor(
            None, functools.partial(_run_ocr, image_data, players)
        )
    except Exception as e:
        log.warning("OCR: ошибка при распознавании: %s", e)
        return None

    log.warning("OCR FULL TEXT:\n%s", ocr_text)

    if not ocr_text or not ocr_text.strip():
        return None

    # 1. Ищем ники игроков в OCR-тексте (с игнорированием тегов)
    matched = _match_players(ocr_text, players)
    team_players = [p for p in players if p["team"] in (1, 2)]
    log.info("OCR matched players: %s / expected: %s",
             matched, [p["username"] for p in team_players])

    # 2. Строгая валидация: все ли игроки комнаты есть на скрине
    err = _validate_players(players, matched, ocr_text)
    if err is not None:
        log.info("OCR validation failed: %s (found %d/%d)",
                 err.reason, err.found_count, err.expected_count)
        return err

    # 3. Ищем ПОБЕДА / ПОРАЖЕНИЕ
    verdict = _find_verdict(ocr_text)
    if verdict is None:
        # Все игроки найдены, но результат неясен → ручное голосование
        log.info(
            "OCR: все игроки найдены (%d/%d), но ПОБЕДА/ПОРАЖЕНИЕ не распознаны — запрашиваем голосование",
            len(matched), len(team_players),
        )
        return ManualVoteNeeded(
            matched_players=matched,
            found_count=len(matched),
            expected_count=len(team_players),
        )

    # 4. Определяем победителя (передаём ocr_text для определения позиций команд)
    winner_team, confidence = _determine_winner_team(verdict, players, matched, ocr_text)
    log.info(
        "OCR result: winner_team=%d confidence=%s verdict=%s matched=%s",
        winner_team, confidence, verdict, matched,
    )

    return ScreenshotResult(
        winner_team=winner_team,
        confidence=confidence,
        raw_verdict=verdict,
        matched_players=matched,
    )


# ── Сетевые и OCR утилиты ───────────────────────────────────────────────────────

async def _download_image(url: str) -> bytes:
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.read()


def _preprocess_variants(img: "Image.Image") -> "list[np.ndarray]":
    """
    Возвращает варианты изображения для RapidOCR.

    Только два варианта вместо четырёх — для скорости:
      1. Оригинал x2 — основной, читает тёмный текст на светлом фоне.
      2. Инверт x2 — читает светлый текст на тёмном фоне (типичный игровой UI).

    Шапку отдельно не вырезаем — RapidOCR сам находит текст в любом месте.
    Если первый вариант дал вердикт (ПОБЕДА/ПОРАЖЕНИЕ) — второй не запускается.
    """
    from PIL import ImageOps
    w, h = img.size
    rgb = img.convert("RGB")
    big = rgb.resize((w * 2, h * 2), Image.LANCZOS)
    return [np.array(big), np.array(ImageOps.invert(big))]


def _norm_line(line: str) -> str:
    """
    Нормализует строку для дедупликации между вариантами OCR.

    Убираем клановые теги вида [TAG] / {TAG} и все не-словесные символы,
    чтобы '[0.3s]alekz' и '[D.3s]alekz' оба давали 'alekz' и дедуплицировались.
    """
    # Убираем теги в начале строки (повторно до 3 раз)
    tag_re = re.compile(r"^[\[{(][^\]})]*[\]})]\s*", re.UNICODE)
    s = line.strip()
    for _ in range(3):
        s2 = tag_re.sub("", s)
        if s2 == s:
            break
        s = s2
    return re.sub(r"[^\wЀ-ӿ]", "", s.lower())


def _run_ocr(image_data: bytes, players: "list[dict] | None" = None) -> str:
    """
    Запускает RapidOCR по вариантам предобработки.

    Оптимизации:
    - Только 2 варианта изображения вместо 4 (вдвое быстрее).
    - Дедупликация по нормализованному виду строки — '[0.3s]alekz' и '[D.3s]alekz'
      оба дают 'alekz' после нормализации и второй отбрасывается.
    - Ранний выход после первого варианта если найдены и вердикт и все ники игроков.
    """
    img = Image.open(io.BytesIO(image_data))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    win_keywords = ("ПОБЕДА", "ПОРАЖЕНИЕ", "VICTORY", "DEFEAT")
    all_lines: list = []
    seen_norm: set = set()   # дедупликация по нормализованному содержимому

    # Нормализованные ники игроков для раннего выхода
    player_norms: set = set()
    if players:
        for p in players:
            if p.get("team") in (1, 2):
                n = re.sub(r"[^\w]", "", p["username"].lower())
                if n:
                    player_norms.add(n)

    for variant in _preprocess_variants(img):
        try:
            result, _ = _rapid(variant)
        except Exception as e:
            log.debug("RapidOCR variant error: %s", e)
            continue

        if not result:
            continue

        for item in result:
            text = item[1] if len(item) > 1 else ""
            if not text or not text.strip():
                continue
            line = text.strip()
            norm = _norm_line(line)
            if norm and norm not in seen_norm:
                seen_norm.add(norm)
                all_lines.append(line)

        # Ранний выход: вердикт найден И все ники присутствуют в тексте
        if player_norms:
            combined_norm = " ".join(seen_norm)
            combined_upper = "\n".join(all_lines).upper()
            has_verdict = any(kw in combined_upper for kw in win_keywords)
            all_found = all(
                any(pn in cn for cn in seen_norm)
                for pn in player_norms
            )
            if has_verdict and all_found:
                log.debug("OCR early exit after variant — all players and verdict found")
                break

    return "\n".join(all_lines)
