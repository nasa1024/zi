"""存量正文冷启动（POST-v15 P1#4）：批量反向抽取 + 去噪 + 分层晋升。"""
from .batch import (  # noqa: F401
    ColdStartJobManager,
    dedup_proposals,
    get_cold_start_manager,
    split_manuscript,
    tier_route_proposals,
)
