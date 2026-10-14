"""F2 "天降外挂"拦截器（v22）：能力必须先被读者理解，代价必须付出。

依据（docs/NovelForge/23 P0-2）：乌贼"不能随意加入新东西或新外挂来解决问题"；阅文金手指五原则；
Sanderson 三定律（读者理解程度决定能用它解决多少冲突 / Limitations > Powers / 先扩展再新增）；
Brooks 第二情节点后"no newcomers"。

扩展点（见 craft/extensions.py）：
- ``craft_checks``：解决冲突的实体/金手指/物品必须早于本章登场且展示过；金手指代价须付出；
  卷进度 > 75% 后首次出现的新实体告警；卷内新增能力 / 旧能力新用法比值提示。
"""
from __future__ import annotations
