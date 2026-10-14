"""F3 极性序列与抑扬上限（v22）：每章必须"转"，同向不超过三。

依据（docs/NovelForge/23 P0-3）：千幻冰云"最惨不能惨过三章，最爽也不能爽过三章"；
Story Grid 每场必须转、避免同向极性链；猫腻"小高潮、平复期和余韵。高潮越小，平复期越短"。

扩展点（见 craft/extensions.py）：
- ``craft_checks``：本章契约转向与近几章实际转向组成同向链超过上限 → 告警；aftermath 安排检查。
- ``draft_brief``：连续低谷后要求本章安排转机；大高潮后建议余韵。
- ``enrich_actuals``：回填本章实际转向/转折类型。
- ``settle``：把实际转向写入 pacing_state（逐章幂等 upsert）并维护 pacing_cursor 连续计数。
"""
from __future__ import annotations
