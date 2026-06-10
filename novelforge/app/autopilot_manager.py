"""AutopilotManager：多章连写后台会话（§13 模式2）。

新增（E4/E7/E8）：
- cancel()：协作式取消，_cancel_requested 标志由 _run_loop 各步检查
- session_budget_cap：会话级跨章 token/USD 累计封顶（非单章封顶）
- _last_heartbeat + cleanup_stale_sessions()：TTL 清理僵尸会话

新增（M1-③ 持久化）：
- 会话写穿到项目库 autopilot_sessions 表（start INSERT / 每章 UPDATE / 终态 UPDATE）
- 进程重启后残留的 running/degraded 行在 list 路径被标为 'interrupted'
- resume()：从 DB 行重建启动请求，以断点章为起点开新会话（resumed_from 链回旧会话）
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from ..ids import new_id

# 会话 TTL：运行中会话超过此时间无心跳则标为 error（防客户端崩溃后僵死）
_SESSION_TTL_SECONDS = 3600  # 1 小时


@dataclass
class AutopilotSession:
    session_id: str
    project_id: str
    from_chapter: int
    to_chapter: int
    current_chapter: int
    status: str          # running / degraded / circuit_broken / completed / error / canceled
    policy_mode: str     # auto_promote / hybrid / human_gate
    chapters_done: int = 0
    budget_tokens_total: int = 0
    budget_usd_total: float = 0.0
    consecutive_hard_issues: int = 0
    last_error: Optional[str] = None
    started_at: str = ""
    finished_at: Optional[str] = None
    # 会话级预算封顶（None = 不限）
    budget_session_max_tokens: Optional[int] = None
    budget_session_max_usd: Optional[float] = None
    # 内部控制标志（不序列化）
    _degrade_requested: bool = field(default=False, repr=False)
    _cancel_requested: bool = field(default=False, repr=False)
    _last_heartbeat: float = field(default_factory=time.time, repr=False)

    @property
    def chapters_total(self) -> int:
        return self.to_chapter - self.from_chapter + 1

    @property
    def pending_reviews(self) -> int:
        return 0  # 调用侧查 DB，这里不缓存

    def _touch(self) -> None:
        """更新心跳时间戳（每章完成后调用）。"""
        self._last_heartbeat = time.time()

    def _is_stale(self) -> bool:
        """超过 TTL 且仍在运行中 → 视为僵尸。"""
        if self.status not in ("running", "degraded"):
            return False
        return (time.time() - self._last_heartbeat) > _SESSION_TTL_SECONDS


class AutopilotManager:
    def __init__(self):
        self._sessions: dict[str, AutopilotSession] = {}
        self._lock = threading.Lock()

    # ── 公开 API ──────────────────────────────────────────────────────────────

    def start(
        self, req, registry, api_key: Optional[str] = None,
        *, resumed_from: Optional[str] = None,
    ) -> AutopilotSession:
        sid = new_id("aps")
        session = AutopilotSession(
            session_id=sid,
            project_id=req.project_id,
            from_chapter=req.from_chapter,
            to_chapter=req.to_chapter,
            current_chapter=req.from_chapter,
            status="running",
            policy_mode=req.mode,
            started_at=datetime.datetime.now(datetime.UTC).isoformat(),
            budget_session_max_tokens=getattr(req, "budget_session_max_tokens", None),
            budget_session_max_usd=getattr(req, "budget_session_max_usd", None),
        )
        with self._lock:
            self._sessions[sid] = session
        self._db_insert(session, req, registry, resumed_from=resumed_from)
        thread = threading.Thread(
            target=self._run_loop,
            args=(session, req, registry, api_key),
            daemon=True,
            name=f"autopilot-{sid}",
        )
        thread.start()
        return session

    def get(self, session_id: str) -> Optional[AutopilotSession]:
        return self._sessions.get(session_id)

    def list_for_project(self, project_id: str) -> list[AutopilotSession]:
        return [s for s in self._sessions.values() if s.project_id == project_id]

    def degrade(self, session_id: str, reason: str) -> bool:
        """请求降级到 human_gate；已完成则返回 False。"""
        with self._lock:
            s = self._sessions.get(session_id)
            if s and s.status == "running":
                s._degrade_requested = True
                return True
        return False

    def cancel(self, session_id: str) -> bool:
        """协作式取消：设置取消标志，_run_loop 在每章开始前检查。

        Returns:
            True  → 会话存在且处于可取消状态（running/degraded），已设置标志
            False → 会话不存在或已结束（可幂等调用）
        """
        with self._lock:
            s = self._sessions.get(session_id)
            if s and s.status in ("running", "degraded"):
                s._cancel_requested = True
                return True
        return False

    def cleanup_stale_sessions(self) -> list[str]:
        """将超过 TTL 的僵尸会话标记为 error，返回已清理的 session_id 列表。"""
        cleaned = []
        with self._lock:
            for sid, s in self._sessions.items():
                if s._is_stale():
                    s.status = "error"
                    s.last_error = "session TTL exceeded — possible client crash"
                    s.finished_at = datetime.datetime.now(datetime.UTC).isoformat()
                    cleaned.append(sid)
        return cleaned

    # ── 持久化（M1-③）─────────────────────────────────────────────────────────

    def load_db_sessions(self, project_id: str, conn) -> list[AutopilotSession]:
        """读取项目库中**不在内存里**的历史会话（进程重启后找回）。

        DB 中仍为 running/degraded 的残留行说明承载线程已随旧进程消失，
        现场标为 'interrupted' 供用户显式 resume。
        """
        try:
            rows = conn.execute(
                "SELECT * FROM autopilot_sessions WHERE project_id=?"
                " ORDER BY started_at DESC LIMIT 20",
                (project_id,),
            ).fetchall()
        except Exception:
            return []
        out: list[AutopilotSession] = []
        for r in rows:
            sid = r["session_id"]
            with self._lock:
                if sid in self._sessions:
                    continue  # 内存中活着的会话以内存为准
            status = r["status"]
            if status in ("running", "degraded"):
                status = "interrupted"
                try:
                    conn.execute(
                        "UPDATE autopilot_sessions SET status='interrupted',"
                        " finished_at=COALESCE(finished_at, datetime('now'))"
                        " WHERE session_id=?",
                        (sid,),
                    )
                    conn.commit()
                except Exception:
                    pass
            out.append(AutopilotSession(
                session_id=sid,
                project_id=r["project_id"],
                from_chapter=r["from_chapter"],
                to_chapter=r["to_chapter"],
                current_chapter=r["current_chapter"],
                status=status,
                policy_mode=r["policy_mode"],
                chapters_done=r["chapters_done"],
                budget_tokens_total=r["budget_tokens_total"],
                budget_usd_total=r["budget_usd_total"],
                consecutive_hard_issues=r["consecutive_hard_issues"],
                last_error=r["last_error"],
                started_at=r["started_at"],
                finished_at=r["finished_at"],
            ))
        return out

    def get_db_session_req(self, session_id: str, conn) -> Optional[dict]:
        """读取持久化行的启动参数（resume 用）。返回 None = 行不存在。"""
        try:
            row = conn.execute(
                "SELECT req_json, current_chapter, to_chapter, status"
                " FROM autopilot_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
        except Exception:
            return None
        if row is None:
            return None
        try:
            req = json.loads(row["req_json"] or "{}")
        except Exception:
            req = {}
        return {
            "req": req,
            "current_chapter": row["current_chapter"],
            "to_chapter": row["to_chapter"],
            "status": row["status"],
        }

    def _db_insert(self, session: AutopilotSession, req, registry,
                   *, resumed_from: Optional[str] = None) -> None:
        try:
            req_json = json.dumps({
                "chapter_goals": getattr(req, "chapter_goals", {}) or {},
                "mode": session.policy_mode,
                "budget_max_tokens_per_chapter": getattr(req, "budget_max_tokens_per_chapter", None),
                "budget_max_usd_per_chapter": getattr(req, "budget_max_usd_per_chapter", None),
                "budget_session_max_tokens": session.budget_session_max_tokens,
                "budget_session_max_usd": session.budget_session_max_usd,
                "auto_degrade_after_consecutive_issues": getattr(req, "auto_degrade_after_consecutive_issues", 2),
            }, ensure_ascii=False)
            conn = registry.open_conn(session.project_id)
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO autopilot_sessions"
                    "(session_id, project_id, from_chapter, to_chapter, current_chapter,"
                    " status, policy_mode, req_json, resumed_from, started_at, heartbeat_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now'))",
                    (session.session_id, session.project_id, session.from_chapter,
                     session.to_chapter, session.current_chapter, session.status,
                     session.policy_mode, req_json, resumed_from, session.started_at),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass  # 持久化失败不阻断连写（与 _persist_draft 容错风格一致）

    def _db_update(self, session: AutopilotSession, registry) -> None:
        try:
            conn = registry.open_conn(session.project_id)
            try:
                conn.execute(
                    "UPDATE autopilot_sessions SET"
                    " current_chapter=?, status=?, policy_mode=?, chapters_done=?,"
                    " budget_tokens_total=?, budget_usd_total=?,"
                    " consecutive_hard_issues=?, last_error=?, finished_at=?,"
                    " heartbeat_at=datetime('now')"
                    " WHERE session_id=?",
                    (session.current_chapter, session.status, session.policy_mode,
                     session.chapters_done, session.budget_tokens_total,
                     round(session.budget_usd_total, 6),
                     session.consecutive_hard_issues, session.last_error,
                     session.finished_at, session.session_id),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    # ── 内部循环 ──────────────────────────────────────────────────────────────

    def _run_loop(self, session: AutopilotSession, req, registry, api_key: Optional[str]):
        from ..config import NovelForgeConfig
        from ..control_plane.budget import BudgetLedger, CircuitTripped
        from ..control_plane.llm.factory import build_gateway
        from ..control_plane.orchestrator import Orchestrator
        from ..control_plane.skill_registry import SkillRegistry
        from ..skills import register_default_skills

        for chapter in range(session.from_chapter, session.to_chapter + 1):
            with self._lock:
                # 检查取消标志
                if session._cancel_requested:
                    session.status = "canceled"
                    session.finished_at = datetime.datetime.now(datetime.UTC).isoformat()
                    self._db_update(session, registry)
                    return
                # 检查降级标志
                if session._degrade_requested and session.policy_mode != "human_gate":
                    session.policy_mode = "human_gate"
                    session.status = "degraded"
                    session._degrade_requested = False
                elif session.status not in ("running", "degraded"):
                    break
                # 检查会话级预算封顶（E4 跨章累计）
                if (session.budget_session_max_tokens and
                        session.budget_tokens_total >= session.budget_session_max_tokens):
                    session.status = "circuit_broken"
                    session.last_error = "session token budget exceeded"
                    session.finished_at = datetime.datetime.now(datetime.UTC).isoformat()
                    self._db_update(session, registry)
                    return
                if (session.budget_session_max_usd and
                        session.budget_usd_total >= session.budget_session_max_usd):
                    session.status = "circuit_broken"
                    session.last_error = "session USD budget exceeded"
                    session.finished_at = datetime.datetime.now(datetime.UTC).isoformat()
                    self._db_update(session, registry)
                    return

            conn = None
            try:
                conn = registry.open_conn(session.project_id)
                cfg = NovelForgeConfig(
                    project_id=session.project_id,
                    db_path=registry.get(session.project_id).db_path if registry.get(session.project_id) else "novel.db",
                )
                cfg.governance.mode = session.policy_mode
                if req.budget_max_tokens_per_chapter:
                    cfg.budget.max_tokens_per_chapter = req.budget_max_tokens_per_chapter
                if req.budget_max_usd_per_chapter:
                    cfg.budget.max_usd_per_chapter = req.budget_max_usd_per_chapter
                if api_key:
                    cfg.provider.api_key = api_key
                    cfg.provider.provider = os.environ.get("NOVELFORGE_PROVIDER", "deepseek")

                gw = build_gateway(cfg)
                reg = SkillRegistry()
                register_default_skills(reg)
                orch = Orchestrator(gw, reg, cfg)

                chapter_goal = req.chapter_goals.get(str(chapter)) or req.chapter_goals.get(chapter) or ""
                if not chapter_goal:
                    # 未显式指定目标 → 按章节卡/上一章钩子/卷大纲/到期伏笔/已计划节拍自动拼装
                    try:
                        from .chapter_suggest import assemble_chapter_goal
                        chapter_goal, _ = assemble_chapter_goal(conn, chapter)
                    except Exception:
                        chapter_goal = ""
                outcome = orch.generate_chapter(chapter, conn, chapter_goal=chapter_goal)

                with self._lock:
                    # 跨章累计（E4：会话级 budget 累加）
                    session.budget_tokens_total += outcome.usage_tokens
                    session.budget_usd_total += outcome.usage_usd
                    session.chapters_done += 1
                    session.current_chapter = chapter + 1
                    session._touch()

                    hard_issues = [i for i in (outcome.issues or []) if i.get("severity") == "block"]
                    if hard_issues:
                        session.consecutive_hard_issues += 1
                        if session.consecutive_hard_issues >= req.auto_degrade_after_consecutive_issues:
                            session.policy_mode = "human_gate"
                            if session.status == "running":
                                session.status = "degraded"
                    else:
                        session.consecutive_hard_issues = 0
                    self._db_update(session, registry)  # 每章一次写穿（天然 checkpoint）

            except Exception as exc:
                from ..control_plane.budget import CircuitTripped
                with self._lock:
                    if isinstance(exc, CircuitTripped):
                        session.status = "circuit_broken"
                    else:
                        session.status = "error"
                    session.last_error = str(exc)[:500]
                    session.finished_at = datetime.datetime.now(datetime.UTC).isoformat()
                    self._db_update(session, registry)
                break
            finally:
                if conn:
                    try:
                        conn.close()
                    except Exception:
                        pass

        with self._lock:
            if session.status in ("running", "degraded"):
                session.status = "completed"
            if session.finished_at is None:
                session.finished_at = datetime.datetime.now(datetime.UTC).isoformat()
            self._db_update(session, registry)


# 全局单例（与 deps._registry 模式一致）
_manager: Optional[AutopilotManager] = None
_manager_lock = threading.Lock()


def get_autopilot_manager() -> AutopilotManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = AutopilotManager()
    return _manager
