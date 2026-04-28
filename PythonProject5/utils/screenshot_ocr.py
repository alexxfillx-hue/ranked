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
    import pytesseract
    from PIL import Image
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False
    log.warning("pytesseract / Pillow не установлены.")

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

def _normalize(s: str) -> str:
    """Приводим к нижнему регистру, убираем диакритику и всё кроме букв/цифр/_."""
    s = unicodedata.normalize("NFKD", s.lower())
    return re.sub(r"[^\w]", "", s, flags=re.UNICODE)


# Паттерн тегов вида: [D.3s] alekz, {TAG} nick, (TAG) nick
# Также обрабатывает случаи когда OCR «потерял» открывающую скобку: "D.3s] alekz"
# Примеры:
#   "[D.3s] alekz"  -> "alekz"
#   "D.3s] alekz"   -> "alekz"   (OCR не распознал '[')
#   "{Rove} psykos" -> "psykos"
#   "(CL) nick"     -> "nick"
#   "TAG. nick"     -> "nick"     (тег с точкой без скобок)
#   "Mursal"        -> "Mursal"   (без тега — не трогаем)
_TAG_RE = re.compile(
    r"^(?:"
    r"\[.*?\]"          # [TAG]
    r"|\{.*?\}"         # {TAG}
    r"|\(.*?\)"         # (TAG)
    r"|[^\s]*\]"        # D.3s]  (OCR потерял '[')
    r"|[^\s]*\}"        # D.3s}  (OCR потерял '{')
    r"|[^\s]*\)"        # D.3s)  (OCR потерял '(')
    r")\s*",
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


def _extract_ocr_names(ocr_text: str) -> list[str]:
    """
    Извлекает «чистые» ники из OCR-текста.

    Для каждой строки:
      1. Убираем клановый тег (всё что в [] {} () в начале строки,
         в том числе если OCR не распознал открывающую скобку: "D.3s] alekz").
      2. Добавляем ВСЕ токены строки — ник может стоять не первым словом.
      3. Нормализуем каждый токен.

    Возвращаем список нормализованных кандидатов (без дублей).
    """
    candidates = set()
    for raw_line in ocr_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Убираем тег из начала строки
        clean = _strip_tag(line)
        if not clean:
            continue
        # Добавляем ВСЕ токены строки — ник может быть не первым словом
        # (например, если OCR не убрал тег и строка выглядит как "D.3s alekz 9999")
        tokens = re.split(r"[\s\t]+", clean)
        for tok in tokens:
            tok_norm = _normalize(tok)
            if len(tok_norm) >= 2:
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

    # 3. Расстояние Левенштейна ≤ 1 — только для длинных ников (≥ 5 символов)
    #    и только если кандидат похожей длины, чтобы "test" не совпал с "test2"
    if len(nick_norm) >= 5:
        for cand in ocr_candidates:
            # Разница длин не более 1 символа — иначе это разные слова
            if abs(len(cand) - len(nick_norm)) <= 1 and _levenshtein(nick_norm, cand) <= 1:
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



def _count_player_rows(ocr_text: str) -> int:
    """
    Считает количество строк с игроками в OCR-тексте.

    Признак строки игрока — наличие GS-рейтинга (число вида "N NNN" или "NNNN",
    значение 1000–9999). Заголовки таблицы и пустые строки пропускаются.
    Используется для проверки что скрин содержит ровно N*2 игроков.

    Возвращает -1 если не удалось надёжно посчитать (OCR не нашёл ни одного числа
    похожего на GS-рейтинг) — в этом случае проверка количества пропускается.
    """
    count = 0
    any_number_found = False
    for raw_line in ocr_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Пропускаем строки заголовков таблицы
        header_keywords = ("ИМЯ", "GS", "СЧЁТ", "СЧЕТ", "У/П", "NAME", "SCORE", "K/D", "KDA")
        if any(kw in line.upper() for kw in header_keywords):
            continue
        # Паттерн GS: "9 999" (цифра, пробел, три цифры) или "9999" (4 цифры слитно)
        # Также учитываем OCR-артефакты: неразрывные пробелы (\xa0), точки вместо пробелов
        normalized_line = line.replace("\xa0", " ").replace(".", " ")
        has_gs = (
            re.search(r"\b[1-9]\s\d{3}\b", normalized_line)
            or re.search(r"\b[1-9]\d{3}\b", normalized_line)
        )
        if has_gs:
            any_number_found = True
            count += 1

    # Если ни одного GS-числа не найдено — OCR не смог прочитать таблицу надёжно
    # Возвращаем -1 чтобы _validate_players пропустил эту проверку
    if not any_number_found:
        return -1
    return count


def _validate_players(players: list[dict], matched: list[str], ocr_text: str | None = None) -> Optional[ValidationError]:
    """
    СТРОГАЯ проверка: все игроки комнаты должны быть на скрине.

    Правило одно для всех форматов:
      Формат NvN → на скрине должно быть ровно N*2 игроков, и все N*2 должны совпасть.
      Если хотя бы один не найден — ValidationError.
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

    # Проверяем количество строк игроков на скрине.
    # Скрин должен содержать ровно size*2 строк с игроками — не больше, не меньше.
    # Это защищает от скринов с другим форматом (например, 4v4 скрин при комнате 1v1).
    if ocr_text is not None:
        rows_on_screenshot = _count_player_rows(ocr_text)
        # -1 означает что OCR не смог надёжно посчитать строки — пропускаем проверку
        # 0 тоже означает что паттерны GS не нашлись — пропускаем
        if rows_on_screenshot > 0 and rows_on_screenshot != total_expected:
            return ValidationError(
                reason=(
                    f"❌ Формат {size}v{size}: на скрине обнаружено **{rows_on_screenshot}** строк игроков, "
                    f"а должно быть ровно **{total_expected}**. "
                    f"Это скрин другого матча — загрузи скрин именно этой игры ({size}v{size})."
                ),
                expected_count=total_expected,
                found_count=0,
            )

    # Дополнительная проверка: считаем уникальных игроков найденных в OCR.
    # Если OCR нашёл БОЛЬШЕ игроков чем ожидается в формате — отклоняем скрин.
    # Это защищает от случая когда _count_player_rows не сработал (OCR не распознал GS),
    # но при этом в OCR-тексте реально видно больше ников чем должно быть.
    if len(matched) > total_expected:
        return ValidationError(
            reason=(
                f"❌ Формат {size}v{size}: распознано **{len(matched)}** игроков, "
                f"хотя в этом матче должно быть ровно **{total_expected}**. "
                f"Это скрин другого матча — загрузи скрин именно этой игры ({size}v{size})."
            ),
            found_players=matched,
            missing_players=[],
            expected_count=total_expected,
            found_count=len(matched),
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


def _determine_winner_team(verdict: str, players: list[dict], matched: list[str]) -> tuple[int, str]:
    """
    Определяет победившую команду исходя из вердикта и расположения игроков.

    win_top    → ПОБЕДА стоит выше — значит верхняя команда победила.
    win_bottom → ПОРАЖЕНИЕ стоит выше — значит верхняя команда проиграла.

    «Верхняя» команда = та, чьи игроки идут первыми в списке скрина.
    Мы определяем это по тому, игроки какой команды найдены раньше в OCR-тексте.
    Если определить не получается — используем team1 как «верхнюю» по умолчанию.
    """
    matched_set = set(matched)
    t1_found = [p for p in players if p["team"] == 1 and p["username"] in matched_set]
    t2_found = [p for p in players if p["team"] == 2 and p["username"] in matched_set]

    # После строгой валидации обе команды гарантированно найдены
    # Confidence = high, т.к. все игроки прошли валидацию
    confidence = "high"

    # win_top: слово ПОБЕДА появляется раньше ПОРАЖЕНИЯ → верхняя команда выиграла.
    # Мы считаем team1 «верхней» (они перечислены первыми в players).
    if verdict == "win_top":
        winner_team = 1
    else:
        # win_bottom: ПОРАЖЕНИЕ раньше → верхняя (team1) проиграла
        winner_team = 2

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
        ocr_text = await asyncio.get_event_loop().run_in_executor(None, _run_ocr, image_data)
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

    # 4. Определяем победителя
    winner_team, confidence = _determine_winner_team(verdict, players, matched)
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


def _preprocess_image(img) -> list:
    from PIL import ImageOps, ImageEnhance
    results = []
    w, h = img.size
    img_big = img.resize((w * 2, h * 2), Image.LANCZOS)
    gray = img_big.convert("L")

    results.append(ImageOps.invert(gray))
    results.append(ImageEnhance.Sharpness(ImageEnhance.Contrast(gray).enhance(3.0)).enhance(2.0))
    results.append(gray.point(lambda p: 255 if p > 100 else 0))
    results.append(gray.point(lambda p: 0 if p > 100 else 255))

    top_h   = max(60, h // 4)
    top_big = img.crop((0, 0, w, top_h)).resize((w * 3, top_h * 3), Image.LANCZOS)
    top_gray = top_big.convert("L")
    results.append(ImageOps.invert(top_gray))
    results.append(top_gray)

    return results


def _run_ocr(image_data: bytes) -> str:
    img = Image.open(io.BytesIO(image_data))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")

    configs = [
        r"--oem 3 --psm 6 -l rus+eng",
        r"--oem 3 --psm 3 -l rus+eng",
        r"--oem 3 --psm 11 -l rus+eng",
    ]
    win_keywords = ("ПОБЕДА", "ПОРАЖЕНИЕ", "VICTORY", "DEFEAT")
    all_texts = []

    for variant in _preprocess_image(img):
        for cfg in configs:
            try:
                text = pytesseract.image_to_string(variant, config=cfg)
                if not text.strip():
                    continue
                all_texts.append(text)
                if any(kw in text.upper() for kw in win_keywords):
                    return "\n".join(all_texts)
            except Exception:
                continue

    return "\n".join(all_texts) if all_texts else ""
