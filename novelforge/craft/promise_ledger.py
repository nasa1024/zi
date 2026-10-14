"""F1 读者承诺账本（v22）：伏笔从"埋与收"升级为"埋—提醒—兑现"。

依据（docs/NovelForge/23 P0-1）：期待感 = 埋扣子 + 提醒 + 兑现（猫腻"大高潮前要挖好扣子"）；
Sanderson："读者弃书主因是进展路标不足"；MICE 线程后开先关；NstAgent 的待兑现义务。

扩展点（见 craft/extensions.py）：
- ``craft_checks``：休眠承诺、短线超时、开篇一长一短、嵌套关闭顺序、同章兑现过多、
  大爽点前置扣子数（全部确定性，默认 warn）。
- ``draft_brief``：强制注入最久休眠的承诺（≤ cfg.promises.inject_dormant_limit 条）。
- ``settle``：结算后确定性校验（嵌套关闭顺序、兑现计数等）并写报告。
"""
from __future__ import annotations
