# database.py  —  PostgreSQL (asyncpg)
import os
import datetime
import asyncpg
from config import Config
from utils.elo import calculate_elo

_SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    discord_id    BIGINT  PRIMARY KEY,
    username      TEXT    NOT NULL,
    elo           INTEGER DEFAULT 0,
    wins          INTEGER DEFAULT 0,
    losses        INTEGER DEFAULT 0,
    draws         INTEGER DEFAULT 0,
    games_played  INTEGER DEFAULT 0,
    win_streak    INTEGER DEFAULT 0,
    penalty_games INTEGER DEFAULT 0,
    report_count  INTEGER DEFAULT 0,
    lang          TEXT    DEFAULT 'ru',
    registered_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS elo_history (
    id           SERIAL  PRIMARY KEY,
    discord_id   BIGINT  NOT NULL,
    elo_before   INTEGER NOT NULL,
    elo_after    INTEGER NOT NULL,
    change       INTEGER NOT NULL,
    game_id      INTEGER,
    timestamp    TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (discord_id) REFERENCES players(discord_id)
);

CREATE TABLE IF NOT EXISTS rooms (
    room_id          SERIAL  PRIMARY KEY,
    channel_id       BIGINT  UNIQUE,
    size             INTEGER NOT NULL,
    mode             TEXT    DEFAULT 'team',
    status           TEXT    DEFAULT 'waiting',
    created_by       BIGINT  NOT NULL,
    embed_message_id BIGINT,
    pick_turn        INTEGER DEFAULT 1,
    strong_side      INTEGER DEFAULT 0,
    started_at       TIMESTAMP,
    pinged_at        TIMESTAMP,
    created_at       TIMESTAMP DEFAULT NOW(),
    FOREIGN KEY (created_by) REFERENCES players(discord_id)
);

CREATE TABLE IF NOT EXISTS room_players (
    id               SERIAL  PRIMARY KEY,
    room_id          INTEGER NOT NULL,
    discord_id       BIGINT  NOT NULL,
    team             INTEGER NOT NULL DEFAULT 0,
    is_captain       INTEGER DEFAULT 0,
    confirmed_start  INTEGER DEFAULT 0,
    end_vote         TEXT,
    UNIQUE(room_id, discord_id),
    FOREIGN KEY (room_id)    REFERENCES rooms(room_id),
    FOREIGN KEY (discord_id) REFERENCES players(discord_id)
);

CREATE TABLE IF NOT EXISTS room_screenshots (
    room_id          INTEGER NOT NULL,
    team             INTEGER NOT NULL,
    uploader_id      BIGINT  NOT NULL,
    uploaded_at      TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (room_id, team)
);

