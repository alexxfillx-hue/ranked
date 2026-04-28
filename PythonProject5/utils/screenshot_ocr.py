# utils/screenshot_ocr.py
"""
Бесплатный анализ скриншотов результатов игры через Tesseract OCR (pytesseract).

Зависимости (установить один раз на сервере):
    pip install pytesseract pillow aiohttp
    apt-get install -y tesseract-ocr tesseract-ocr-rus   # или через yum/brew

Логика:
    1. Скачиваем изображение по URL вложения Discord.
    2. OCR → получаем текст.
    3. Ищем ПОБЕДА / ПОРАЖЕНИЕ (верхняя команда).
    4. Пробуем сопоставить ники игроков комнаты с никами в скрине
       (с учётом клановых тегов вида [TAG] nick).
    5. Возвращаем ScreenshotResult или None если не удалось распознать.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("bot.ocr")

# ── Ленивая загрузка зависимостей ──────────────────────────────────────────────
try:
    import pytesseract
    from PIL import Image
    _OCR_AVAILABLE = True
except ImportError:
    _OCR_AVAILABLE = False
    log.warning(
        "pytesseract / Pillow не установлены. "
        "OCR-анализ скриншотов недоступен. "
        "Установи: pip install pytesseract pillow && apt-get install tesseract-ocr tesseract-ocr-rus"
    )

try:
    import aiohttp
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False
    log.warning("aiohttp не установлен. Установи: pip install aiohttp")


# ── Результат анализа ───────────────────────────────────────────────────────────

@dataclass
class ScreenshotResult:
    """
    winner_team: 1 или 2 — какая команда (из комнаты) победила.
    confidence:  'high' | 'low' — насколько уверены в результате.
    raw_verdict: 'win_top' | 'win_bottom' — что написано на скрине относительно верхней группы.
    matched_players: список ников игроков, найденных в тексте скрина.
    """
    winner_team: int           # 1 или 2
    confidence: str            # 'high' | 'low'
    raw_verdict: str           # 'win_top' | 'win_bottom'
    matched_players: list[str]


# ── Вспомогательные функции ─────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    """Нижний регистр + убираем диакритику/невидимые символы."""
    s = unicodedata.normalize("NFKD", s.lower())
    return re.sub(r"[^\w]", "", s, flags=re.UNICODE)


def _strip_clan_tag(name: str) -> str:
    """
    '[D.3s] alekz'  →  'alekz'
    '[BANGS] Carnage' → 'Carnage'
    Удаляем всё до и включая ']', если строка начинается с '['.
    """
    return re.sub(r"^\[.*?\]\s*", "", name).strip()


def _find_verdict(text: str) -> Optional[str]:
    """
    Ищем ПОБЕДА / ПОРАЖЕНИЕ в тексте OCR.
    Возвращает:
        'win_top'    — верхняя команда ПОБЕДИЛА
        'win_bottom' — верхняя команда ПРОИГРАЛА (нижняя ПОБЕДИЛА)
        None         — не найдено
    Поддерживаем также возможные OCR-опечатки через нечёткий поиск.
    """
    # Переводим весь текст в верхний регистр для поиска
    upper = text.upper()

    # Прямые совпадения (кириллица + возможные варианты OCR)
    win_patterns = [
        r"ПОБЕДА",
        r"П0БЕДА",   # OCR: О → 0
        r"ПОБЕДА!",
        r"VICTORY",
        r"WIN",
    ]
    lose_patterns = [
        r"ПОРАЖЕНИЕ",
        r"П0РАЖЕНИЕ",
        r"DEFEAT",
        r"LOSS",
        r"LOSE",
    ]

    found_win = any(re.search(p, upper) for p in win_patterns)
    found_lose = any(re.search(p, upper) for p in lose_patterns)

    if found_win and not found_lose:
        return "win_top"
    if found_lose and not found_win:
        return "win_bottom"
    if found_win and found_lose:
        # Оба — пробуем понять по позиции в тексте
        # ПОБЕДА стоит выше ПОРАЖЕНИЯ → это заголовок, верхняя команда победила
        win_pos = min(
            (m.start() for p in win_patterns for m in re.finditer(p, upper)),
            default=9999,
        )
        lose_pos = min(
            (m.start() for p in lose_patterns for m in re.finditer(p, upper)),
            default=9999,
        )
        if win_pos < lose_pos:
            return "win_top"
        return "win_bottom"

    return None


def _match_players(ocr_text: str, players: list[dict]) -> list[str]:
    """
    Ищем ники игроков из комнаты в тексте OCR.
    Учитываем кланы: [TAG] ник в игре == ник в Discord.
    Возвращаем список найденных ников (из базы данных, без тегов).
    """
    matched = []
    # Разбиваем OCR-текст на «слова» из строк таблицы результатов
    # и нормализуем каждую строку
    ocr_lines = [_normalize(line) for line in ocr_text.splitlines() if line.strip()]
    ocr_full = _normalize(ocr_text)

    for p in players:
        nick = p["username"]
        nick_norm = _normalize(nick)

        # Прямое совпадение нормализованного ника
        if nick_norm and nick_norm in ocr_full:
            matched.append(nick)
            continue

        # Частичное совпадение: ник длиной >= 3 и встречается как подстрока
        if len(nick_norm) >= 3 and nick_norm in ocr_full:
            matched.append(nick)
            continue

        # Fuzzy: проверяем каждую строку на близость
        for line in ocr_lines:
            if len(nick_norm) < 3:
                continue
            # Расстояние Левенштейна упрощённое: допускаем 1 ошибку на каждые 5 символов
            if _levenshtein(nick_norm, line) <= max(1, len(nick_norm) // 5):
                matched.append(nick)
                break

    return matched


def _levenshtein(a: str, b: str) -> int:
    """Простая реализация расстояния Левенштейна."""
    if abs(len(a) - len(b)) > 4:
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


def _determine_winner_team(verdict: str, players: list[dict], matched: list[str]) -> tuple[int, str]:
    """
    Определяем какая команда (1 или 2) победила.

    Стратегия:
    1. Если среди matched есть игроки команды 1 и команды 2 — используем позицию
       «верхней» команды (та, у кого больше совпадений в первой половине текста).
    2. Fallback: если просто нашли ПОБЕДА/ПОРАЖЕНИЕ — ориентируемся только на verdict
       и считаем, что верхняя половина скрина = команда 1 (как в игре обычно).

    Возвращает (winner_team, confidence).
    """
    # Считаем игроков по командам среди найденных
    matched_set = set(matched)
    t1_found = [p for p in players if p["team"] == 1 and p["username"] in matched_set]
    t2_found = [p for p in players if p["team"] == 2 and p["username"] in matched_set]

    total_t1 = len([p for p in players if p["team"] == 1])
    total_t2 = len([p for p in players if p["team"] == 2])

    # Если нашли достаточно игроков обеих команд — высокая уверенность
    t1_ratio = len(t1_found) / max(total_t1, 1)
    t2_ratio = len(t2_found) / max(total_t2, 1)

    has_good_match = (t1_ratio >= 0.5 or t2_ratio >= 0.5) and (len(t1_found) + len(t2_found) >= 2)

    # На скриншоте: верхняя половина таблицы = победители, нижняя = проигравшие
    # ПОБЕДА → верхняя группа выиграла
    # ПОРАЖЕНИЕ → верхняя группа проиграла
    if verdict == "win_top":
        # Верхние — победители
        # Если у команды 1 больше совпадений в «победителях» — она наверху
        if t1_found and t2_found:
            # Больше совпадений → эта команда наверху
            if len(t1_found) >= len(t2_found):
                winner_team = 1
            else:
                winner_team = 2
        elif t1_found and not t2_found:
            winner_team = 1  # все найденные — команда 1 → она наверху и победила
        elif t2_found and not t1_found:
            winner_team = 2
        else:
            # Не нашли никого — предполагаем команда 1 наверху (стандартное расположение)
            winner_team = 1
    else:  # win_bottom
        # Верхние — проигравшие, нижние — победители
        if t1_found and t2_found:
            if len(t1_found) >= len(t2_found):
                # Команда 1 наверху → проиграла → победила команда 2
                winner_team = 2
            else:
                winner_team = 1
        elif t1_found and not t2_found:
            winner_team = 2  # команда 1 наверху и проиграла
        elif t2_found and not t1_found:
            winner_team = 1
        else:
            winner_team = 2  # предполагаем команда 1 наверху, проиграла

    confidence = "high" if has_good_match else "low"
    return winner_team, confidence


# ── Основная публичная функция ──────────────────────────────────────────────────

async def analyze_screenshot(
    image_url: str,
    players: list[dict],
) -> Optional[ScreenshotResult]:
    """
    Скачивает изображение по URL и анализирует результат игры.

    players — список dict с ключами 'discord_id', 'username', 'team'.

    Возвращает ScreenshotResult или None если:
    - OCR недоступен (не установлен tesseract)
    - Не удалось скачать изображение
    - Не нашли ПОБЕДА / ПОРАЖЕНИЕ в тексте
    """
    if not _OCR_AVAILABLE or not _AIOHTTP_AVAILABLE:
        return None

    # Скачиваем изображение
    try:
        image_data = await _download_image(image_url)
    except Exception as e:
        log.warning(f"OCR: не удалось скачать изображение: {e}")
        return None

    # Запускаем OCR в executor (блокирующая операция)
    try:
        ocr_text = await asyncio.get_event_loop().run_in_executor(
            None, _run_ocr, image_data
        )
    except Exception as e:
        log.warning(f"OCR: ошибка при распознавании: {e}")
        return None

    if not ocr_text or not ocr_text.strip():
        log.debug("OCR: пустой результат")
        return None

    log.debug(f"OCR raw text (first 300 chars): {ocr_text[:300]!r}")

    # Ищем ПОБЕДА / ПОРАЖЕНИЕ
    verdict = _find_verdict(ocr_text)
    if verdict is None:
        log.debug("OCR: не найдено ПОБЕДА/ПОРАЖЕНИЕ в тексте")
        return None

    # Сопоставляем ники
    matched = _match_players(ocr_text, players)

    # Определяем победителя
    winner_team, confidence = _determine_winner_team(verdict, players, matched)

    result = ScreenshotResult(
        winner_team=winner_team,
        confidence=confidence,
        raw_verdict=verdict,
        matched_players=matched,
    )
    log.info(
        f"OCR result: winner_team={winner_team}, confidence={confidence}, "
        f"verdict={verdict}, matched={matched}"
    )
    return result


async def _download_image(url: str) -> bytes:
    """Скачивает изображение по URL."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.read()


