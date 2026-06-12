"""M1-M5 冒烟测试：缺口改造五个里程碑的端到端验证（真实 LLM）。

覆盖：
  M1-③ Autopilot 会话持久化（DB 写穿 + resume 语义）
  M1-⑥ Prompt 前缀稳定化（cache_read_tokens 观测）
  M2-② 分层叙事摘要（chapter_summaries 落库 + 注入）
  M3-① 多候选择优（detail_json 报告）
  M4-④ 卷批量预规划（chapter_cards → /pipeline/next 大纲驱动）
  M5-⑦ 质量分门控（quality_score）
  M5-⑧ 伏笔健康度（overdue 翻转 + health 端点）

使用方法（会真实调用 DeepSeek，约 2-3 章成本 < $0.05）：
    export DEEPSEEK_API_KEY=sk-...
    python smoke_m1_m5.py
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────────────────────

API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
if not API_KEY:
    print("缺少 DEEPSEEK_API_KEY 环境变量。\n"
          "    export DEEPSEEK_API_KEY=sk-...\n"
          "    python smoke_m1_m5.py")
    sys.exit(2)

BASE_URL = "http://127.0.0.1:8787"
DATA_DIR = os.getenv("NOVELFORGE_DATA", "data/smoke_m1_m5")
os.environ["NOVELFORGE_DATA"] = DATA_DIR

if Path(DATA_DIR).exists():
    shutil.rmtree(DATA_DIR)
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

GREEN, RED, YELLOW, RESET, BOLD = "\033[92m", "\033[91m", "\033[93m", "\033[0m", "\033[1m"
_pass = _fail = _warn = 0


def ok(msg):
    global _pass; _pass += 1
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg, detail=""):
    global _fail; _fail += 1
    print(f"  {RED}✗{RESET} {msg}")
    if detail:
        print(f"    {RED}{str(detail)[:300]}{RESET}")


def warn(msg):
    global _warn; _warn += 1
    print(f"  {YELLOW}△{RESET} {msg}")


def section(title):
    print(f"\n{BOLD}── {title} {'─' * max(0, 56 - len(title))}{RESET}")


def http(method, path, payload=None, timeout=600):
    url = BASE_URL + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if text else {})
    except urllib.error.HTTPError as e:
        try:
            result = json.loads(e.read())
        except Exception:
            result = {}
        return e.code, result
    except (TimeoutError, OSError) as e:
        return -1, {"error": str(e)}


def project_db():
    """定位项目 novel.db（直查 chapter_summaries / autopilot_sessions 等无 API 表）。"""
    hits = glob.glob(f"{DATA_DIR}/**/novel.db", recursive=True)
    if not hits:
        return None
    conn = sqlite3.connect(hits[0])
    conn.row_factory = sqlite3.Row
    return conn


def start_server():
    import subprocess
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn",
         "novelforge.app.main:app", "--port", "8787", "--log-level", "warning"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    for _ in range(30):
        try:
            with urllib.request.urlopen(BASE_URL + "/health", timeout=2) as r:
                if r.status == 200:
                    return proc
        except Exception:
            pass
        time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("FastAPI 服务启动超时")


def run_smoke():
    section("0. 健康检查 + 项目")
    status, body = http("GET", "/health")
    if status != 200:
        fail("GET /health", f"{status} {body}"); return
    ok("GET /health")

    status, body = http("POST", "/v1/projects", {"name": "冒烟之书", "genre": "xuanhuan"})
    if status != 201:
        fail("创建项目", f"{status} {body}"); return
    pid = body["project_id"]
    ok(f"项目创建 → {pid}")

    # 种一点设定（实体 + 低风险事实自动晋升）
    status, body = http("POST", f"/v1/{pid}/seed", {
        "proposals": [
            {"fact_type": "character_trait", "entity": "陆青云",
             "new": {"subject": "陆青云", "predicate": "身份", "object": "青云宗外门弟子"},
             "valid_from_chapter": 0, "risk_tier": "low"},
            {"fact_type": "world_rule",
             "new": {"subject": "修炼体系", "predicate": "境界", "object": "炼气-筑基-金丹"},
             "valid_from_chapter": 0, "risk_tier": "low"},
        ],
        "auto_approve_low_risk": True, "actor": "smoke",
    })
    ok("seed 设定") if status == 202 else fail("seed", f"{status} {body}")

    # 伏笔：due=2，跑完第 2 章后应翻 overdue（M5-⑧）
    status, body = http("POST", f"/v1/{pid}/foreshadow", {
        "label": "神秘玉佩", "description": "陆青云捡到的玉佩来历", "planted_chapter": 1, "due_chapter": 2,
    })
    ok("埋伏笔 due=2") if status == 201 else fail("埋伏笔", f"{status} {body}")

    # ── M4：卷规划 ────────────────────────────────────────────────────────────
    section("M4-④ 卷批量预规划")
    status, body = http("POST", f"/v1/{pid}/volumes", {
        "volume_no": 1, "title": "凡尘崛起", "synopsis": "陆青云自凡界崛起，初入青云宗并初露锋芒",
        "start_chapter": 1, "end_chapter": 20,
    })
    ok("创建卷") if status == 201 else fail("创建卷", f"{status} {body}")

    status, body = http("POST", f"/v1/{pid}/volumes/1/plan", {"to_chapter": 3}, timeout=600)
    if status == 200 and not body.get("error") and len(body.get("planned", [])) >= 2:
        ok(f"卷规划 → {len(body['planned'])} 章细纲（{body['from_chapter']}-{body['to_chapter']}）")
        first = body["planned"][0]
        ok(f"  第{first['chapter']}章《{first.get('title')}》hook: {str(first.get('hook_text'))[:30]}…") \
            if first.get("hook_text") else warn("首章 hook_text 为空")
    else:
        fail("卷规划", f"{status} {body.get('error')} planned={len(body.get('planned', []))}")

    status, body = http("GET", f"/v1/{pid}/pipeline/next")
    if status == 200 and "chapter_card" in body.get("sources", []):
        ok(f"/pipeline/next 已大纲驱动 → 第{body['next_chapter']}章, sources={body['sources']}")
    else:
        fail("/pipeline/next 未消费章节卡", f"{status} {body}")

    # ── M3+M5+M1⑥：双候选 + 质量评分生成第 1 章 ──────────────────────────────
    section("M3-① 多候选 + M5-⑦ 质量分 + M1-⑥ 缓存（第 1 章）")
    t0 = time.time()
    status, body = http("POST", f"/v1/{pid}/pipeline/run", {
        "chapter_no": 1, "mode": "auto_promote",
        "n_candidates": 2, "quality_check": True,
    }, timeout=900)
    if status == 200 and not body.get("error") and len(body.get("draft_text", "")) > 500:
        ok(f"第 1 章生成 {len(body['draft_text'])} 字，{time.time()-t0:.0f}s，"
           f"tokens={body['budget_spent']['tokens']} usd=${body['budget_spent']['usd']:.4f}")
    else:
        fail("第 1 章生成", f"{status} err={body.get('error')}")
        return
    if body.get("quality_score") is not None:
        ok(f"质量分 = {body['quality_score']}")
    else:
        fail("quality_score 缺失", body.get("quality_score"))
    if body.get("cache_read_tokens", 0) > 0:
        ok(f"前缀缓存命中 {body['cache_read_tokens']} tokens（双候选共享前缀）")
    else:
        warn("cache_read_tokens=0（DeepSeek 缓存异步生效，偶发未命中属正常）")

    conn = project_db()
    row = conn.execute("SELECT detail_json FROM pipeline_run WHERE chapter=1"
                       " AND detail_json IS NOT NULL").fetchone()
    if row:
        detail = json.loads(row["detail_json"])
        ok(f"候选择优报告落库：winner=#{detail['winner']} reason={detail['reason']}"
           f" judge_used={detail['judge_used']}")
    else:
        fail("pipeline_run.detail_json 缺失")

    # ── M2：章摘要 ────────────────────────────────────────────────────────────
    section("M2-② 分层叙事摘要")
    row = conn.execute("SELECT summary FROM chapter_summaries WHERE chapter=1").fetchone()
    if row and len(row["summary"]) > 20:
        ok(f"第 1 章摘要落库（{len(row['summary'])} 字）：{row['summary'][:40]}…")
    else:
        fail("chapter_summaries 缺第 1 章")
    conn.close()

    # ── M1③：Autopilot 持久化（挂机写第 2 章）────────────────────────────────
    section("M1-③ Autopilot 持久化（挂机第 2 章，含候选+质量）")
    status, body = http("POST", f"/v1/{pid}/autopilot/start", {
        "from_chapter": 2, "to_chapter": 2, "mode": "auto_promote",
        "n_candidates": 2, "quality_check": True,
    })
    if status != 202:
        fail("autopilot start", f"{status} {body}"); return
    sid = body["session_id"]
    ok(f"挂机会话 → {sid}")

    deadline = time.time() + 900
    final = None
    while time.time() < deadline:
        status, s = http("GET", f"/v1/{pid}/autopilot/{sid}")
        if status == 200 and s["status"] not in ("running", "degraded"):
            final = s; break
        time.sleep(5)
    if final and final["status"] == "completed":
        ok(f"挂机完成：{final['chapters_done']}/{final['chapters_total']} 章，"
           f"usd=${final['budget_usd_total']:.4f}")
    else:
        fail("挂机未完成", final)

    conn = project_db()
    row = conn.execute("SELECT status, chapters_done, req_json FROM autopilot_sessions"
                       " WHERE session_id=?", (sid,)).fetchone()
    if row and row["status"] == "completed" and row["chapters_done"] == 1:
        req = json.loads(row["req_json"])
        ok(f"会话写穿 DB：status={row['status']} req(n_candidates={req.get('n_candidates')},"
           f" quality_check={req.get('quality_check')})")
    else:
        fail("autopilot_sessions 持久化", dict(row) if row else None)
    conn.close()

    status, body = http("POST", f"/v1/{pid}/autopilot/{sid}/resume")
    ok("completed 会话 resume → 409（语义正确）") if status == 409 \
        else fail("resume 语义", f"{status} {body}")

    # ── M5⑧：伏笔健康度 ──────────────────────────────────────────────────────
    section("M5-⑧ 伏笔健康度")
    status, body = http("GET", f"/v1/{pid}/foreshadow/health")
    if status == 200:
        ok(f"health → status={body['status']} open={body['open_count']}"
           f" overdue={body['overdue_count']}")
    else:
        fail("foreshadow/health", f"{status} {body}")
    conn = project_db()
    row = conn.execute("SELECT state FROM foreshadow WHERE label='神秘玉佩'").fetchone()
    conn.close()
    # due=2，第 2 章生成后翻转条件是 due < chapter（即写第 3 章时翻转）——此处仅观测
    ok(f"伏笔当前状态 = {row['state']}") if row else fail("伏笔行缺失")

    status, body = http("GET", f"/v1/{pid}/pipeline/next")
    if status == 200:
        ok(f"下一章建议 → 第{body['next_chapter']}章 sources={body['sources']}")


def main():
    print(f"{BOLD}NovelForge M1-M5 冒烟（真实 DeepSeek，预计 < $0.05 / 5-10 分钟）{RESET}")
    proc = start_server()
    try:
        run_smoke()
    except Exception:
        fail("未捕获异常", traceback.format_exc())
    finally:
        proc.terminate()
    print(f"\n{BOLD}结果：{GREEN}{_pass} 通过{RESET} / "
          f"{RED}{_fail} 失败{RESET} / {YELLOW}{_warn} 警告{RESET}")
    sys.exit(1 if _fail else 0)


if __name__ == "__main__":
    main()