CREATE TABLE IF NOT EXISTS reports (
    id          SERIAL  PRIMARY KEY,
    reporter_id BIGINT  NOT NULL,
    reported_id BIGINT  NOT NULL,
    reason      TEXT    NOT NULL,
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS game_results (
    id           SERIAL    PRIMARY KEY,
    game_id      INTEGER   NOT NULL,
    discord_id   BIGINT    NOT NULL REFERENCES players(discord_id),
    opponent_id  BIGINT    NOT NULL REFERENCES players(discord_id),
    result       TEXT      NOT NULL,  -- 'win' | 'lose' | 'draw'
    played_at    TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_game_results_discord ON game_results(discord_id);
CREATE INDEX IF NOT EXISTS idx_game_results_opponent ON game_results(opponent_id);

CREATE TABLE IF NOT EXISTS match_results (
    id                 SERIAL    PRIMARY KEY,
    game_id            INTEGER   NOT NULL UNIQUE,
    winner_team        INTEGER   NOT NULL,   -- 1 или 2, 0 = ничья
    mode               TEXT,
    size               INTEGER,
    result_message_id  BIGINT,              -- id embed-сообщения в канале результатов
    result_channel_id  BIGINT,              -- id канала результатов
    played_at          TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_match_results_game ON match_results(game_id);

CREATE TABLE IF NOT EXISTS teammate_results (
    game_id      INTEGER NOT NULL,
    discord_id   BIGINT  NOT NULL REFERENCES players(discord_id),
    teammate_id  BIGINT  NOT NULL REFERENCES players(discord_id),
    result       TEXT    NOT NULL,  -- 'win' | 'lose' | 'draw'
    PRIMARY KEY (game_id, discord_id, teammate_id)
);
CREATE INDEX IF NOT EXISTS idx_teammate_discord ON teammate_results(discord_id);
CREATE INDEX IF NOT EXISTS idx_teammate_teammate ON teammate_results(teammate_id);

CREATE TABLE IF NOT EXISTS bans (
    discord_id   BIGINT    PRIMARY KEY REFERENCES players(discord_id),
    banned_until TIMESTAMP NOT NULL,
    banned_by    BIGINT    NOT NULL,
    duration_raw TEXT      NOT NULL,
    created_at   TIMESTAMP DEFAULT NOW()
);
"""


class _Row(dict):
    """Лёгкая обёртка над dict — позволяет обращаться к полям и через [] и через .keys()."""
    def __getitem__(self, key):
        return super().__getitem__(key)

    def keys(self):
        return super().keys()


def _rows(records) -> list[_Row]:
    if not records:
        return []
    return [_Row(r) for r in records]


def _row(record) -> _Row | None:
    if record is None:
        return None
    return _Row(record)


class Database:
    def __init__(self):
        self._dsn: str = os.getenv("DATABASE_URL", "")
        self._pool: asyncpg.Pool | None = None

    async def init(self):
        """Создаёт пул соединений и инициализирует схему БД."""
        dsn = self._dsn
        # Railway иногда даёт URL с префиксом postgres:// — asyncpg требует postgresql://
        if dsn.startswith("postgres://"):
            dsn = "postgresql://" + dsn[len("postgres://"):]

        self._pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)

        # Создаём таблицы если их нет
        async with self._pool.acquire() as conn:
            await conn.execute(_SCHEMA)
            # Миграции: добавляем новые колонки если их нет (безопасно для старых данных)
            await conn.execute(
                "ALTER TABLE elo_history ADD COLUMN IF NOT EXISTS mode TEXT"
            )
            await conn.execute(
                "ALTER TABLE elo_history ADD COLUMN IF NOT EXISTS size INTEGER"
            )
            await conn.execute(
                "ALTER TABLE elo_history ADD COLUMN IF NOT EXISTS result TEXT"
            )
            await conn.execute(
                "ALTER TABLE rooms ADD COLUMN IF NOT EXISTS strong_side INTEGER DEFAULT 0"
            )
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS match_results (
                    id                 SERIAL    PRIMARY KEY,
                    game_id            INTEGER   NOT NULL UNIQUE,
                    winner_team        INTEGER   NOT NULL,
                    mode               TEXT,
                    size               INTEGER,
                    result_message_id  BIGINT,
                    result_channel_id  BIGINT,
                    played_at          TIMESTAMP DEFAULT NOW()
                )"""
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_match_results_game ON match_results(game_id)"
            )
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS teammate_results (
                    game_id      INTEGER NOT NULL,
                    discord_id   BIGINT  NOT NULL REFERENCES players(discord_id),
                    teammate_id  BIGINT  NOT NULL REFERENCES players(discord_id),
                    result       TEXT    NOT NULL,
                    PRIMARY KEY (game_id, discord_id, teammate_id)
                )"""
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_teammate_discord ON teammate_results(discord_id)"
            )
            await conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_teammate_teammate ON teammate_results(teammate_id)"
            )
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS bans (
                    discord_id   BIGINT    PRIMARY KEY REFERENCES players(discord_id),
                    banned_until TIMESTAMP NOT NULL,
                    banned_by    BIGINT    NOT NULL,
                    duration_raw TEXT      NOT NULL,
                    created_at   TIMESTAMP DEFAULT NOW()
                )"""
            )
            # Миграция: колонка is_bet (добавлена системой ставок)
            await conn.execute(
                "ALTER TABLE elo_history ADD COLUMN IF NOT EXISTS is_bet BOOLEAN DEFAULT FALSE"
            )
            # Миграция: помечаем старые строки ставок (is_bet IS NULL/FALSE,
            # но игрок не является участником матча в game_results с тем же game_id)
            await conn.execute(
                """
                UPDATE elo_history eh
                SET is_bet = TRUE
                WHERE eh.game_id IS NOT NULL
                  AND COALESCE(eh.is_bet, FALSE) = FALSE
                  AND NOT EXISTS (
                      SELECT 1 FROM game_results gr
                      WHERE gr.discord_id = eh.discord_id
                        AND gr.game_id    = eh.game_id
                  )
                """
            )
            # Одноразовая миграция: пересчитываем wins/losses/draws/games_played.
            # Выполняется только если таблица-флаг ещё не содержит эту запись.
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS _migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT NOW()
                )
                """
            )
            already = await conn.fetchval(
                "SELECT 1 FROM _migrations WHERE name = 'recalc_wins_from_game_results'"
            )
            if not already:
                await conn.execute(
                    """
                    UPDATE players p
                    SET
                        wins         = s.wins,
                        losses       = s.losses,
                        draws        = s.draws,
                        games_played = s.wins + s.losses + s.draws
                    FROM (
                        SELECT
                            discord_id,
                            COUNT(DISTINCT game_id) FILTER (WHERE result = 'win')  AS wins,
                            COUNT(DISTINCT game_id) FILTER (WHERE result = 'lose') AS losses,
                            COUNT(DISTINCT game_id) FILTER (WHERE result = 'draw') AS draws
                        FROM game_results
                        GROUP BY discord_id
                    ) s
                    WHERE p.discord_id = s.discord_id
                    """
                )
                await conn.execute(
                    "INSERT INTO _migrations (name) VALUES ('recalc_wins_from_game_results')"
                )

    @property
    def pool(self) -> asyncpg.Pool:
        assert self._pool is not None, "Database.init() не был вызван"
        return self._pool

    # ──────────────────────── Players ────────────────────────

    async def register(self, discord_id: int, username: str) -> bool:
        try:
            await self.pool.execute(
                "INSERT INTO players (discord_id, username, elo) VALUES ($1,$2,$3)",
                discord_id, username, Config.STARTING_ELO,
            )
            return True
        except asyncpg.UniqueViolationError:
            return False

    async def get_player(self, discord_id: int) -> _Row | None:
        r = await self.pool.fetchrow(
            "SELECT * FROM players WHERE discord_id=$1", discord_id
        )
        return _row(r)

    async def delete_player(self, discord_id: int) -> bool:
        """
        Полностью удаляет игрока из БД.
        Порядок важен: сначала зависимые таблицы, потом players.
        Возвращает True если игрок был найден и удалён, False если не найден.
        """
        async with self.pool.acquire() as conn:
            player = await conn.fetchrow("SELECT 1 FROM players WHERE discord_id=$1", discord_id)
            if not player:
                return False
            await conn.execute("DELETE FROM bans             WHERE discord_id=$1", discord_id)
            await conn.execute("DELETE FROM room_players     WHERE discord_id=$1", discord_id)
            # Удаляем комнаты созданные игроком (room_players других участников этих комнат тоже)
            room_ids = await conn.fetch("SELECT room_id FROM rooms WHERE created_by=$1", discord_id)
            for row in room_ids:
                await conn.execute("DELETE FROM room_players      WHERE room_id=$1", row["room_id"])
                await conn.execute("DELETE FROM room_screenshots  WHERE room_id=$1", row["room_id"])
            await conn.execute("DELETE FROM rooms            WHERE created_by=$1", discord_id)
            await conn.execute("DELETE FROM elo_history      WHERE discord_id=$1", discord_id)
            await conn.execute("DELETE FROM teammate_results WHERE discord_id=$1 OR teammate_id=$1", discord_id)
            await conn.execute("DELETE FROM game_results     WHERE discord_id=$1 OR opponent_id=$1", discord_id)
            await conn.execute("DELETE FROM reports          WHERE reporter_id=$1 OR reported_id=$1", discord_id)
            await conn.execute("DELETE FROM players          WHERE discord_id=$1", discord_id)
            return True

    async def get_player_by_username(self, username: str) -> "_Row | None":
        r = await self.pool.fetchrow(
            "SELECT * FROM players WHERE LOWER(username)=LOWER($1)", username
        )
        return _row(r)

    async def update_username(self, discord_id: int, new_username: str):
        await self.pool.execute(
            "UPDATE players SET username=$1 WHERE discord_id=$2",
            new_username, discord_id,
        )

    async def get_lang(self, discord_id: int) -> str:
        row = await self.pool.fetchrow(
            "SELECT lang FROM players WHERE discord_id=$1", discord_id
        )
        return row["lang"] if row and row["lang"] else "ru"

    async def set_lang(self, discord_id: int, lang: str):
        await self.pool.execute(
            "UPDATE players SET lang=$1 WHERE discord_id=$2", lang, discord_id
        )

    async def get_top(self, limit: int = 10) -> list[_Row]:
        rows = await self.pool.fetch(
            "SELECT * FROM players ORDER BY elo DESC LIMIT $1", limit
        )
        return _rows(rows)

    async def get_all_players_ranked(self) -> list[_Row]:
        """Все игроки отсортированные по ELO — для пагинации лидерборда."""
        rows = await self.pool.fetch(
            "SELECT * FROM players ORDER BY elo DESC"
        )
        return _rows(rows)

    async def update_after_game(
        self,
        discord_id: int,
        new_elo: int,
        result: str,   # 'win' | 'lose' | 'draw'
        game_id: int,
        mode: str = None,
        size: int = None,
    ):
        pl = await self.get_player(discord_id)
        if not pl:
            return
        elo_before = pl["elo"]

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if result == "win":
                    await conn.execute(
                        """UPDATE players
                           SET elo=$1, wins=wins+1, games_played=games_played+1,
                               win_streak=win_streak+1,
                               penalty_games=GREATEST(0,penalty_games-1)
                           WHERE discord_id=$2""",
                        new_elo, discord_id,
                    )
                elif result == "lose":
                    await conn.execute(
                        """UPDATE players
                           SET elo=$1, losses=losses+1, games_played=games_played+1,
                               win_streak=0,
                               penalty_games=GREATEST(0,penalty_games-1)
                           WHERE discord_id=$2""",
                        new_elo, discord_id,
                    )
                else:  # draw
                    await conn.execute(
                        """UPDATE players
                           SET elo=$1, draws=draws+1, games_played=games_played+1,
                               win_streak=0,
                               penalty_games=GREATEST(0,penalty_games-1)
                           WHERE discord_id=$2""",
                        new_elo, discord_id,
                    )
                await conn.execute(
                    """INSERT INTO elo_history
                       (discord_id, elo_before, elo_after, change, game_id, mode, size)
                       VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                    discord_id, elo_before, new_elo, new_elo - elo_before, game_id, mode, size,
                )

    async def apply_penalty(self, discord_id: int):
        await self.pool.execute(
            "UPDATE players SET penalty_games=penalty_games+3 WHERE discord_id=$1",
            discord_id,
        )

    # ──────────────────────── Bans ────────────────────────

    async def set_ban(
        self,
        discord_id: int,
        banned_until: datetime.datetime,
        banned_by: int,
        duration_raw: str,
    ):
        """Устанавливает или обновляет бан игрока."""
        await self.pool.execute(
            """INSERT INTO bans (discord_id, banned_until, banned_by, duration_raw)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (discord_id) DO UPDATE
               SET banned_until=$2, banned_by=$3, duration_raw=$4, created_at=NOW()""",
            discord_id, banned_until, banned_by, duration_raw,
        )

    async def get_ban(self, discord_id: int) -> "_Row | None":
        """Возвращает запись бана или None если игрок не забанен."""
        r = await self.pool.fetchrow(
            "SELECT * FROM bans WHERE discord_id=$1", discord_id
        )
        return _row(r)

    async def remove_ban(self, discord_id: int):
        """Снимает бан игрока."""
        await self.pool.execute(
            "DELETE FROM bans WHERE discord_id=$1", discord_id
        )

    async def get_expired_bans(self) -> list["_Row"]:
        """Возвращает все баны у которых истёк срок."""
        rows = await self.pool.fetch(
            "SELECT * FROM bans WHERE banned_until <= $1",
            datetime.datetime.utcnow(),
        )
        return _rows(rows)

    async def get_elo_history(
        self, discord_id: int, since: datetime.datetime | None
    ) -> list[_Row]:
        if since:
            rows = await self.pool.fetch(
                """SELECT * FROM elo_history
                   WHERE discord_id=$1 AND timestamp>=$2
                   ORDER BY timestamp ASC""",
                discord_id, since,
            )
        else:
            rows = await self.pool.fetch(
                "SELECT * FROM elo_history WHERE discord_id=$1 ORDER BY timestamp ASC",
                discord_id,
            )
        return _rows(rows)

    # ──────────────────────── Rooms ────────────────────────

    async def create_room(
        self, channel_id: int, size: int, creator_id: int, mode: str = "team"
    ) -> int:
        row = await self.pool.fetchrow(
            """INSERT INTO rooms (channel_id, size, mode, created_by)
               VALUES ($1,$2,$3,$4) RETURNING room_id""",
            channel_id or None, size, mode, creator_id,
        )
        return row["room_id"]

    async def update_channel_id(self, room_id: int, channel_id: int):
        await self.pool.execute(
            "UPDATE rooms SET channel_id=$1 WHERE room_id=$2",
            channel_id, room_id,
        )

    async def get_room(self, room_id: int) -> _Row | None:
        r = await self.pool.fetchrow(
            "SELECT * FROM rooms WHERE room_id=$1", room_id
        )
        return _row(r)

    async def get_room_by_channel(self, channel_id: int) -> _Row | None:
        r = await self.pool.fetchrow(
            "SELECT * FROM rooms WHERE channel_id=$1", channel_id
        )
        return _row(r)

    async def get_player_room(self, discord_id: int) -> _Row | None:
        r = await self.pool.fetchrow(
            """SELECT r.* FROM rooms r
               JOIN room_players rp ON r.room_id=rp.room_id
               WHERE rp.discord_id=$1
                 AND r.status IN ('waiting','full','started','picking','awaiting_screenshot')""",
            discord_id,
        )
        return _row(r)

    async def get_room_players(self, room_id: int) -> list[_Row]:
        rows = await self.pool.fetch(
            """SELECT rp.*, p.username, p.elo
               FROM room_players rp
               JOIN players p ON rp.discord_id=p.discord_id
               WHERE rp.room_id=$1
               ORDER BY rp.team, rp.is_captain DESC""",
            room_id,
        )
        return _rows(rows)

    async def add_to_room(
        self, room_id: int, discord_id: int, team: int, is_captain: bool = False
    ):
        await self.pool.execute(
            """INSERT INTO room_players (room_id, discord_id, team, is_captain)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (room_id, discord_id) DO NOTHING""",
            room_id, discord_id, team, int(is_captain),
        )

    async def remove_from_room(self, room_id: int, discord_id: int):
        await self.pool.execute(
            "DELETE FROM room_players WHERE room_id=$1 AND discord_id=$2",
            room_id, discord_id,
        )

    async def set_captain(self, room_id: int, discord_id: int, value: bool):
        await self.pool.execute(
            "UPDATE room_players SET is_captain=$1 WHERE room_id=$2 AND discord_id=$3",
            int(value), room_id, discord_id,
        )

    async def set_player_team(self, room_id: int, discord_id: int, team: int):
        await self.pool.execute(
            "UPDATE room_players SET team=$1 WHERE room_id=$2 AND discord_id=$3",
            team, room_id, discord_id,
        )

    async def set_pick_turn(self, room_id: int, turn: int):
        await self.pool.execute(
            "UPDATE rooms SET pick_turn=$1 WHERE room_id=$2", turn, room_id
        )

    async def set_strong_side(self, room_id: int, team: int):
        await self.pool.execute(
            "UPDATE rooms SET strong_side=$1 WHERE room_id=$2", team, room_id
        )

    async def set_ready(self, room_id: int, team: int, value: bool):
        await self.pool.execute(
            """UPDATE room_players SET confirmed_start=$1
               WHERE room_id=$2 AND team=$3 AND is_captain=1""",
            int(value), room_id, team,
        )

    async def set_start_confirm(self, room_id: int, discord_id: int):
        await self.pool.execute(
            "UPDATE room_players SET confirmed_start=1 WHERE room_id=$1 AND discord_id=$2",
            room_id, discord_id,
        )

    async def set_end_vote(self, room_id: int, discord_id: int, vote):
        await self.pool.execute(
            "UPDATE room_players SET end_vote=$1 WHERE room_id=$2 AND discord_id=$3",
            vote, room_id, discord_id,
        )

    async def update_room_status(self, room_id: int, status: str):
        if status == "started":
            await self.pool.execute(
                "UPDATE rooms SET status=$1, started_at=NOW() WHERE room_id=$2",
                status, room_id,
            )
        else:
            await self.pool.execute(
                "UPDATE rooms SET status=$1 WHERE room_id=$2", status, room_id
            )

    async def update_embed_id(self, room_id: int, msg_id: int):
        await self.pool.execute(
            "UPDATE rooms SET embed_message_id=$1 WHERE room_id=$2", msg_id, room_id
        )

    async def set_pinged(self, room_id: int):
        await self.pool.execute(
            "UPDATE rooms SET pinged_at=NOW() WHERE room_id=$1", room_id
        )

    async def try_finalize_room(self, room_id: int) -> bool:
        """
        Атомарно переводит комнату из статуса 'started' → 'finalizing'.
        Возвращает True только если переход успешен (защита от двойного ELO).
        """
        result = await self.pool.execute(
            "UPDATE rooms SET status='finalizing' WHERE room_id=$1 AND status='started'",
            room_id,
        )
        # asyncpg возвращает строку вида 'UPDATE 1' или 'UPDATE 0'
        return result.endswith("1")

    async def delete_room(self, room_id: int):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM room_players WHERE room_id=$1", room_id
                )
                await conn.execute(
                    "DELETE FROM rooms WHERE room_id=$1", room_id
                )

    async def get_open_rooms(self) -> list[_Row]:
        rows = await self.pool.fetch(
            "SELECT * FROM rooms WHERE status='waiting' ORDER BY created_at ASC"
        )
        return _rows(rows)

    async def get_all_active_rooms(self) -> list[_Row]:
        """Все активные комнаты для отображения в лобби."""
        rows = await self.pool.fetch(
            """SELECT * FROM rooms
               WHERE status IN ('waiting', 'full', 'picking', 'started', 'awaiting_screenshot')
               ORDER BY created_at ASC"""
        )
        return _rows(rows)

    async def get_available_rooms(
        self, size: int | None = None, mode: str | None = None
    ) -> list[_Row]:
        conditions = ["status='waiting'"]
        params: list = []
        i = 1
        if size:
            conditions.append(f"size=${i}")
            params.append(size)
            i += 1
        if mode:
            conditions.append(f"mode=${i}")
            params.append(mode)
        where = " AND ".join(conditions)
        rows = await self.pool.fetch(f"SELECT * FROM rooms WHERE {where}", *params)
        return _rows(rows)

    async def get_started_rooms(self) -> list[_Row]:
        rows = await self.pool.fetch(
            "SELECT * FROM rooms WHERE status='started'"
        )
        return _rows(rows)

    # ──────────────────────── Reports ────────────────────────

    async def add_report(self, reporter_id: int, reported_id: int, reason: str):
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO reports (reporter_id,reported_id,reason) VALUES ($1,$2,$3)",
                    reporter_id, reported_id, reason,
                )
                await conn.execute(
                    "UPDATE players SET report_count=report_count+1 WHERE discord_id=$1",
                    reported_id,
                )

    async def get_report_count(self, discord_id: int) -> int:
        row = await self.pool.fetchrow(
            "SELECT report_count FROM players WHERE discord_id=$1", discord_id
        )
        return row["report_count"] if row else 0

    async def mod_adjust_elo(self, discord_id: int, amount: int) -> int:
        pl = await self.get_player(discord_id)
        if not pl:
            return -1
        new_elo = max(0, pl["elo"] + amount)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE players SET elo=$1 WHERE discord_id=$2",
                    new_elo, discord_id,
                )
                await conn.execute(
                    """INSERT INTO elo_history (discord_id, elo_before, elo_after, change)
                       VALUES ($1,$2,$3,$4)""",
                    discord_id, pl["elo"], new_elo, amount,
                )
        return new_elo

    async def reports_today(self, reporter_id: int) -> int:
        row = await self.pool.fetchrow(
            """SELECT COUNT(*) AS cnt FROM reports
               WHERE reporter_id=$1 AND DATE(created_at)=CURRENT_DATE""",
            reporter_id,
        )
        return row["cnt"] if row else 0

    async def already_reported(self, reporter_id: int, reported_id: int) -> bool:
        row = await self.pool.fetchrow(
            "SELECT 1 FROM reports WHERE reporter_id=$1 AND reported_id=$2",
            reporter_id, reported_id,
        )
        return row is not None

    # ──────────────────────── ELO leave penalty ────────────────────

    async def deduct_elo_for_leave(self, discord_id: int, amount: int = 15) -> int:
        pl = await self.get_player(discord_id)
        if not pl:
            return 0
        new_elo = max(0, pl["elo"] - amount)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE players SET elo=$1 WHERE discord_id=$2",
                    new_elo, discord_id,
                )
                await conn.execute(
                    """INSERT INTO elo_history (discord_id, elo_before, elo_after, change)
                       VALUES ($1,$2,$3,$4)""",
                    discord_id, pl["elo"], new_elo, -amount,
                )
        return new_elo

    # ──────────────────────── Screenshots ────────────────────────

    async def add_screenshot(self, room_id: int, team: int, uploader_id: int):
        await self.pool.execute(
            """INSERT INTO room_screenshots (room_id, team, uploader_id)
               VALUES ($1,$2,$3)
               ON CONFLICT (room_id, team) DO UPDATE SET uploader_id=$3, uploaded_at=NOW()""",
            room_id, team, uploader_id,
        )

    async def get_screenshots(self, room_id: int) -> list[_Row]:
        rows = await self.pool.fetch(
            "SELECT * FROM room_screenshots WHERE room_id=$1", room_id
        )
        return _rows(rows)

    async def delete_screenshots(self, room_id: int):
        await self.pool.execute(
            "DELETE FROM room_screenshots WHERE room_id=$1", room_id
        )
    async def save_game_results(self, game_id: int, team1: list, team2: list, result: str):
        """
        Сохраняет парные результаты для каждого игрока относительно каждого оппонента,
        а также данные о тиммейтах (для !stat тандемы/трио).
        result — результат для команды 1: 'win'|'lose'|'draw'
        """
        rows = []
        # team1 против team2
        for p1 in team1:
            for p2 in team2:
                r1 = result
                r2 = "lose" if result == "win" else ("win" if result == "lose" else "draw")
                rows.append((game_id, p1["discord_id"], p2["discord_id"], r1))
                rows.append((game_id, p2["discord_id"], p1["discord_id"], r2))
        if not rows:
            return

        async with self.pool.acquire() as conn:
            await conn.executemany(
                "INSERT INTO game_results (game_id, discord_id, opponent_id, result) VALUES ($1,$2,$3,$4)",
                rows,
            )

    async def get_elo_history_simple(self, discord_id: int) -> list[_Row]:
        """
        Возвращает историю всех игр для !streak.
        Читает из elo_history — там есть ВСЕ игры включая сыгранные до обновления.
        Для прошлых игр mode/size = NULL (отображаем как '?').
        result выводится из знака change: >0 = win, <0 = lose, 0 = draw.
        """
        rows = await self.pool.fetch(
            """SELECT id, game_id, elo_before, elo_after, change, mode, size, timestamp,
                      COALESCE(is_bet, FALSE) AS is_bet
               FROM elo_history
               WHERE discord_id=$1
               ORDER BY timestamp ASC, id ASC""",
            discord_id,
        )
        return _rows(rows)

    async def get_stat_vs_players(self, discord_id: int) -> list[_Row]:
        """
        Возвращает статистику побед/поражений/ничьих против каждого оппонента.
        Сортировка: сначала по wins DESC, затем по losses DESC.
        """
        rows = await self.pool.fetch(
            """SELECT
                gr.opponent_id,
                p.username,
                COUNT(*) FILTER (WHERE gr.result='win')  AS wins,
                COUNT(*) FILTER (WHERE gr.result='lose') AS losses,
                COUNT(*) FILTER (WHERE gr.result='draw') AS draws,
                COUNT(*) AS total
               FROM game_results gr
               JOIN players p ON p.discord_id = gr.opponent_id
               WHERE gr.discord_id = $1
               GROUP BY gr.opponent_id, p.username
               ORDER BY wins DESC, losses DESC""",
            discord_id,
        )
        return _rows(rows)

    async def get_teammate_stats(self, discord_id: int) -> list[_Row]:
        """
        Статистика с тиммейтами — восстанавливается из game_results.
        Два игрока тиммейты если у них одинаковый game_id И одинаковый result.
        Используем DISTINCT game_id чтобы не множить на количество оппонентов.
        """
        rows = await self.pool.fetch(
            """SELECT
                teammate_id,
                username,
                COUNT(*) FILTER (WHERE result = 'win')  AS wins,
                COUNT(*) FILTER (WHERE result = 'lose') AS losses,
                COUNT(*) FILTER (WHERE result = 'draw') AS draws,
                COUNT(*)                                 AS total
               FROM (
                   SELECT DISTINCT g1.game_id, g2.discord_id AS teammate_id, p.username, g1.result
                   FROM game_results g1
                   JOIN game_results g2
                     ON g1.game_id = g2.game_id
                    AND g1.result  = g2.result
                    AND g2.discord_id != $1
                   JOIN players p ON p.discord_id = g2.discord_id
                   WHERE g1.discord_id = $1
               ) sub
               GROUP BY teammate_id, username
               ORDER BY wins DESC, total DESC""",
            discord_id,
        )
        return _rows(rows)

    async def get_trio_stats(self, discord_id: int) -> list[_Row]:
        """
        Лучшее трио — два тиммейта с которыми вместе больше всего побед.
        DISTINCT game_id чтобы не множить на количество оппонентов.
        """
        rows = await self.pool.fetch(
            """SELECT
                teammate1_id,
                teammate1_name,
                teammate2_id,
                teammate2_name,
                COUNT(*) FILTER (WHERE result = 'win') AS wins,
                COUNT(*)                                AS total
               FROM (
                   SELECT DISTINCT
                       g1.game_id,
                       g1.result,
                       g2.discord_id AS teammate1_id,
                       p1.username   AS teammate1_name,
                       g3.discord_id AS teammate2_id,
                       p2.username   AS teammate2_name
                   FROM game_results g1
                   JOIN game_results g2
                     ON g1.game_id = g2.game_id
                    AND g1.result  = g2.result
                    AND g2.discord_id != $1
                   JOIN game_results g3
                     ON g1.game_id = g3.game_id
                    AND g1.result  = g3.result
                    AND g3.discord_id != $1
                    AND g3.discord_id > g2.discord_id
                   JOIN players p1 ON p1.discord_id = g2.discord_id
                   JOIN players p2 ON p2.discord_id = g3.discord_id
                   WHERE g1.discord_id = $1
               ) sub
               GROUP BY teammate1_id, teammate1_name, teammate2_id, teammate2_name
               ORDER BY wins DESC, total DESC
               LIMIT 3""",
            discord_id,
        )
        return _rows(rows)

    # ──────────────────────── Match Results ────────────────────────

    async def save_match_result(
        self,
        game_id: int,
        winner_team: int,
        mode: str,
        size: int,
        result_message_id: int,
        result_channel_id: int,
    ):
        """Сохраняет мета-информацию о завершённом матче (для !switch и !cancel)."""
        await self.pool.execute(
            """INSERT INTO match_results
               (game_id, winner_team, mode, size, result_message_id, result_channel_id)
               VALUES ($1,$2,$3,$4,$5,$6)
               ON CONFLICT (game_id) DO UPDATE
               SET winner_team=$2, mode=$3, size=$4,
                   result_message_id=$5, result_channel_id=$6""",
            game_id, winner_team, mode, size, result_message_id, result_channel_id,
        )

    async def get_match_results_bulk(self, game_ids: list[int]) -> dict[int, "_Row"]:
        """Возвращает dict {game_id: row} для списка game_id одним запросом."""
        if not game_ids:
            return {}
        rows = await self.pool.fetch(
            "SELECT * FROM match_results WHERE game_id = ANY($1::int[])",
            game_ids,
        )
        return {row["game_id"]: _row(row) for row in rows}

    async def get_match_result(self, game_id: int) -> _Row | None:
        r = await self.pool.fetchrow(
            "SELECT * FROM match_results WHERE game_id=$1", game_id
        )
        return _row(r)

    # ──────────────────────── !cancel ────────────────────────

    async def cancel_match(self, game_id: int) -> dict:
        """
        Полностью отменяет матч:
        — откатывает ELO всем игрокам (wins/losses/draws/games_played/win_streak)
        — удаляет записи из game_results и elo_history
        — удаляет из match_results
        Возвращает dict с информацией об откате для отчёта модератору.
        """
        affected = []

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Собираем уникальных игроков матча из elo_history
                hist_rows = await conn.fetch(
                    "SELECT * FROM elo_history WHERE game_id=$1", game_id
                )
                if not hist_rows:
                    return {"error": "not_found"}

                for h in hist_rows:
                    pid = h["discord_id"]
                    elo_before = h["elo_before"]
                    result_in_hist = h["result"]  # 'win'|'lose'|'draw' или NULL (старые записи)

                    # Определяем результат из знака change если result NULL
                    if not result_in_hist:
                        if h["change"] > 0:
                            result_in_hist = "win"
                        elif h["change"] < 0:
                            result_in_hist = "lose"
                        else:
                            result_in_hist = "draw"

                    # Откатываем ELO и статистику
                    if result_in_hist == "win":
                        await conn.execute(
                            """UPDATE players SET
                               elo=$1,
                               wins=GREATEST(0, wins-1),
                               games_played=GREATEST(0, games_played-1),
                               win_streak=GREATEST(0, win_streak-1)
                               WHERE discord_id=$2""",
                            elo_before, pid,
                        )
                    elif result_in_hist == "lose":
                        await conn.execute(
                            """UPDATE players SET
                               elo=$1,
                               losses=GREATEST(0, losses-1),
                               games_played=GREATEST(0, games_played-1)
                               WHERE discord_id=$2""",
                            elo_before, pid,
                        )
                    else:  # draw
                        await conn.execute(
                            """UPDATE players SET
                               elo=$1,
                               draws=GREATEST(0, draws-1),
                               games_played=GREATEST(0, games_played-1)
                               WHERE discord_id=$2""",
                            elo_before, pid,
                        )

                    affected.append({
                        "discord_id": pid,
                        "elo_before": h["elo_after"],   # было до отката
                        "elo_after": elo_before,         # стало после отката
                        "result": result_in_hist,
                    })

                # Удаляем все следы матча из БД
                await conn.execute("DELETE FROM elo_history WHERE game_id=$1", game_id)
                await conn.execute("DELETE FROM game_results WHERE game_id=$1", game_id)
                await conn.execute("DELETE FROM match_results WHERE game_id=$1", game_id)

        return {"affected": affected}

    # ──────────────────────── !switch ────────────────────────

    async def switch_match(self, game_id: int) -> dict:
        """
        Меняет победителя матча на проигравшего и наоборот.
        — Откатывает старые ELO
        — Применяет новые (победители становятся проигравшими и наоборот)
        — Обновляет game_results и match_results
        Возвращает dict с новыми данными для перепостинга embed.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                hist_rows = await conn.fetch(
                    "SELECT * FROM elo_history WHERE game_id=$1", game_id
                )
                if not hist_rows:
                    return {"error": "not_found"}

                match_row = await conn.fetchrow(
                    "SELECT * FROM match_results WHERE game_id=$1", game_id
                )

                # Собираем данные игроков по командам через game_results
                gr_rows = await conn.fetch(
                    "SELECT DISTINCT discord_id, result FROM game_results WHERE game_id=$1",
                    game_id,
                )

                # Строим карту: discord_id → старый result
                old_results: dict[int, str] = {}
                for row in gr_rows:
                    # Берём первый результат (игрок может встречаться против нескольких)
                    if row["discord_id"] not in old_results:
                        old_results[row["discord_id"]] = row["result"]

                # Инвертируем результаты
                def invert(r: str) -> str:
                    if r == "win":
                        return "lose"
                    if r == "lose":
                        return "win"
                    return "draw"

                new_results = {pid: invert(r) for pid, r in old_results.items()}

                # Строим карту: discord_id → строка истории
                hist_by_pid = {h["discord_id"]: h for h in hist_rows}

                # Группируем игроков по старому результату
                old_winners = [pid for pid, r in old_results.items() if r == "win"]
                old_losers  = [pid for pid, r in old_results.items() if r == "lose"]

                # Пересчитываем ELO с нуля через calculate_elo.
                # После switch: старые победители → проигравшие, старые losers → победители.
                # ELO каждого игрока ДО матча хранится в elo_history.elo_before.

                # Строим список игроков для calculate_elo (нужен только ключ "elo")
                winners_for_calc = [{"elo": hist_by_pid[pid]["elo_before"]} for pid in old_winners if pid in hist_by_pid]
                losers_for_calc  = [{"elo": hist_by_pid[pid]["elo_before"]} for pid in old_losers  if pid in hist_by_pid]

                # Режим и размер берём из match_results
                m_mode = match_row["mode"] if match_row else "cap"
                m_size = match_row["size"] if match_row else len(old_winners)

                # После switch: бывшие losers теперь winners — считаем по их ELO
                # бывшие winners теперь losers — считаем по их ELO
                new_win_change  = calculate_elo(losers_for_calc,  m_size, "win")
                new_lose_change = calculate_elo(winners_for_calc, m_size, "lose")

                # Применяем коэффициент team-режима если нужно
                if m_mode == "team":
                    new_win_change  = max(1,  int(new_win_change  * 0.7))
                    new_lose_change = min(-1, int(new_lose_change * 0.7))

                # Для каждого игрока: откат → применение нового ELO
                elo_changes = {}
                for h in hist_rows:
                    pid = h["discord_id"]
                    old_result = old_results.get(pid)
                    new_result = new_results.get(pid)
                    if not old_result or not new_result:
                        continue

                    elo_before_orig = h["elo_before"]

                    if new_result == "win":
                        new_change = new_win_change
                    elif new_result == "lose":
                        new_change = new_lose_change
                    else:
                        new_change = 0

                    # Не уходим в минус
                    if elo_before_orig == 0 and new_change < 0:
                        new_change = 0

                    new_elo = max(0, elo_before_orig + new_change)
                    elo_changes[pid] = (elo_before_orig, new_elo, new_change, new_result, old_result)

                    # Откат старой статистики
                    if old_result == "win":
                        await conn.execute(
                            """UPDATE players SET
                               wins=GREATEST(0, wins-1),
                               games_played=GREATEST(0, games_played-1)
                               WHERE discord_id=$1""", pid,
                        )
                    elif old_result == "lose":
                        await conn.execute(
                            """UPDATE players SET
                               losses=GREATEST(0, losses-1),
                               games_played=GREATEST(0, games_played-1)
                               WHERE discord_id=$1""", pid,
                        )
                    else:
                        await conn.execute(
                            """UPDATE players SET
                               draws=GREATEST(0, draws-1),
                               games_played=GREATEST(0, games_played-1)
                               WHERE discord_id=$1""", pid,
                        )

                    # Применяем новую статистику
                    if new_result == "win":
                        await conn.execute(
                            """UPDATE players SET
                               elo=$1,
                               wins=wins+1,
                               games_played=games_played+1,
                               win_streak=win_streak+1
                               WHERE discord_id=$2""",
                            new_elo, pid,
                        )
                    elif new_result == "lose":
                        await conn.execute(
                            """UPDATE players SET
                               elo=$1,
                               losses=losses+1,
                               games_played=games_played+1,
                               win_streak=0
                               WHERE discord_id=$2""",
                            new_elo, pid,
                        )
                    else:
                        await conn.execute(
                            """UPDATE players SET
                               elo=$1,
                               draws=draws+1,
                               games_played=games_played+1,
                               win_streak=0
                               WHERE discord_id=$2""",
                            new_elo, pid,
                        )

                    # Обновляем elo_history
                    await conn.execute(
                        """UPDATE elo_history SET
                           elo_after=$1, change=$2, result=$3
                           WHERE game_id=$4 AND discord_id=$5""",
                        new_elo, new_change, new_result, game_id, pid,
                    )

                # Обновляем game_results
                await conn.execute("DELETE FROM game_results WHERE game_id=$1", game_id)
                rows_to_insert = []
                # Перестраиваем game_results с новыми результатами
                team1_ids = [pid for pid, r in old_results.items() if r == "win"]
                team2_ids = [pid for pid, r in old_results.items() if r == "lose"]
                # После switch: старые победители → lose, старые losers → win
                for p1 in team1_ids:
                    for p2 in team2_ids:
                        rows_to_insert.append((game_id, p1, p2, "lose"))
                        rows_to_insert.append((game_id, p2, p1, "win"))
                if rows_to_insert:
                    await conn.executemany(
                        "INSERT INTO game_results (game_id, discord_id, opponent_id, result) VALUES ($1,$2,$3,$4)",
                        rows_to_insert,
                    )

                # Обновляем match_results — меняем winner_team
                if match_row:
                    old_winner = match_row["winner_team"]
                    new_winner = 2 if old_winner == 1 else (1 if old_winner == 2 else 0)
                    await conn.execute(
                        "UPDATE match_results SET winner_team=$1 WHERE game_id=$2",
                        new_winner, game_id,
                    )

        return {
            "elo_changes": elo_changes,
            "match_row": dict(match_row) if match_row else None,
            "new_winner_team": new_winner if match_row else None,
        }

