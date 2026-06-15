from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class Database:
    def __init__(self, path: str, max_database_mb: float = 50.0):
        self.path = Path(path)
        self.max_database_bytes = max(0, int(max_database_mb * 1024 * 1024))
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        if self.max_database_bytes:
            self.conn.execute(f"PRAGMA journal_size_limit={max(1_048_576, self.max_database_bytes // 10)}")
        self.migrate()

    def migrate(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                window_start_ts INTEGER,
                market_id TEXT,
                decision TEXT,
                reason TEXT,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dry_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                window_start_ts INTEGER NOT NULL,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL DEFAULT 'BUY',
                price REAL NOT NULL,
                notional_usdc REAL NOT NULL,
                shares REAL NOT NULL,
                reason TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS markets (
                market_id TEXT PRIMARY KEY,
                slug TEXT NOT NULL,
                up_token_id TEXT NOT NULL,
                down_token_id TEXT NOT NULL,
                window_start_ts INTEGER NOT NULL,
                window_end_ts INTEGER NOT NULL,
                target_price REAL,
                target_source TEXT,
                payload_json TEXT NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                decision_id TEXT NOT NULL,
                window_start INTEGER NOT NULL,
                window_end INTEGER NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                price REAL NOT NULL,
                notional_usdc REAL NOT NULL,
                shares REAL NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS paper_fills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                window_start INTEGER NOT NULL,
                window_end INTEGER NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                price REAL NOT NULL,
                notional_usdc REAL NOT NULL,
                shares REAL NOT NULL,
                metadata_json TEXT NOT NULL,
                FOREIGN KEY(order_id) REFERENCES paper_orders(id)
            );

            CREATE TABLE IF NOT EXISTS window_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_start INTEGER NOT NULL,
                window_end INTEGER NOT NULL,
                market_id TEXT NOT NULL,
                target_price REAL NOT NULL,
                final_btc_price REAL NOT NULL,
                winning_side TEXT NOT NULL,
                resolved INTEGER NOT NULL,
                resolved_at REAL NOT NULL,
                UNIQUE(window_start, market_id)
            );

            CREATE TABLE IF NOT EXISTS window_pnl (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_start INTEGER NOT NULL,
                market_id TEXT NOT NULL,
                up_shares REAL NOT NULL,
                down_shares REAL NOT NULL,
                total_cost REAL NOT NULL,
                pnl_if_up REAL NOT NULL,
                pnl_if_down REAL NOT NULL,
                realized_pnl REAL NOT NULL,
                winning_side TEXT NOT NULL,
                locked INTEGER NOT NULL,
                notes TEXT NOT NULL,
                UNIQUE(window_start, market_id)
            );

            CREATE TABLE IF NOT EXISTS live_order_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                decision_id TEXT NOT NULL,
                window_start INTEGER NOT NULL,
                window_end INTEGER NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                price REAL NOT NULL,
                notional_usdc REAL NOT NULL,
                shares REAL NOT NULL,
                status TEXT NOT NULL,
                external_order_id TEXT,
                reason TEXT NOT NULL,
                response_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS live_account_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                market_id TEXT,
                event_type TEXT NOT NULL,
                external_id TEXT,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_paper_orders_window ON paper_orders (market_id, window_start);
            CREATE INDEX IF NOT EXISTS idx_paper_fills_window ON paper_fills (market_id, window_start);
            CREATE INDEX IF NOT EXISTS idx_window_results_window ON window_results (market_id, window_start);
            CREATE INDEX IF NOT EXISTS idx_window_pnl_window ON window_pnl (market_id, window_start);
            CREATE INDEX IF NOT EXISTS idx_live_order_audit_window ON live_order_audit (market_id, window_start);
            CREATE INDEX IF NOT EXISTS idx_live_account_events_market ON live_account_events (market_id, created_at);
            """
        )
        self._ensure_column("dry_fills", "action", "TEXT NOT NULL DEFAULT 'BUY'")
        self.conn.commit()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        columns = {str(row["name"]) for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _dumps(self, payload: Any) -> str:
        return json.dumps(payload, default=str)

    def _fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def insert_decision(self, payload: dict[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO decisions (ts, window_start_ts, market_id, decision, reason, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                payload.get("timestamp", payload.get("ts")),
                payload.get("window_start"),
                payload.get("market_id"),
                payload.get("decision"),
                payload.get("selected_reason", payload.get("reason", "")),
                self._dumps(payload),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def insert_fill(self, payload: dict[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO dry_fills (ts, window_start_ts, market_id, side, action, price, notional_usdc, shares, reason, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["ts"],
                payload["window_start_ts"],
                payload["market_id"],
                payload["side"],
                payload.get("action", "BUY"),
                payload["price"],
                payload["notional_usdc"],
                payload["shares"],
                payload["reason"],
                self._dumps(payload),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def upsert_market(self, payload: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO markets (market_id, slug, up_token_id, down_token_id, window_start_ts, window_end_ts, target_price, target_source, payload_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(market_id) DO UPDATE SET
                slug=excluded.slug,
                up_token_id=excluded.up_token_id,
                down_token_id=excluded.down_token_id,
                window_start_ts=excluded.window_start_ts,
                window_end_ts=excluded.window_end_ts,
                target_price=excluded.target_price,
                target_source=excluded.target_source,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                payload["market_id"], payload["slug"], payload["up_token_id"], payload["down_token_id"],
                payload["window_start_ts"], payload["window_end_ts"], payload.get("target_price"), payload.get("target_source"),
                self._dumps(payload), payload.get("updated_at"),
            ),
        )
        self.conn.commit()

    def insert_paper_order(self, payload: dict[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO paper_orders (
                created_at, decision_id, window_start, window_end, market_id, token_id,
                side, action, price, notional_usdc, shares, status, reason, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["created_at"],
                payload["decision_id"],
                payload["window_start"],
                payload["window_end"],
                payload["market_id"],
                payload["token_id"],
                payload["side"],
                payload["action"],
                payload["price"],
                payload["notional_usdc"],
                payload["shares"],
                payload["status"],
                payload["reason"],
                self._dumps(payload.get("metadata_json", {})),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update_paper_order(self, order_id: int, status: str, reason: str, metadata_json: dict[str, Any]) -> None:
        self.conn.execute(
            """
            UPDATE paper_orders
            SET status = ?, reason = ?, metadata_json = ?
            WHERE id = ?
            """,
            (status, reason, self._dumps(metadata_json), order_id),
        )
        self.conn.commit()

    def get_paper_order(self, order_id: int) -> dict[str, Any] | None:
        return self._fetchone("SELECT * FROM paper_orders WHERE id = ?", (order_id,))

    def list_paper_orders(self, market_id: str, window_start: int) -> list[dict[str, Any]]:
        return self._fetchall(
            "SELECT * FROM paper_orders WHERE market_id = ? AND window_start = ? ORDER BY id",
            (market_id, window_start),
        )

    def list_all_paper_orders(self) -> list[dict[str, Any]]:
        return self._fetchall("SELECT * FROM paper_orders ORDER BY id")

    def insert_paper_fill(self, payload: dict[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO paper_fills (
                order_id, created_at, window_start, window_end, market_id, token_id,
                side, action, price, notional_usdc, shares, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["order_id"],
                payload["created_at"],
                payload["window_start"],
                payload["window_end"],
                payload["market_id"],
                payload["token_id"],
                payload["side"],
                payload["action"],
                payload["price"],
                payload["notional_usdc"],
                payload["shares"],
                self._dumps(payload.get("metadata_json", {})),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_paper_fills(self, market_id: str, window_start: int) -> list[dict[str, Any]]:
        return self._fetchall(
            "SELECT * FROM paper_fills WHERE market_id = ? AND window_start = ? ORDER BY id",
            (market_id, window_start),
        )

    def list_all_paper_fills(self) -> list[dict[str, Any]]:
        return self._fetchall("SELECT * FROM paper_fills ORDER BY id")

    def insert_live_order_audit(self, payload: dict[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO live_order_audit (
                created_at, decision_id, window_start, window_end, market_id, token_id,
                side, action, price, notional_usdc, shares, status, external_order_id,
                reason, response_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                payload["created_at"],
                payload["decision_id"],
                payload["window_start"],
                payload["window_end"],
                payload["market_id"],
                payload["token_id"],
                payload["side"],
                payload["action"],
                payload["price"],
                payload["notional_usdc"],
                payload["shares"],
                payload["status"],
                payload.get("external_order_id"),
                payload["reason"],
                self._dumps(payload.get("response_json", {})),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_live_order_audits(self) -> list[dict[str, Any]]:
        return self._fetchall("SELECT * FROM live_order_audit ORDER BY id")

    def list_matched_live_orders(self, market_id: str) -> list[dict[str, Any]]:
        """Return immutable matched-order history for live position/risk recovery."""
        return self._fetchall(
            """
            SELECT *
            FROM live_order_audit
            WHERE market_id = ?
              AND status = 'MATCHED'
            ORDER BY id
            """,
            (market_id,),
        )

    def list_all_matched_live_orders(self) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            SELECT *
            FROM live_order_audit
            WHERE status = 'MATCHED'
            ORDER BY id
            """
        )

    def insert_live_account_event(self, payload: dict[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO live_account_events (
                created_at, market_id, event_type, external_id, payload_json
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                payload["created_at"],
                payload.get("market_id"),
                payload["event_type"],
                payload.get("external_id"),
                self._dumps(payload.get("payload_json", {})),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def upsert_window_result(self, payload: dict[str, Any]) -> int:
        self.conn.execute(
            """
            INSERT INTO window_results (
                window_start, window_end, market_id, target_price, final_btc_price,
                winning_side, resolved, resolved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(window_start, market_id) DO UPDATE SET
                window_end = excluded.window_end,
                target_price = excluded.target_price,
                final_btc_price = excluded.final_btc_price,
                winning_side = excluded.winning_side,
                resolved = excluded.resolved,
                resolved_at = excluded.resolved_at
            """,
            (
                payload["window_start"],
                payload["window_end"],
                payload["market_id"],
                payload["target_price"],
                payload["final_btc_price"],
                payload["winning_side"],
                1 if payload["resolved"] else 0,
                payload["resolved_at"],
            ),
        )
        self.conn.commit()
        row = self._fetchone(
            "SELECT id FROM window_results WHERE window_start = ? AND market_id = ?",
            (payload["window_start"], payload["market_id"]),
        )
        return int(row["id"]) if row else 0

    def upsert_window_pnl(self, payload: dict[str, Any]) -> int:
        self.conn.execute(
            """
            INSERT INTO window_pnl (
                window_start, market_id, up_shares, down_shares, total_cost,
                pnl_if_up, pnl_if_down, realized_pnl, winning_side, locked, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(window_start, market_id) DO UPDATE SET
                up_shares = excluded.up_shares,
                down_shares = excluded.down_shares,
                total_cost = excluded.total_cost,
                pnl_if_up = excluded.pnl_if_up,
                pnl_if_down = excluded.pnl_if_down,
                realized_pnl = excluded.realized_pnl,
                winning_side = excluded.winning_side,
                locked = excluded.locked,
                notes = excluded.notes
            """,
            (
                payload["window_start"],
                payload["market_id"],
                payload["up_shares"],
                payload["down_shares"],
                payload["total_cost"],
                payload["pnl_if_up"],
                payload["pnl_if_down"],
                payload["realized_pnl"],
                payload["winning_side"],
                1 if payload["locked"] else 0,
                payload.get("notes", ""),
            ),
        )
        self.conn.commit()
        row = self._fetchone(
            "SELECT id FROM window_pnl WHERE window_start = ? AND market_id = ?",
            (payload["window_start"], payload["market_id"]),
        )
        return int(row["id"]) if row else 0

    def get_window_result(self, market_id: str, window_start: int) -> dict[str, Any] | None:
        return self._fetchone(
            "SELECT * FROM window_results WHERE market_id = ? AND window_start = ?",
            (market_id, window_start),
        )

    def get_window_pnl(self, market_id: str, window_start: int) -> dict[str, Any] | None:
        return self._fetchone(
            "SELECT * FROM window_pnl WHERE market_id = ? AND window_start = ?",
            (market_id, window_start),
        )

    def list_unresolved_markets(self, now_ts: float) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            SELECT m.*
            FROM markets m
            LEFT JOIN window_results wr
                ON wr.market_id = m.market_id
               AND wr.window_start = m.window_start_ts
            WHERE m.window_end_ts <= ?
              AND wr.id IS NULL
            ORDER BY m.window_end_ts
            """,
            (int(now_ts),),
        )

    def list_window_report_rows(self) -> list[dict[str, Any]]:
        return self._fetchall(
            """
            SELECT
                p.window_start,
                r.window_end,
                p.market_id,
                r.target_price,
                r.final_btc_price AS final_price,
                r.winning_side,
                p.up_shares,
                p.down_shares,
                p.total_cost,
                p.pnl_if_up,
                p.pnl_if_down,
                CASE
                    WHEN p.pnl_if_up < p.pnl_if_down THEN p.pnl_if_up
                    ELSE p.pnl_if_down
                END AS worst_case_pnl,
                p.realized_pnl,
                p.locked,
                p.notes,
                (
                    SELECT COUNT(*)
                    FROM paper_orders o
                    WHERE o.market_id = p.market_id
                      AND o.window_start = p.window_start
                ) AS number_of_orders,
                (
                    SELECT COUNT(*)
                    FROM paper_fills f
                    WHERE f.market_id = p.market_id
                      AND f.window_start = p.window_start
                ) AS number_of_fills
                ,(
                    SELECT COALESCE(SUM(f.notional_usdc), 0.0)
                    FROM paper_fills f
                    WHERE f.market_id = p.market_id
                      AND f.window_start = p.window_start
                      AND f.action = 'BUY'
                ) AS gross_buy_cost
                ,(
                    SELECT COALESCE(SUM(f.notional_usdc), 0.0)
                    FROM paper_fills f
                    WHERE f.market_id = p.market_id
                      AND f.window_start = p.window_start
                      AND f.action = 'SELL'
                ) AS gross_sell_proceeds
                ,(
                    SELECT COUNT(*)
                    FROM paper_fills f
                    WHERE f.market_id = p.market_id
                      AND f.window_start = p.window_start
                      AND f.action = 'SELL'
                ) AS number_of_sells
            FROM window_pnl p
            LEFT JOIN window_results r
              ON r.market_id = p.market_id
             AND r.window_start = p.window_start
            ORDER BY p.window_start
            """
        )

    def list_live_window_report_rows(self) -> list[dict[str, Any]]:
        """Reconstruct settled live PnL from immutable matched-order audits."""
        return self._fetchall(
            """
            WITH positions AS (
                SELECT
                    a.window_start,
                    a.market_id,
                    SUM(CASE WHEN a.side = 'UP' AND a.action = 'BUY' THEN a.shares WHEN a.side = 'UP' AND a.action = 'SELL' THEN -a.shares ELSE 0 END) AS up_shares,
                    SUM(CASE WHEN a.side = 'DOWN' AND a.action = 'BUY' THEN a.shares WHEN a.side = 'DOWN' AND a.action = 'SELL' THEN -a.shares ELSE 0 END) AS down_shares,
                    SUM(CASE WHEN a.action = 'SELL' THEN -a.notional_usdc ELSE a.notional_usdc END) AS total_cost,
                    SUM(CASE WHEN a.action = 'BUY' THEN a.notional_usdc ELSE 0 END) AS gross_buy_cost,
                    SUM(CASE WHEN a.action = 'SELL' THEN a.notional_usdc ELSE 0 END) AS gross_sell_proceeds,
                    SUM(CASE WHEN a.action = 'SELL' THEN 1 ELSE 0 END) AS number_of_sells,
                    COUNT(*) AS number_of_orders,
                    COUNT(*) AS number_of_fills
                FROM live_order_audit a
                WHERE a.status = 'MATCHED'
                GROUP BY a.window_start, a.market_id
            )
            SELECT
                p.window_start,
                r.window_end,
                p.market_id,
                r.target_price,
                r.final_btc_price AS final_price,
                r.winning_side,
                p.up_shares,
                p.down_shares,
                p.total_cost,
                p.up_shares - p.total_cost AS pnl_if_up,
                p.down_shares - p.total_cost AS pnl_if_down,
                MIN(p.up_shares - p.total_cost, p.down_shares - p.total_cost) AS worst_case_pnl,
                CASE
                    WHEN r.winning_side = 'UP' THEN p.up_shares - p.total_cost
                    ELSE p.down_shares - p.total_cost
                END AS realized_pnl,
                CASE
                    WHEN MIN(p.up_shares - p.total_cost, p.down_shares - p.total_cost) > 0 THEN 1
                    ELSE 0
                END AS locked,
                'settled_from_live_matched_audit' AS notes,
                p.number_of_orders,
                p.number_of_fills,
                p.gross_buy_cost,
                p.gross_sell_proceeds,
                p.number_of_sells
            FROM positions p
            JOIN window_results r
              ON r.market_id = p.market_id
             AND r.window_start = p.window_start
            WHERE r.resolved = 1
            ORDER BY p.window_start
            """
        )

    def total_realized_pnl(self) -> float:
        row = self._fetchone("SELECT COALESCE(SUM(realized_pnl), 0.0) AS total FROM window_pnl")
        return float(row["total"]) if row else 0.0

    def win_rate_percent(self) -> float:
        """Return profitable settled traded windows as a percentage."""
        row = self._fetchone(
            """
            SELECT
                COUNT(*) AS settled_windows,
                COALESCE(SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END), 0) AS wins
            FROM window_pnl
            WHERE total_cost != 0
               OR EXISTS (
                    SELECT 1
                    FROM paper_fills f
                    WHERE f.market_id = window_pnl.market_id
                      AND f.window_start = window_pnl.window_start
               )
            """
        )
        settled_windows = int(row["settled_windows"]) if row else 0
        return (float(row["wins"]) / settled_windows * 100.0) if settled_windows else 0.0

    def list_decisions(self) -> list[dict[str, Any]]:
        rows = self._fetchall("SELECT * FROM decisions ORDER BY id")
        for row in rows:
            try:
                row["payload"] = json.loads(row["payload_json"])
            except json.JSONDecodeError:
                row["payload"] = {}
        return rows

    def decision_count(self) -> int:
        row = self._fetchone("SELECT COUNT(*) AS count FROM decisions")
        return int(row["count"]) if row else 0

    def storage_bytes(self) -> int:
        """Return the SQLite database plus its temporary WAL/SHM sidecar files."""
        return sum(
            path.stat().st_size
            for path in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm"))
            if path.exists()
        )

    def maintain_storage(self, decision_rows_to_keep: int = 5000) -> dict[str, Any]:
        """Prune disposable decision telemetry while preserving trading and PnL history."""
        keep = max(0, int(decision_rows_to_keep))
        before_count = self.decision_count()
        if before_count > keep:
            if keep == 0:
                self.conn.execute("DELETE FROM decisions")
            else:
                self.conn.execute(
                    """
                    DELETE FROM decisions
                    WHERE id NOT IN (
                        SELECT id FROM decisions ORDER BY id DESC LIMIT ?
                    )
                    """,
                    (keep,),
                )
            self.conn.commit()

        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        size_before_vacuum = self.storage_bytes()
        vacuumed = False
        if self.max_database_bytes and size_before_vacuum > self.max_database_bytes:
            # Decisions are sampled monitoring telemetry. Remove them before ever
            # considering deletion of orders, fills, balance, or settled PnL.
            if self.decision_count() > 0:
                self.conn.execute("DELETE FROM decisions")
                self.conn.commit()
            self.conn.execute("VACUUM")
            vacuumed = True

        database_bytes = self.storage_bytes()
        return {
            "decisions_before": before_count,
            "decisions_after": self.decision_count(),
            "database_bytes": database_bytes,
            "vacuumed": vacuumed,
            "cap_exceeded_by_important_history": bool(
                self.max_database_bytes and database_bytes > self.max_database_bytes
            ),
        }
