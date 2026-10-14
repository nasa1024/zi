"""F5 爽点三段式（v22）：动作 → 在场者反应 → 收获重要性展示。

依据（docs/NovelForge/23 P0-5）：猫腻"'看对方怎么输'才是快感的来源"；阅文"剧情 = 动作 + 反应"；
起点编辑子良"目标—行动—收获—收获重要性展示"。

扩展点（见 craft/extensions.py）：
- ``craft_checks``：爽点章（或下一章）至少 1 个非主角态度/关系变化的证据。
- ``enrich_actuals``：从正文抽取 witnesses 的反应证据（ReactionEvidence）。
- ``draft_brief``：用 knowledge_edges 推荐"信息差打脸"机会给规划/起草。
"""
from __future__ import annotations
