"""MVP1 冒烟测试：用真实 DeepSeek API 跑完整 generate_chapter() 管线。

运行：python smoke_mvp1.py
"""
import json
import os
import sqlite3
import sys
import textwrap
import time

# ── 配置 ──────────────────────────────────────────────────────────────────────
API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
CHAPTER = 1
CHAPTER_GOAL = "主角李云在宗门废弃秘境中意外觉醒，完成第一次境界突破，从凡人跨入炼气一层。"


def sep(title=""):
    print("\n" + "─" * 60)
    if title:
        print(f"  {title}")
    print("─" * 60)


def main():
    t0 = time.time()
    sep("① 初始化 DB（:memory:）")

    from novelforge.db.connection import init_db_from_conn
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db_from_conn(conn)
    print("  DB OK")

    # ── 种子数据 ─────────────────────────────────────────────────────────────
    sep("② 写入种子数据")
    from novelforge.ids import new_id

    # 实体
    eid_hero = new_id("ent")
    eid_sect = new_id("ent")
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type) VALUES(?,?,?)",
        (eid_hero, "李云", "character"),
    )
    conn.execute(
        "INSERT INTO entities(id, canonical_name, entity_type) VALUES(?,?,?)",
        (eid_sect, "青云宗", "faction"),
    )

    # 境界体系
    ranks = [
        ("凡人", 0), ("炼气一层", 1), ("炼气二层", 2), ("炼气三层", 3),
        ("炼气大圆满", 9), ("筑基初期", 10),
    ]
    for rname, rorder in ranks:
        conn.execute(
            "INSERT INTO power_ranks(id, system_name, rank_name, rank_order) VALUES(?,?,?,?)",
            (new_id("pr"), "炼气境", rname, rorder),
        )

    # 主角当前境界（凡人）
    rank_id = conn.execute(
        "SELECT id FROM power_ranks WHERE rank_name='凡人'"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO character_power_log(id, entity_id, system_name, rank_id, rank_order, change_chapter, change_type)"
        " VALUES(?,?,?,?,?,?,?)",
        (new_id("cpl"), eid_hero, "炼气境", rank_id, 0, 0, "init"),
    )
    conn.commit()
    print(f"  实体: 李云({eid_hero[:8]}…)  青云宗({eid_sect[:8]}…)")
    print(f"  境界: {len(ranks)} 级炼气境  主角当前=凡人")

    # ── 构造 Orchestrator ─────────────────────────────────────────────────────
    sep("③ 构造 Orchestrator（DeepSeek V4 Pro）")

    from novelforge.config import NovelForgeConfig
    from novelforge.control_plane.llm.factory import build_gateway
    from novelforge.control_plane.skill_registry import SkillRegistry
    from novelforge.skills import register_default_skills
    from novelforge.control_plane.orchestrator import Orchestrator

    cfg = NovelForgeConfig()
    cfg.provider.provider = "deepseek"
    cfg.provider.api_key = API_KEY
    cfg.governance.mode = "human_gate"   # 所有候选进 review_queue，不自动提交
    cfg.governance.require_human_for = []
    cfg.max_revise_loops = 0             # 冒烟不跑 REVISE 循环
    cfg.draft_target_chars = 800         # 短一点，省 token

    gw = build_gateway(cfg)
    reg = SkillRegistry()
    register_default_skills(reg)
    orch = Orchestrator(gw, reg, cfg)
    print(f"  MID 档 → {gw.model_for(__import__('novelforge.control_plane.llm.tiers', fromlist=['ModelTier']).ModelTier.MID)}")

    # ── 执行管线 ──────────────────────────────────────────────────────────────
    sep(f"④ generate_chapter({CHAPTER})")
    print(f"  章节目标: {CHAPTER_GOAL}")
    print("  调用中（预计 15-40s）…")

    outcome = orch.generate_chapter(
        CHAPTER,
        conn,
        chapter_goal=CHAPTER_GOAL,
        entity_ids=[eid_hero],
    )

    elapsed = time.time() - t0
    sep("⑤ 结果")

    if not outcome.ok:
        print(f"  ❌ 失败: {outcome.error}")
        sys.exit(1)

    print(f"  ✅ ok=True  耗时={elapsed:.1f}s")
    print(f"  tokens={outcome.usage_tokens}  USD≈${outcome.usage_usd:.4f}")
    print(f"  committed={len(outcome.fact_ids_committed)}  queued={len(outcome.candidates_queued)}")
    print(f"  issues={len(outcome.issues)}")

    sep("草稿正文（前 400 字）")
    print(textwrap.fill(outcome.draft_text[:400], width=60))
    if len(outcome.draft_text) > 400:
        print(f"  …（共 {len(outcome.draft_text)} 字）")

    sep("fact_candidates（DB）")
    cands = conn.execute(
        "SELECT candidate_id, fact_type, risk_tier, status FROM fact_candidates"
    ).fetchall()
    if cands:
        print(f"  共 {len(cands)} 条 fact_candidates：")
        for c in cands:
            print(f"    [{c['risk_tier']:6s}] {c['fact_type']:20s}  status={c['status']}")
    else:
        print("  （无候选）")

    sep("review_queue")
    rq = conn.execute("SELECT id, risk_tier, reason FROM review_queue").fetchall()
    if rq:
        for r in rq:
            print(f"  {r['id'][:12]}…  risk={r['risk_tier']}  reason={r['reason']}")
    else:
        print("  （空）")

    sep("Done")
    print(f"  MVP1 冒烟测试通过  总耗时={elapsed:.1f}s\n")


if __name__ == "__main__":
    main()
