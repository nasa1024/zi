"""L0 draft 文件原子写入 + 启动期 sweep（F6/F7）。

F7: temp→fsync→rename 防止崩溃产生孤儿文件或悬空指针。
F6: pipeline_run 状态机 — 启动期将残留 'running' 行标为 'crashed'。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Optional


def atomic_write_l0(l0_dir: Path, filename: str, text: str) -> tuple[Path, str]:
    """将 text 原子写入 l0_dir/filename（temp→fsync→rename）。

    Returns:
        (final_path, sha256_hex)
    """
    project_root = l0_dir.parent.resolve()
    resolved_l0_dir = l0_dir.resolve()
    try:
        resolved_l0_dir.relative_to(project_root)
    except ValueError as exc:
        raise ValueError(f"l0 目录越出项目根目录: {l0_dir}") from exc
    l0_dir = resolved_l0_dir
    l0_dir.mkdir(parents=True, exist_ok=True)
    final_path = (l0_dir / filename).resolve()
    try:
        final_path.relative_to(l0_dir)
    except ValueError as exc:
        raise ValueError(f"L0 文件名越出目录: {filename}") from exc

    data = text.encode("utf-8")
    sha256_hex = hashlib.sha256(data).hexdigest()

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{filename}.", suffix=".tmp", dir=str(l0_dir)
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(final_path))
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return final_path, sha256_hex


def database_file(conn: sqlite3.Connection) -> Optional[Path]:
    """返回当前连接 main 数据库的绝对路径；内存库返回 None。"""
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            # sqlite3.Row 与 tuple 两种 row_factory 都兼容。
            name = row[1]
            file_name = row[2]
            if name == "main" and file_name:
                return Path(file_name).resolve()
    except Exception:
        pass
    return None


def resolve_draft_path(
    conn: sqlite3.Connection,
    file_path: str | Path,
    *,
    db_path: str | Path | None = None,
) -> Path:
    """把 draft_index.file_path 解析为项目隔离的真实路径。

    `draft_index` 为可迁移性保存 `l0/filename` 相对路径。相对路径必须相对
    novel.db 所在项目目录解析，绝不能相对进程 CWD，否则不同项目的同名章节会
    串读。内存库没有数据库路径时保留 CWD 回退，主要用于历史测试兼容。
    """
    db_file = Path(db_path).resolve() if db_path else database_file(conn)
    base = db_file.parent if db_file is not None else Path.cwd()
    base = base.resolve()
    l0_root = (base / "l0").resolve()
    try:
        l0_root.relative_to(base)
    except ValueError as exc:
        raise ValueError("项目 l0 目录解析到项目根目录之外") from exc
    path = Path(file_path)
    resolved = path.resolve() if path.is_absolute() else (base / path).resolve()
    try:
        resolved.relative_to(l0_root)
    except ValueError as exc:
        raise ValueError(f"draft file_path 越出项目 l0 目录: {file_path}") from exc
    return resolved


def load_draft_text_by_id(
    conn: sqlite3.Connection,
    draft_id: str,
    *,
    db_path: str | Path | None = None,
) -> Optional[str]:
    """按 draft_id 读取 L0 正文；文件缺失/不可读时返回 None。"""
    try:
        row = conn.execute(
            "SELECT file_path FROM draft_index WHERE id=?", (draft_id,)
        ).fetchone()
        if row is None or not row["file_path"]:
            return None
        return resolve_draft_path(conn, row["file_path"], db_path=db_path).read_text(
            encoding="utf-8"
        )
    except Exception:
        return None


def load_latest_draft_text(
    conn: sqlite3.Connection,
    chapter: int,
    *,
    db_path: str | Path | None = None,
) -> Optional[str]:
    """读取某章最新修订的 L0 正文。"""
    try:
        row = conn.execute(
            "SELECT id FROM draft_index WHERE chapter=?"
            " ORDER BY revision_round DESC LIMIT 1",
            (chapter,),
        ).fetchone()
        if row is None:
            return None
        return load_draft_text_by_id(conn, row["id"], db_path=db_path)
    except Exception:
        return None


def sweep_orphans(conn: sqlite3.Connection, l0_dir: Path) -> dict:
    """启动期孤儿对账（F7）。

    - 清理 l0_dir 中残留的 .tmp 文件（崩溃写入的半成品）
    - 检测 draft_index 中指向不存在文件的行（只记录，不修改状态）

    Returns:
        {"orphaned_db_rows": int, "deleted_tmp_files": int}
    """
    orphaned_ids: list[str] = []
    try:
        rows = conn.execute(
            "SELECT id, file_path FROM draft_index"
        ).fetchall()
        synthetic_db = l0_dir.parent / "novel.db"
        for row in rows:
            try:
                fp = resolve_draft_path(conn, row["file_path"], db_path=synthetic_db)
            except (TypeError, ValueError):
                orphaned_ids.append(row["id"])
                continue
            if not fp.exists():
                orphaned_ids.append(row["id"])
    except Exception:
        pass

    # 清理 .tmp 文件
    deleted_tmp = 0
    try:
        if l0_dir.exists():
            for f in l0_dir.iterdir():
                if f.suffix == ".tmp":
                    try:
                        f.unlink()
                        deleted_tmp += 1
                    except OSError:
                        pass
    except Exception:
        pass

    return {"orphaned_db_rows": len(orphaned_ids), "deleted_tmp_files": deleted_tmp}


def sweep_crashed_runs(conn: sqlite3.Connection) -> list[str]:
    """启动期崩溃扫描（F6）。

    将 pipeline_run 中残留的 status='running' 行标为 'crashed'。
    这些行代表进程上次崩溃时未完成的 generate_chapter() 调用。

    Returns:
        已标记为 crashed 的 run_id 列表。
    """
    try:
        rows = conn.execute(
            "SELECT run_id FROM pipeline_run WHERE status='running'"
        ).fetchall()
        run_ids = [r["run_id"] for r in rows]
        if run_ids:
            conn.executemany(
                "UPDATE pipeline_run"
                " SET status='crashed', finished_at=datetime('now')"
                " WHERE run_id=?",
                [(rid,) for rid in run_ids],
            )
            conn.commit()
        return run_ids
    except Exception:
        return []


def sweep_stale_batches(conn: sqlite3.Connection) -> list[str]:
    """启动期连写批次扫描（v22/F6）。

    进程崩溃时仍为 ``status='running'`` 的 ``draft_batches`` 行标为 ``failed``
    （detail_json.failure_reason='interrupted'），其下尚未处理的 ``pending`` 章标为
    ``discarded``。这些正文从未进入 ``draft_index``，丢弃不影响章节进度。

    Returns:
        已标记为 failed 的 batch id 列表（老库缺表时为空）。
    """
    try:
        rows = conn.execute(
            "SELECT id FROM draft_batches WHERE status='running'"
        ).fetchall()
        batch_ids = [r[0] for r in rows]
        for batch_id in batch_ids:
            conn.execute(
                "UPDATE draft_batches SET status='failed',"
                " detail_json=json_set(CASE WHEN json_valid(detail_json) THEN detail_json"
                " ELSE '{}' END, '$.failure_reason', 'interrupted'),"
                " updated_at=datetime('now') WHERE id=?",
                (batch_id,),
            )
            conn.execute(
                "UPDATE draft_batch_chapters SET status='discarded',"
                " reason=COALESCE(reason, 'interrupted'), updated_at=datetime('now')"
                " WHERE batch_id=? AND status='pending'",
                (batch_id,),
            )
        if batch_ids:
            conn.commit()
        return batch_ids
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        return []
