"""AutopilotManager：多章连写后台会话（§13 模式2）。

新增（E4/E7/E8）：
- cancel()：协作式取消，_cancel_requested 标志由 _run_loop 各步检查
- session_budget_cap：会话级跨章 token/USD 累计封顶（非单章封顶）
- _last_heartbeat + cleanup_stale_sessions()：TTL 清理僵尸会话
"""
from __future__ import annotations

import datetime
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

    def start(self, req, registry, api_key: Optional[str] = None) -> AutopilotSession:
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
                    return
                if (session.budget_session_max_usd and
                        session.budget_usd_total >= session.budget_session_max_usd):
                    session.status = "circuit_broken"
                    session.last_error = "session USD budget exceeded"
                    session.finished_at = datetime.datetime.now(datetime.UTC).isoformat()
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

            except Exception as exc:
                from ..control_plane.budget import CircuitTripped
                with self._lock:
                    if isinstance(exc, CircuitTripped):
                        session.status = "circuit_broken"
                    else:
                        session.status = "error"
                    session.last_error = str(exc)[:500]
                    session.finished_at = datetime.datetime.now(datetime.UTC).isoformat()
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
