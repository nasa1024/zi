# P2#13/#14 设计：全书 Objective + 卷级 KR 结算 / 弱模型失败升级

日期：2026-06-13
来源：`docs/research_inkos_oh-story_20260612.md` P2 #13（inkos §3.1.8）+ #14（oh-story §3.2.7）
授权：延续 P2 批次的"不用确认、选最合适方式"授权。

## 0. 两项为何一起做

#14 是**使能器**，#13 是它的**第一个真实消费者**：

- #14：gateway 缺"机械校验失败→升一档重试"这一步。FAST 档跑提取/结算类任务，JSON 解析失败时现状是直接降级（foreshadow_settle 返回 None、KR 无从谈起）。补一个 `generate_validated`：在起始档跑，`parse(text)` 失败则升一档（fast→mid→strong）重试。
- #13：卷级 KR 兑现判定就是典型的"FAST 跑、解析失败该升级"场景——一次 LLM 调用判每条 KR met/partial/missed，JSON 畸形时升 MID 重试。

## 1. #14 弱模型分层 + 失败升级

### 1.1 `LLMGateway.generate_validated`

```python
@dataclass
class ValidatedResult:
    value: object | None      # parse 成功的产物；全档失败为 None
    response: Response        # 最后一次调用的原始响应（成本已计入 ledger）
    tier_used: str            # 实际产出档位
    escalated: bool           # 是否发生过升级

def generate_validated(self, tier, messages, *, parse,
                       system=None, max_tokens=4096, temperature=1.0,
                       cache_hint=None, max_tier=ModelTier.STRONG) -> ValidatedResult:
```

- 档序 `[FAST, MID, STRONG]`，从 `tier` 到 `max_tier` 逐档尝试；
- 每档 `self.generate(...)` 后跑 `parse(resp.text)`，返回非 None 即成功（`parse` 抛异常视同 None）；
- 全档失败返回 `value=None` + 最后响应；
- 每次调用都正常计入 ledger（升级即多花一次，成本看板可见）；
- CircuitTripped 不拦截——与现有 `generate` 一致向上抛。

不在 gateway 加配置：是否允许升级由调用方的 `max_tier` 决定（想关掉就 `max_tier=tier`）。

### 1.2 接入 foreshadow_settle

`_call_settler` 现状：手动 `generate` + 正则取 JSON + 解析失败返回 None。改为：

```python
result = gateway.generate_validated(
    mt, [Message(...)], parse=_parse_settle_json,
    system=_SETTLE_SYSTEM, max_tokens=2048, max_tier=ModelTier.MID)
return result.value   # None 时上层照旧 raise（行为不变，只是多了一次 MID 抢救）
```

FAST 默认、升级上限 MID（结算不值得烧 STRONG）。`_parse_settle_json(text)` 抽出 `_call_settler` 现有的正则+json.loads 逻辑。行为变化：之前 FAST 解析失败直接 None，现在多一次 MID 重试才 None——纯增益，无回退风险。

## 2. #13 全书 Objective + 卷级 KR

### 2.1 Schema（migration v14）

```sql
ALTER TABLE volumes ADD COLUMN objective   TEXT;   -- 本卷可验证目标（一句话）
ALTER TABLE volumes ADD COLUMN key_results TEXT;   -- JSON: [{id,text,status,evidence}]
```

schema.sql 基线同步加列。volumes 表历史上**有迁移创建过**（v5），不像 chapter_cards/foreshadow 那样缺表——v14 直接 ALTER 即可，但仍按既定双路径写（缺表兜底整建，防御未来）。

KR 单元：`{"id": "kr1", "text": "主角揭穿幕后黑手", "status": "pending", "evidence": ""}`，status ∈ `pending|met|partial|missed`。

### 2.2 API

- `VolumeCreateRequest` / `VolumeUpdateRequest` 增 `objective: Optional[str]`、`key_results: Optional[list[str|dict]]`；
  - 入库归一：纯字符串列表 → 自动补 `{id: kr{N}, text, status: pending, evidence: ""}`；已是 dict 的保留 status/evidence。
- `VolumeResponse` 增 `objective`、`key_results: list[dict]`（解析 JSON，畸形→[]）。
- `POST /v1/{project}/volumes/{volume_no}/settle-kr` → 手动触发结算，返回报告。
- PATCH status→completed 时**自动**跑一次结算（best-effort，失败不阻断状态更新）。

### 2.3 结算（`craft/volume_kr.py`）

```python
def settle_volume_kr(gateway, conn, volume_no, *, tier="fast") -> dict:
```

1. 读 volumes 的 objective + key_results + rolling_summary；无 objective 或无 KR → 返回 `{settled: False, reason}`；
2. 拼 chapter_summaries（本卷各章）作为证据材料；
3. 一次 `gateway.generate_validated(FAST, ..., parse=_parse_kr_verdict, max_tier=MID)`：judge 每条 KR 对照 objective 与卷情节，输出 `{kr_id: {status, evidence}}`；
4. **确定性兜底**（防 LLM 虚报，同 foreshadow 防假回收精神）：LLM 判 met 但 evidence 为空 → 降为 partial；status 非法 → 保持 pending；
5. 写回 volumes.key_results（更新每条 status/evidence），返回 `{settled, objective, met, partial, missed, results:[...]}`。

不进 generate_chapter 热路径——只在显式触发（手动端点 / status→completed）时跑，原型期不给挂机连载加每卷一次的隐性 LLM 成本（除非用户主动 complete 卷）。

### 2.4 前端

原型期**不做** KR 编辑/展示 UI——纯后端 + API。卷管理 UI 已有，KR 是 JSON 字段，留以后接。（与 P1 最小集同理，directional 原型先打通后端。）

## 3. 不做什么（YAGNI）

- 不做 KR 的确定性绑定（"KR 关联某伏笔 paid_off"）——KR 是 OKR 式自由文本，一般无法确定性判定，LLM judge 是 inkos 既定做法；只加 met-需-evidence 的兜底。
- 不做全书级 Objective 的跨卷结算——先卷级 KR 跑通。
- 不把 KR 结算塞进 generate_chapter 热路径。
- 不动三条既定 non-goal。

## 4. 测试

`tests/test_p2_tier_upgrade.py`：
- generate_validated：起档即过不升级；起档失败升 MID 成功（FakeProvider 按 model 返不同响应）；全档失败 value=None；escalated 标志；ledger 计入每次调用；max_tier=tier 时不升级。
- foreshadow_settle：FAST 返畸形 JSON、MID 返合法 → 结算成功且 tier 升级（用 model-based factory）。

`tests/test_p2_volume_kr.py`：
- KR 归一：字符串列表入库补全 id/status；dict 保留。
- settle_volume_kr：met/partial/missed 写回；met 无 evidence → 降 partial；无 objective→未结算；JSON 畸形→升级路径。
- API：create 带 objective+KR、response 透出；settle-kr 端点；PATCH completed 自动结算（用 FakeProvider）。

`tests/test_migrations.py`：v14 双路径（ALTER 既有 volumes + 新库基线）。

## 5. 风险

- generate_validated 升级会多花钱——但只在解析失败时，且封顶 MID（settle 场景）；正常路径零额外成本。
- KR 结算依赖卷有 rolling_summary/chapter_summaries——空卷结算会让 LLM 巧妇难为，兜底为全 pending 不强判。