def _preprocess_image(img) -> list:
    """
    Возвращает несколько вариантов предобработки для OCR.
    Скрины игры — тёмный фон, цветной текст → нужна агрессивная обработка.
    """
    from PIL import ImageOps, ImageEnhance

    results = []
    w, h = img.size

    # Масштабируем x2 (Tesseract лучше читает крупный текст)
    img_big = img.resize((w * 2, h * 2), Image.LANCZOS)

    # 1. Инвертированная grayscale (белый текст → чёрный на белом)
    gray = img_big.convert("L")
    inverted = ImageOps.invert(gray)
    results.append(inverted)

    # 2. Grayscale с повышенным контрастом
    enhanced = ImageEnhance.Contrast(gray).enhance(3.0)
    enhanced = ImageEnhance.Sharpness(enhanced).enhance(2.0)
    results.append(enhanced)

    # 3. Бинаризация (порог 100)
    thresh = gray.point(lambda p: 255 if p > 100 else 0)
    results.append(thresh)

    # 4. Инвертированная бинаризация
    thresh_inv = gray.point(lambda p: 0 if p > 100 else 255)
    results.append(thresh_inv)

    # 5. Только верхняя часть x3 (заголовок ПОБЕДА/ПОРАЖЕНИЕ — верхние ~25%)
    top_h = max(60, h // 4)
    top_crop = img.crop((0, 0, w, top_h))
    top_big = top_crop.resize((w * 3, top_h * 3), Image.LANCZOS)
    top_gray = top_big.convert("L")
    top_inv = ImageOps.invert(top_gray)
    results.append(top_inv)
    results.append(top_gray)

    return results


def _run_ocr(image_data: bytes) -> str:
    """
    Синхронный OCR через pytesseract с предобработкой.
    Пробует несколько вариантов изображения и конфигов,
    возвращает объединённый текст. Останавливается раньше
    если уже нашёл ПОБЕДА / ПОРАЖЕНИЕ.
    Запускается в executor чтобы не блокировать event loop.
    """
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
                # Нашли ключевое слово — дальше не нужно
                if any(kw in text.upper() for kw in win_keywords):
                    return "\n".join(all_texts)
            except Exception:
                continue

    return "\n".join(all_texts) if all_texts else ""
