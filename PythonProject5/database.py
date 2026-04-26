# database.py  —  PostgreSQL (asyncpg)
import os
import datetime
import asyncpg
from config import Config

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

    async def get_player_by_username(self, username: str) -> _Row | None:
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
                       (discord_id, elo_before, elo_after, change, game_id, mode, size, result)
                       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                    discord_id, elo_before, new_elo, new_elo - elo_before, game_id, mode, size, result,
                )

    async def apply_penalty(self, discord_id: int):
        await self.pool.execute(
            "UPDATE players SET penalty_games=penalty_games+3 WHERE discord_id=$1",
            discord_id,
        )

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
                 AND r.status IN ('waiting','full','started','picking')""",
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
        Сохраняет парные результаты для каждого игрока относительно каждого оппонента.
        result — результат для команды 1: 'win'|'lose'|'draw'
        """
        rows = []
        # team1 против team2
        for p1 in team1:
            for p2 in team2:
                r1 = result            # результат игрока из team1
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
            """SELECT id, game_id, elo_before, elo_after, change, mode, size, timestamp
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

