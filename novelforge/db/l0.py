"""L0 draft 文件原子写入 + 启动期 sweep（F6/F7）。

F7: temp→fsync→rename 防止崩溃产生孤儿文件或悬空指针。
F6: pipeline_run 状态机 — 启动期将残留 'running' 行标为 'crashed'。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path


def atomic_write_l0(l0_dir: Path, filename: str, text: str) -> tuple[Path, str]:
    """将 text 原子写入 l0_dir/filename（temp→fsync→rename）。

    Returns:
        (final_path, sha256_hex)
    """
    l0_dir.mkdir(parents=True, exist_ok=True)
    final_path = l0_dir / filename
    tmp_path = l0_dir / (filename + ".tmp")

    data = text.encode("utf-8")
    sha256_hex = hashlib.sha256(data).hexdigest()

    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())

    os.replace(str(tmp_path), str(final_path))
    return final_path, sha256_hex


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
        for row in rows:
            fp = Path(row["file_path"])
            if not fp.is_absolute():
                fp = l0_dir / fp.name  # file_path stored as "l0/filename"
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
