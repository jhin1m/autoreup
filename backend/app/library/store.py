"""Repository over the library SQLite DB — the only place that knows SQL.

Every method opens a short connection via :class:`LibraryDB`, maps rows to the pydantic
models in ``models.py``, and returns those. The service layer composes these; nothing
above this file touches SQL.
"""

import json
from uuid import uuid4

from backend.app.library.db import LibraryDB
from backend.app.library.models import (
    DubState,
    Episode,
    Export,
    ExportState,
    Series,
    SeriesSource,
    now_iso,
)
from backend.app.studio.models import Platform


def _new_id() -> str:
    return uuid4().hex[:12]


class LibraryStore:
    def __init__(self, db: LibraryDB):
        self.db = db

    # ----- series ---------------------------------------------------------
    def create_series(self, title: str, cover_url: str | None = None, tags: list[str] | None = None) -> Series:
        series = Series(id=_new_id(), title=title.strip() or "Series mới", cover_url=cover_url, tags=tags or [])
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO series(id, title, cover_url, tags, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (series.id, series.title, series.cover_url, json.dumps(series.tags), series.created_at, series.updated_at),
            )
        return series

    def list_series(self, q: str = "", limit: int = 60, offset: int = 0) -> tuple[list[Series], int]:
        where, params = "", []
        if q.strip():
            where = "WHERE s.title LIKE ?"
            params.append(f"%{q.strip()}%")
        with self.db.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM series s {where}", params).fetchone()[0]
            rows = conn.execute(
                f"""
                SELECT s.*,
                  (SELECT COUNT(*) FROM episode e WHERE e.series_id = s.id) AS episode_count,
                  (SELECT COUNT(*) FROM episode e WHERE e.series_id = s.id AND e.dub_state = 'dubbed') AS dubbed_count,
                  (SELECT COUNT(*) FROM export x WHERE x.series_id = s.id) AS export_count
                FROM series s {where}
                ORDER BY s.updated_at DESC
                LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return [self._row_to_series(r) for r in rows], int(total)

    def get_series(self, series_id: str, *, with_sources: bool = True) -> Series | None:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT s.*,
                  (SELECT COUNT(*) FROM episode e WHERE e.series_id = s.id) AS episode_count,
                  (SELECT COUNT(*) FROM episode e WHERE e.series_id = s.id AND e.dub_state = 'dubbed') AS dubbed_count,
                  (SELECT COUNT(*) FROM export x WHERE x.series_id = s.id) AS export_count
                FROM series s WHERE s.id = ?
                """,
                (series_id,),
            ).fetchone()
            if not row:
                return None
            series = self._row_to_series(row)
            if with_sources:
                series.sources = self._list_sources(conn, series_id)
        return series

    def update_series(self, series_id: str, **changes) -> Series | None:
        allowed = {"title", "cover_url", "tags"}
        sets, params = [], []
        for key, value in changes.items():
            if key not in allowed or value is None:
                continue
            sets.append(f"{key} = ?")
            params.append(json.dumps(value) if key == "tags" else value)
        if sets:
            sets.append("updated_at = ?")
            params.append(now_iso())
            with self.db.connect() as conn:
                conn.execute(f"UPDATE series SET {', '.join(sets)} WHERE id = ?", [*params, series_id])
        return self.get_series(series_id)

    def touch_series(self, series_id: str) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE series SET updated_at = ? WHERE id = ?", (now_iso(), series_id))

    def delete_series(self, series_id: str) -> bool:
        with self.db.connect() as conn:
            cur = conn.execute("DELETE FROM series WHERE id = ?", (series_id,))
            return cur.rowcount > 0

    # ----- sources --------------------------------------------------------
    def add_source(self, series_id: str, platform: Platform, url: str, label: str | None = None) -> SeriesSource:
        source = SeriesSource(id=_new_id(), series_id=series_id, platform=platform, url=url, label=label)
        with self.db.connect() as conn:
            conn.execute(
                "INSERT INTO series_source(id, series_id, platform, url, label, last_scanned_at) VALUES (?,?,?,?,?,?)",
                (source.id, series_id, platform.value, url, label, None),
            )
        self.touch_series(series_id)
        return source

    def list_sources(self, series_id: str) -> list[SeriesSource]:
        with self.db.connect() as conn:
            return self._list_sources(conn, series_id)

    def delete_source(self, source_id: str) -> bool:
        with self.db.connect() as conn:
            cur = conn.execute("DELETE FROM series_source WHERE id = ?", (source_id,))
            return cur.rowcount > 0

    def mark_source_scanned(self, source_id: str) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE series_source SET last_scanned_at = ? WHERE id = ?", (now_iso(), source_id))

    def series_ids_for_source_url(self, url: str) -> list[str]:
        """Which series already pull from this channel link (to sync, not duplicate)."""
        with self.db.connect() as conn:
            rows = conn.execute("SELECT DISTINCT series_id FROM series_source WHERE url = ?", (url,)).fetchall()
            return [r[0] for r in rows]

    # ----- episodes -------------------------------------------------------
    def upsert_episodes(self, series_id: str, episodes: list[Episode]) -> int:
        """Insert new episodes; existing ones (same series+platform+video id) are kept
        (we never clobber dub state on re-scan). Returns the count of NEW rows."""
        added = 0
        with self.db.connect() as conn:
            for ep in episodes:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO episode
                      (id, series_id, platform, platform_video_id, source_url, title,
                       thumbnail_url, duration, uploader, discovered_at, dub_job_id, dub_state)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (ep.id, series_id, ep.platform.value, ep.platform_video_id, ep.source_url, ep.title,
                     ep.thumbnail_url, ep.duration, ep.uploader, ep.discovered_at, ep.dub_job_id, ep.dub_state.value),
                )
                added += cur.rowcount
        if added:
            self.touch_series(series_id)
        return added

    def list_episodes(self, series_id: str, dub_state: DubState | None = None) -> list[Episode]:
        sql = "SELECT * FROM episode WHERE series_id = ?"
        params: list = [series_id]
        if dub_state is not None:
            sql += " AND dub_state = ?"
            params.append(dub_state.value)
        sql += " ORDER BY discovered_at DESC"
        with self.db.connect() as conn:
            return [self._row_to_episode(r) for r in conn.execute(sql, params).fetchall()]

    def get_episode(self, episode_id: str) -> Episode | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM episode WHERE id = ?", (episode_id,)).fetchone()
            return self._row_to_episode(row) if row else None

    def set_episode_dub(self, episode_id: str, job_id: str | None, state: DubState) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "UPDATE episode SET dub_job_id = ?, dub_state = ? WHERE id = ?",
                (job_id, state.value, episode_id),
            )

    def set_episode_dub_by_job(self, job_id: str, state: DubState) -> None:
        """Called when a job changes state — keeps the episode's cached dub_state fresh."""
        with self.db.connect() as conn:
            conn.execute("UPDATE episode SET dub_state = ? WHERE dub_job_id = ?", (state.value, job_id))

    # ----- exports --------------------------------------------------------
    def create_export(self, series_id: str, title: str, description: str, hashtags: list[str], episode_job_ids: list[str]) -> Export:
        export = Export(
            id=_new_id(), series_id=series_id, title=title.strip() or "Bản ghép", description=description,
            hashtags=hashtags, episode_job_ids=episode_job_ids,
        )
        with self.db.connect() as conn:
            conn.execute(
                """INSERT INTO export(id, series_id, title, description, hashtags, episode_job_ids,
                       output_path, duration, state, error, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (export.id, series_id, export.title, export.description, json.dumps(export.hashtags),
                 json.dumps(export.episode_job_ids), None, None, export.state.value, None,
                 export.created_at, export.updated_at),
            )
        self.touch_series(series_id)
        return export

    def get_export(self, export_id: str) -> Export | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM export WHERE id = ?", (export_id,)).fetchone()
            return self._row_to_export(row) if row else None

    def list_exports(self, series_id: str) -> list[Export]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM export WHERE series_id = ? ORDER BY created_at DESC", (series_id,)).fetchall()
            return [self._row_to_export(r) for r in rows]

    def update_export(self, export_id: str, **changes) -> Export | None:
        allowed = {"title", "description", "hashtags", "episode_job_ids", "output_path", "duration", "state", "error"}
        sets, params = [], []
        for key, value in changes.items():
            if key not in allowed:
                continue
            sets.append(f"{key} = ?")
            params.append(json.dumps(value) if key in {"hashtags", "episode_job_ids"} else value)
        if sets:
            sets.append("updated_at = ?")
            params.append(now_iso())
            with self.db.connect() as conn:
                conn.execute(f"UPDATE export SET {', '.join(sets)} WHERE id = ?", [*params, export_id])
        return self.get_export(export_id)

    def delete_export(self, export_id: str) -> bool:
        with self.db.connect() as conn:
            cur = conn.execute("DELETE FROM export WHERE id = ?", (export_id,))
            return cur.rowcount > 0

    # ----- row mappers ----------------------------------------------------
    @staticmethod
    def _list_sources(conn, series_id: str) -> list[SeriesSource]:
        rows = conn.execute("SELECT * FROM series_source WHERE series_id = ? ORDER BY rowid", (series_id,)).fetchall()
        return [
            SeriesSource(id=r["id"], series_id=r["series_id"], platform=Platform(r["platform"]),
                         url=r["url"], label=r["label"], last_scanned_at=r["last_scanned_at"])
            for r in rows
        ]

    @staticmethod
    def _row_to_series(r) -> Series:
        keys = r.keys()
        return Series(
            id=r["id"], title=r["title"], cover_url=r["cover_url"], tags=json.loads(r["tags"] or "[]"),
            created_at=r["created_at"], updated_at=r["updated_at"],
            episode_count=r["episode_count"] if "episode_count" in keys else 0,
            dubbed_count=r["dubbed_count"] if "dubbed_count" in keys else 0,
            export_count=r["export_count"] if "export_count" in keys else 0,
        )

    def set_episode_title_vi(self, episode_id: str, title_vi: str) -> None:
        with self.db.connect() as conn:
            conn.execute("UPDATE episode SET title_vi = ? WHERE id = ?", (title_vi, episode_id))

    @staticmethod
    def _row_to_episode(r) -> Episode:
        keys = r.keys()
        return Episode(
            id=r["id"], series_id=r["series_id"], platform=Platform(r["platform"]),
            platform_video_id=r["platform_video_id"], source_url=r["source_url"], title=r["title"],
            title_vi=r["title_vi"] if "title_vi" in keys else None,
            thumbnail_url=r["thumbnail_url"], duration=r["duration"], uploader=r["uploader"],
            discovered_at=r["discovered_at"], dub_job_id=r["dub_job_id"], dub_state=DubState(r["dub_state"]),
        )

    @staticmethod
    def _row_to_export(r) -> Export:
        return Export(
            id=r["id"], series_id=r["series_id"], title=r["title"], description=r["description"],
            hashtags=json.loads(r["hashtags"] or "[]"), episode_job_ids=json.loads(r["episode_job_ids"] or "[]"),
            output_path=r["output_path"], duration=r["duration"], state=ExportState(r["state"]),
            error=r["error"], created_at=r["created_at"], updated_at=r["updated_at"],
        )
