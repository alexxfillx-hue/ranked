# database.py
import aiosqlite
import datetime
from config import Config

DB = Config.DB_PATH

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS players (
    discord_id    INTEGER PRIMARY KEY,
    username      TEXT    NOT NULL,
    elo           INTEGER DEFAULT 1000,
    wins          INTEGER DEFAULT 0,
    losses        INTEGER DEFAULT 0,
    draws         INTEGER DEFAULT 0,
    games_played  INTEGER DEFAULT 0,
    win_streak    INTEGER DEFAULT 0,
    penalty_games INTEGER DEFAULT 0,
    report_count  INTEGER DEFAULT 0,
    lang          TEXT    DEFAULT 'ru',
    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS elo_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id   INTEGER NOT NULL,
    elo_before   INTEGER NOT NULL,
    elo_after    INTEGER NOT NULL,
    change       INTEGER NOT NULL,
    game_id      INTEGER,
    timestamp    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (discord_id) REFERENCES players(discord_id)
);

CREATE TABLE IF NOT EXISTS rooms (
    room_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id       INTEGER UNIQUE,
    size             INTEGER NOT NULL,
    mode             TEXT    DEFAULT 'team',
    status           TEXT    DEFAULT 'waiting',
    created_by       INTEGER NOT NULL,
    embed_message_id INTEGER,
    pick_turn        INTEGER DEFAULT 1,
    started_at       TIMESTAMP,
    pinged_at        TIMESTAMP,
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by) REFERENCES players(discord_id)
);

CREATE TABLE IF NOT EXISTS room_players (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id          INTEGER NOT NULL,
    discord_id       INTEGER NOT NULL,
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
    uploader_id      INTEGER NOT NULL,
    uploaded_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (room_id, team)
);

CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    reporter_id INTEGER NOT NULL,
    reported_id INTEGER NOT NULL,
    reason      TEXT    NOT NULL,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self):
        self.path = DB

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    # ──────────────────────── Players ────────────────────────

    async def register(self, discord_id: int, username: str) -> bool:
        try:
            async with aiosqlite.connect(self.path) as db:
                await db.execute(
                    "INSERT INTO players (discord_id, username, elo) VALUES (?,?,?)",
                    (discord_id, username, Config.STARTING_ELO),
                )
                await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def get_player(self, discord_id: int):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                    "SELECT * FROM players WHERE discord_id=?", (discord_id,)
            ) as cur:
                return await cur.fetchone()

    async def get_player_by_username(self, username: str):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                    "SELECT * FROM players WHERE LOWER(username)=LOWER(?)", (username,)
            ) as cur:
                return await cur.fetchone()

    async def update_username(self, discord_id: int, new_username: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE players SET username=? WHERE discord_id=?",
                (new_username, discord_id),
            )
            await db.commit()

    async def get_lang(self, discord_id: int) -> str:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT lang FROM players WHERE discord_id=?", (discord_id,)
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row and row[0] else "ru"

    async def set_lang(self, discord_id: int, lang: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE players SET lang=? WHERE discord_id=?",
                (lang, discord_id),
            )
            await db.commit()

    async def get_top(self, limit: int = 10):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                    "SELECT * FROM players ORDER BY elo DESC LIMIT ?", (limit,)
            ) as cur:
                return await cur.fetchall()

    async def update_after_game(
            self,
            discord_id: int,
            new_elo: int,
            result: str,  # 'win' | 'lose' | 'draw'
            game_id: int,
    ):
        pl = await self.get_player(discord_id)
        if not pl:
            return
        elo_before = pl["elo"]
        async with aiosqlite.connect(self.path) as db:
            if result == "win":
                await db.execute(
                    """UPDATE players
                       SET elo=?, wins=wins+1, games_played=games_played+1,
                           win_streak=win_streak+1,
                           penalty_games=MAX(0,penalty_games-1)
                       WHERE discord_id=?""",
                    (new_elo, discord_id),
                )
            elif result == "lose":
                await db.execute(
                    """UPDATE players
                       SET elo=?, losses=losses+1, games_played=games_played+1,
                           win_streak=0,
                           penalty_games=MAX(0,penalty_games-1)
                       WHERE discord_id=?""",
                    (new_elo, discord_id),
                )
            else:  # draw
                await db.execute(
                    """UPDATE players
                       SET elo=?, draws=draws+1, games_played=games_played+1,
                           win_streak=0,
                           penalty_games=MAX(0,penalty_games-1)
                       WHERE discord_id=?""",
                    (new_elo, discord_id),
                )
            await db.execute(
                """INSERT INTO elo_history
                   (discord_id, elo_before, elo_after, change, game_id)
                   VALUES (?,?,?,?,?)""",
                (discord_id, elo_before, new_elo, new_elo - elo_before, game_id),
            )
            await db.commit()

    async def apply_penalty(self, discord_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE players SET penalty_games=penalty_games+3 WHERE discord_id=?",
                (discord_id,),
            )
            await db.commit()

    async def get_elo_history(self, discord_id: int, since: datetime.datetime | None):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            if since:
                async with db.execute(
                        """SELECT * FROM elo_history
                           WHERE discord_id=? AND timestamp>=?
                           ORDER BY timestamp ASC""",
                        (discord_id, since.isoformat()),
                ) as cur:
                    return await cur.fetchall()
            async with db.execute(
                    "SELECT * FROM elo_history WHERE discord_id=? ORDER BY timestamp ASC",
                    (discord_id,),
            ) as cur:
                return await cur.fetchall()

    # ──────────────────────── Rooms ────────────────────────

    async def create_room(self, channel_id: int, size: int, creator_id: int, mode: str = "team") -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO rooms (channel_id, size, mode, created_by) VALUES (?,?,?,?)",
                (channel_id, size, mode, creator_id),
            )
            await db.commit()
            return cur.lastrowid  # type: ignore

    async def update_channel_id(self, room_id: int, channel_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE rooms SET channel_id=? WHERE room_id=?",
                (channel_id, room_id),
            )
            await db.commit()

    async def get_room(self, room_id: int):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                    "SELECT * FROM rooms WHERE room_id=?", (room_id,)
            ) as cur:
                return await cur.fetchone()

    async def get_room_by_channel(self, channel_id: int):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                    "SELECT * FROM rooms WHERE channel_id=?", (channel_id,)
            ) as cur:
                return await cur.fetchone()

    async def get_player_room(self, discord_id: int):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                    """SELECT r.* FROM rooms r
                       JOIN room_players rp ON r.room_id=rp.room_id
                       WHERE rp.discord_id=? AND r.status IN ('waiting','full','started','picking')""",
                    (discord_id,),
            ) as cur:
                return await cur.fetchone()

    async def get_room_players(self, room_id: int):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                    """SELECT rp.*, p.username, p.elo
                       FROM room_players rp
                       JOIN players p ON rp.discord_id=p.discord_id
                       WHERE rp.room_id=?
                       ORDER BY rp.team, rp.is_captain DESC""",
                    (room_id,),
            ) as cur:
                return await cur.fetchall()

    async def add_to_room(
            self, room_id: int, discord_id: int, team: int, is_captain: bool = False
    ):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT INTO room_players (room_id,discord_id,team,is_captain)
                   VALUES (?,?,?,?)""",
                (room_id, discord_id, team, int(is_captain)),
            )
            await db.commit()

    async def remove_from_room(self, room_id: int, discord_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM room_players WHERE room_id=? AND discord_id=?",
                (room_id, discord_id),
            )
            await db.commit()

    async def set_captain(self, room_id: int, discord_id: int, value: bool):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE room_players SET is_captain=? WHERE room_id=? AND discord_id=?",
                (int(value), room_id, discord_id),
            )
            await db.commit()

    async def set_player_team(self, room_id: int, discord_id: int, team: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE room_players SET team=? WHERE room_id=? AND discord_id=?",
                (team, room_id, discord_id),
            )
            await db.commit()

    async def set_pick_turn(self, room_id: int, turn: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE rooms SET pick_turn=? WHERE room_id=?",
                (turn, room_id),
            )
            await db.commit()

    async def set_ready(self, room_id: int, team: int, value: bool):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """UPDATE room_players SET confirmed_start=?
                   WHERE room_id=? AND team=? AND is_captain=1""",
                (int(value), room_id, team),
            )
            await db.commit()

    async def set_start_confirm(self, room_id: int, discord_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE room_players SET confirmed_start=1 WHERE room_id=? AND discord_id=?",
                (room_id, discord_id),
            )
            await db.commit()

    async def set_end_vote(self, room_id: int, discord_id: int, vote):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE room_players SET end_vote=? WHERE room_id=? AND discord_id=?",
                (vote, room_id, discord_id),
            )
            await db.commit()

    async def update_room_status(self, room_id: int, status: str):
        async with aiosqlite.connect(self.path) as db:
            if status == "started":
                await db.execute(
                    "UPDATE rooms SET status=?, started_at=CURRENT_TIMESTAMP WHERE room_id=?",
                    (status, room_id),
                )
            else:
                await db.execute(
                    "UPDATE rooms SET status=? WHERE room_id=?", (status, room_id)
                )
            await db.commit()

    async def update_embed_id(self, room_id: int, msg_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE rooms SET embed_message_id=? WHERE room_id=?",
                (msg_id, room_id),
            )
            await db.commit()

    async def set_pinged(self, room_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE rooms SET pinged_at=CURRENT_TIMESTAMP WHERE room_id=?",
                (room_id,),
            )
            await db.commit()

    async def try_finalize_room(self, room_id: int) -> bool:
        """
        Атомарно переводит комнату из статуса 'started' → 'finalizing'.
        Возвращает True только если переход успешен (т.е. этот вызов — первый).
        Защищает от двойного начисления ELO при одновременном нажатии кнопок.
        """
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "UPDATE rooms SET status='finalizing' WHERE room_id=? AND status='started'",
                (room_id,),
            )
            await db.commit()
            return cur.rowcount == 1

    async def delete_room(self, room_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("DELETE FROM room_players WHERE room_id=?", (room_id,))
            await db.execute("DELETE FROM rooms WHERE room_id=?", (room_id,))
            await db.commit()

    async def get_open_rooms(self):
        """Все комнаты куда ещё можно зайти (waiting)."""
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                    "SELECT * FROM rooms WHERE status='waiting' ORDER BY created_at ASC"
            ) as cur:
                return await cur.fetchall()

    async def get_available_rooms(self, size: int | None = None, mode: str | None = None):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            # cap-комнаты доступны и в статусе 'picking' (пик идёт — нельзя зайти)
            # random/team доступны только в 'waiting'
            conditions = ["status='waiting'"]
            params = []
            if size:
                conditions.append("size=?")
                params.append(size)
            if mode:
                conditions.append("mode=?")
                params.append(mode)
            where = " AND ".join(conditions)
            async with db.execute(
                    f"SELECT * FROM rooms WHERE {where}", params
            ) as cur:
                return await cur.fetchall()

    async def get_started_rooms(self):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                    "SELECT * FROM rooms WHERE status='started'"
            ) as cur:
                return await cur.fetchall()

    # ──────────────────────── Reports ────────────────────────

    async def add_report(self, reporter_id: int, reported_id: int, reason: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO reports (reporter_id,reported_id,reason) VALUES (?,?,?)",
                (reporter_id, reported_id, reason),
            )
            await db.execute(
                "UPDATE players SET report_count = report_count + 1 WHERE discord_id=?",
                (reported_id,),
            )
            await db.commit()

    async def get_report_count(self, discord_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                "SELECT report_count FROM players WHERE discord_id=?", (discord_id,)
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def mod_adjust_elo(self, discord_id: int, amount: int) -> int:
        """Прибавляет или вычитает ELO модератором. Возвращает новое ELO."""
        pl = await self.get_player(discord_id)
        if not pl:
            return -1
        new_elo = max(0, pl["elo"] + amount)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE players SET elo=? WHERE discord_id=?",
                (new_elo, discord_id),
            )
            await db.execute(
                """INSERT INTO elo_history (discord_id, elo_before, elo_after, change)
                   VALUES (?,?,?,?)""",
                (discord_id, pl["elo"], new_elo, amount),
            )
            await db.commit()
        return new_elo

    async def reports_today(self, reporter_id: int) -> int:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                    """SELECT COUNT(*) FROM reports
                       WHERE reporter_id=? AND DATE(created_at)=DATE('now')""",
                    (reporter_id,),
            ) as cur:
                row = await cur.fetchone()
                return row[0] if row else 0

    async def already_reported(self, reporter_id: int, reported_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            async with db.execute(
                    "SELECT 1 FROM reports WHERE reporter_id=? AND reported_id=?",
                    (reporter_id, reported_id),
            ) as cur:
                return await cur.fetchone() is not None

    # ──────────────────────── ELO leave penalty ────────────────────

    async def deduct_elo_for_leave(self, discord_id: int, amount: int = 15) -> int:
        """Вычитает ELO за выход из активной игры. Возвращает новое ELO."""
        pl = await self.get_player(discord_id)
        if not pl:
            return 0
        new_elo = max(0, pl["elo"] - amount)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE players SET elo=? WHERE discord_id=?",
                (new_elo, discord_id),
            )
            await db.execute(
                """INSERT INTO elo_history (discord_id, elo_before, elo_after, change)
                   VALUES (?,?,?,?)""",
                (discord_id, pl["elo"], new_elo, -amount),
            )
            await db.commit()
        return new_elo

    # ──────────────────────── Screenshots ────────────────────────

    async def add_screenshot(self, room_id: int, team: int, uploader_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO room_screenshots (room_id, team, uploader_id)
                   VALUES (?,?,?)""",
                (room_id, team, uploader_id),
            )
            await db.commit()

    async def get_screenshots(self, room_id: int):
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM room_screenshots WHERE room_id=?", (room_id,)
            ) as cur:
                return await cur.fetchall()

    async def delete_screenshots(self, room_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "DELETE FROM room_screenshots WHERE room_id=?", (room_id,)
            )
            await db.commit()