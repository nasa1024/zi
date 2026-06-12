"""MVP3 冒烟测试：FastAPI REST 端点 + revert + pipeline/run（含真实 DeepSeek API）。

使用方法：
    python smoke_mvp3.py
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
DATA_DIR = os.getenv("NOVELFORGE_DATA", "data/smoke_mvp3")

os.environ.setdefault("DEEPSEEK_API_KEY", API_KEY)
os.environ["NOVELFORGE_DATA"] = DATA_DIR

import shutil
if Path(DATA_DIR).exists():
    shutil.rmtree(DATA_DIR)
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

# ── 颜色输出 ──────────────────────────────────────────────────────────────────

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


# ── HTTP 工具 ─────────────────────────────────────────────────────────────────

def http(method: str, path: str, body=None, timeout=120) -> tuple[int, dict]:
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


# ── 启动 FastAPI 服务 ──────────────────────────────────────────────────────────

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


# ── 冒烟测试主体 ───────────────────────────────────────────────────────────────

def run_smoke():
    # ── 0. 健康检查 ────────────────────────────────────────────────────────────
    section("0. 健康检查")
    status, body = http("GET", "/health")
    if status == 200 and body.get("status") == "ok":
        ok(f"GET /health → {body}")
    else:
        fail("GET /health", f"status={status} body={body}")
        return

    # ── 1. 项目管理 ────────────────────────────────────────────────────────────
    section("1. 项目管理 CRUD")

    status, body = http("POST", "/v1/projects", {"name": "破天之路", "genre": "xuanhuan"})
    if status == 201 and "project_id" in body:
        ok(f"POST /v1/projects 201 → project_id={body['project_id']}")
        project_id = body["project_id"]
    else:
        fail("POST /v1/projects", f"status={status} body={body}")
        return

    status, body = http("GET", "/v1/projects")
    if status == 200 and any(p["project_id"] == project_id for p in body):
        ok(f"GET /v1/projects → {len(body)} 个项目")
    else:
        fail("GET /v1/projects", f"status={status} body={body}")

    status, body = http("GET", f"/v1/projects/{project_id}")
    if status == 200 and body["project_id"] == project_id:
        ok(f"GET /v1/projects/{{id}} → name={body['name']}")
    else:
        fail("GET /v1/projects/{id}", f"status={status} body={body}")

    # ── 2. 捕获候选 ────────────────────────────────────────────────────────────
    section("2. 捕获候选（capture）")

    status, body = http("POST", f"/v1/{project_id}/capture", {
        "source_chapter": 1,
        "source_kind": "manual",
        "proposals": [
            {"op": "add", "fact_type": "style",
             "new": {"predicate": "修炼体系", "object": "天道五境"},
             "valid_from_chapter": 1},
            {"op": "add", "fact_type": "character_trait",
             "new": {"predicate": "性格", "object": "沉稳"},
             "valid_from_chapter": 1},
        ],
    })
    if status == 202 and len(body.get("candidate_ids", [])) == 2:
        ok(f"POST capture 202 → {len(body['candidate_ids'])} 个候选")
    else:
        fail("POST capture", f"status={status} body={body}")

    # ── 3. 审阅队列 ────────────────────────────────────────────────────────────
    section("3. 审阅队列（reviews）")

    status, body = http("GET", f"/v1/{project_id}/reviews")
    if status == 200:
        ok(f"GET /reviews → {len(body)} 条")
    else:
        fail("GET /reviews", f"status={status} body={body}")

    # ── 4. Bible 渲染 ──────────────────────────────────────────────────────────
    section("4. Bible 渲染")

    status, body = http("GET", f"/v1/{project_id}/bible")
    if status == 200 and body.get("is_readonly") is True:
        ok(f"GET /bible markdown → {len(body.get('content',''))} 字符")
    else:
        fail("GET /bible markdown", f"status={status} body={body}")

    status, body = http("GET", f"/v1/{project_id}/bible?format=json")
    if status == 200:
        try:
            parsed = json.loads(body["content"])
            assert "entities" in parsed
            ok(f"GET /bible json → entities={len(parsed.get('entities', {}))}")
        except Exception as e:
            fail("GET /bible json 解析", str(e))
    else:
        fail("GET /bible json", f"status={status} body={body}")

    # ── 5. Pipeline run（DeepSeek 真实 API）───────────────────────────────────
    section("5. Pipeline run（DeepSeek 真实 API，限预算）")

    _, real_proj = http("POST", "/v1/projects", {"name": "灵武神界", "genre": "xuanhuan"})
    real_id = real_proj.get("project_id", "")
    if not real_id:
        fail("创建测试项目失败", str(real_proj))
    else:
        print(f"    → project_id={real_id}，生成第 1 章（最多等 300 秒）...")
        status, body = http("POST", f"/v1/{real_id}/pipeline/run", {
            "chapter_no": 1,
            "chapter_goal": "男主角凌云在废弃矿洞中觉醒龙魂血脉，击败欺凌者，踏上修仙之路",
            "budget_max_tokens": 50000,
            "budget_max_usd": 0.5,
        }, timeout=300)
        if status == 200 and body.get("run_id"):
            draft_len = len(body.get("draft_text", ""))
            tokens = body.get("budget_spent", {}).get("tokens", 0)
            ok(f"pipeline/run 章节1 → ok={body.get('ok')} draft={draft_len}字 tokens={tokens}")
            if draft_len > 200:
                ok(f"  草稿片段：{body['draft_text'][:80]}...")
            elif draft_len == 0 and body.get("error"):
                fail(f"  草稿为空，错误：{body.get('error','')[:100]}")
            else:
                fail(f"  草稿内容太短（{draft_len}字）")
        elif status == -1:
            fail("pipeline/run 超时", "300 秒内未完成")
        else:
            fail("pipeline/run（DeepSeek）", f"status={status} body={str(body)[:300]}")

    # ── 6. State query ─────────────────────────────────────────────────────────
    section("6. 状态查询（state）")

    rid = real_id or project_id
    status, body = http("POST", f"/v1/{rid}/state", {"as_of_chapter": 1})
    if status == 200 and "power_ranks" in body:
        ok(f"POST state → as_of_chapter={body['as_of_chapter']}")
    else:
        fail("POST state", f"status={status} body={body}")

    # ── 7. 归档项目 ────────────────────────────────────────────────────────────
    section("7. 归档项目")

    status, _ = http("DELETE", f"/v1/projects/{project_id}")
    if status == 204:
        ok(f"DELETE /projects/{project_id} → 204")
    else:
        fail("DELETE /projects", f"status={status}")

    status, body = http("GET", "/v1/projects")
    if status == 200 and project_id not in [p["project_id"] for p in body]:
        ok("归档后不出现在列表")
    else:
        fail("归档后仍出现在列表")


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"\n{BOLD}NovelForge MVP3 冒烟测试{RESET}")
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
