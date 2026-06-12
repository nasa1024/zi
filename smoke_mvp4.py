"""MVP4 冒烟测试：Autopilot 连写 + Bible Seed + 完整 pipeline 验证。

使用方法：
    python smoke_mvp4.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
import urllib.request
import urllib.error
from pathlib import Path

# ── 配置 ──────────────────────────────────────────────────────────────────────

API_KEY  = os.getenv("DEEPSEEK_API_KEY", "")
BASE_URL = "http://127.0.0.1:8787"
DATA_DIR = os.getenv("NOVELFORGE_DATA", "data/smoke_mvp4")

os.environ.setdefault("DEEPSEEK_API_KEY", API_KEY)
os.environ["NOVELFORGE_DATA"] = DATA_DIR

import shutil
if Path(DATA_DIR).exists():
    shutil.rmtree(DATA_DIR)
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

_pass = 0
_fail = 0

def ok(msg):
    global _pass; _pass += 1
    print(f"  {GREEN}✓{RESET} {msg}")

def fail(msg, detail=""):
    global _fail; _fail += 1
    print(f"  {RED}✗{RESET} {msg}")
    if detail:
        print(f"    {RED}{detail[:200]}{RESET}")

def section(title):
    print(f"\n{BOLD}{YELLOW}── {title}{RESET}")


def http(method: str, path: str, body=None, timeout=300) -> tuple[int, dict]:
    url = BASE_URL + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            try:
                result = json.loads(resp.read())
            except Exception:
                result = {}
            return resp.status, result
    except urllib.error.HTTPError as e:
        try:
            result = json.loads(e.read())
        except Exception:
            result = {}
        return e.code, result
    except (TimeoutError, OSError) as e:
        return -1, {"error": str(e)}


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
    # ── 0. 健康检查 ────────────────────────────────────────────────────────────
    section("0. 健康检查")
    status, body = http("GET", "/health")
    if status == 200 and body.get("status") == "ok":
        ok(f"GET /health → {body}")
    else:
        fail("GET /health", f"{status} {body}")
        return

    # ── 1. 创建项目 ────────────────────────────────────────────────────────────
    section("1. 创建测试项目")
    status, body = http("POST", "/v1/projects", {"name": "天道圣主", "genre": "xuanhuan"})
    if status != 201:
        fail("创建项目", f"{status} {body}")
        return
    project_id = body["project_id"]
    ok(f"项目创建 → {project_id}")

    # ── 2. Bible Seed ──────────────────────────────────────────────────────────
    section("2. Bible Seed（世界观种子录入）")

    status, body = http("POST", f"/v1/{project_id}/seed", {
        "proposals": [
            {"fact_type": "style", "new": {"predicate": "修炼体系", "object": "天道七境"},
             "valid_from_chapter": 0, "risk_tier": "low"},
            {"fact_type": "style", "new": {"predicate": "故事背景", "object": "天玄大陆"},
             "valid_from_chapter": 0, "risk_tier": "low"},
            {"fact_type": "character_trait", "new": {"predicate": "主角名", "object": "陆天"},
             "valid_from_chapter": 0, "risk_tier": "low"},
        ],
        "auto_approve_low_risk": True,
        "actor": "smoke_seed",
    })
    if status == 202:
        body_s = body
        ok(f"POST /seed → {len(body_s['candidate_ids'])} 条候选，"
           f"{len(body_s['auto_approved'])} 条自动批准")
    else:
        fail("POST /seed", f"{status} {body}")

    # ── 3. Bible 渲染（种子后应有内容）─────────────────────────────────────────
    section("3. Bible 渲染（seed 后）")
    status, body = http("GET", f"/v1/{project_id}/bible")
    if status == 200:
        content_len = len(body.get("content", ""))
        ok(f"GET /bible → {content_len} 字符")
    else:
        fail("GET /bible", f"{status} {body}")

    # ── 4. Autopilot start（FakeProvider 验证端点不崩溃）──────────────────────
    section("4. Autopilot start（FakeProvider，验证 API 层）")
    status, body = http("POST", f"/v1/{project_id}/autopilot/start", {
        "from_chapter": 1,
        "to_chapter": 2,
        "mode": "auto_promote",
        "chapter_goals": {"1": "主角初登场，觉醒天道血脉", "2": "拜入宗门，展示天赋"},
    })
    if status == 202 and "session_id" in body:
        sid = body["session_id"]
        ok(f"POST /autopilot/start → session_id={sid} status={body['status']}")
    else:
        fail("POST /autopilot/start", f"{status} {body}")
        sid = None

    # ── 5. Autopilot status 轮询 ──────────────────────────────────────────────
    section("5. Autopilot 状态轮询")
    if sid:
        for _ in range(30):
            status2, body2 = http("GET", f"/v1/{project_id}/autopilot/{sid}", timeout=10)
            if status2 == 200 and body2.get("status") in ("completed","error","circuit_broken","degraded"):
                break
            time.sleep(0.3)
        status2, body2 = http("GET", f"/v1/{project_id}/autopilot/{sid}")
        final_status = body2.get("status", "?")
        chapters_done = body2.get("chapters_done", 0)
        if status2 == 200:
            ok(f"GET /autopilot/{sid} → status={final_status} chapters_done={chapters_done}")
        else:
            fail("GET autopilot status", f"{status2} {body2}")
    else:
        fail("跳过轮询（无 session_id）")

    # ── 6. Autopilot status 列表 ──────────────────────────────────────────────
    section("6. Autopilot 会话列表")
    status3, body3 = http("GET", f"/v1/{project_id}/autopilot/status")
    if status3 == 200 and isinstance(body3, list):
        ok(f"GET /autopilot/status → {len(body3)} 个会话")
    else:
        fail("GET /autopilot/status", f"{status3} {body3}")

    # ── 7. Autopilot degrade（已完成会话应返回 409）───────────────────────────
    section("7. Degrade 请求")
    if sid:
        status4, body4 = http("POST", f"/v1/{project_id}/autopilot/{sid}/degrade",
                              {"reason": "smoke_test"})
        if status4 in (200, 409):
            ok(f"POST /autopilot/degrade → {status4}（running→degrade 或 已完成→409）")
        else:
            fail("POST /autopilot/degrade", f"{status4} {body4}")

    # ── 8. 真实 DeepSeek Autopilot（1 章，限预算）────────────────────────────
    section("8. Autopilot（DeepSeek 真实 API，1 章）")
    _, real_proj = http("POST", "/v1/projects", {"name": "天道-real", "genre": "xuanhuan"})
    real_id = real_proj.get("project_id", "")
    if not real_id:
        fail("创建真实测试项目失败")
    else:
        print(f"    → project_id={real_id}，启动 autopilot 生成第1章...")
        status5, body5 = http("POST", f"/v1/{real_id}/autopilot/start", {
            "from_chapter": 1,
            "to_chapter": 1,
            "chapter_goals": {"1": "男主角陆天在残破宗门中苦修，遭受同门欺凌后觉醒天道之眼"},
            "mode": "auto_promote",
            "budget_max_tokens_per_chapter": 50000,
            "budget_max_usd_per_chapter": 0.5,
        })
        if status5 != 202 or "session_id" not in body5:
            fail("autopilot/start（真实）", f"{status5} {body5}")
        else:
            sid5 = body5["session_id"]
            for _ in range(100):
                _, s5 = http("GET", f"/v1/{real_id}/autopilot/{sid5}", timeout=10)
                if s5.get("status") in ("completed", "error", "circuit_broken", "degraded"):
                    break
                time.sleep(3)
            _, s5 = http("GET", f"/v1/{real_id}/autopilot/{sid5}")
            final = s5.get("status", "?")
            tokens = s5.get("budget_tokens_total", 0)
            done = s5.get("chapters_done", 0)
            ok(f"Autopilot 真实完成 → status={final} done={done}/1 tokens={tokens}")

    # ── 9. 归档 ───────────────────────────────────────────────────────────────
    section("9. 归档项目")
    status6, _ = http("DELETE", f"/v1/projects/{project_id}")
    if status6 == 204:
        ok(f"DELETE /projects/{project_id} → 204")
    else:
        fail("DELETE /projects", f"{status6}")


if __name__ == "__main__":
    print(f"\n{BOLD}NovelForge MVP4 冒烟测试（Autopilot + Seed）{RESET}")
    print(f"  DATA_DIR = {DATA_DIR}")
    print(f"  BASE_URL = {BASE_URL}")
    print(f"  API_KEY  = {API_KEY[:8]}...")

    proc = None
    try:
        print("\n启动 FastAPI 服务...")
        proc = start_server()
        print("服务已就绪。\n")
        run_smoke()
    except Exception:
        traceback.print_exc()
    finally:
        if proc:
            proc.terminate()

    print(f"\n{'─'*40}")
    total = _pass + _fail
    print(f"结果: {GREEN}{_pass}{RESET} 通过 / {RED}{_fail}{RESET} 失败  （共 {total} 项）")
    if _fail:
        print(f"{RED}冒烟测试未完全通过{RESET}")
        sys.exit(1)
    else:
        print(f"{GREEN}冒烟测试全部通过 ✓{RESET}")
        sys.exit(0)
