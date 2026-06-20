"""NovelForge memory 包（Recall / PipelineManager / L2 聚合 / L3 事件 / 向量召回）。"""

from .embedding import (
    Embedder,
    HashEmbedder,
    OpenAIEmbedder,
    build_embedder,
    cosine,
    get_default_embedder,
)
from .l2_aggregate import (
    aggregate_chapter_scenes,
    pending_l2_chapters,
    run_l2_window,
    should_run_l2,
    split_scenes,
)
from .l3_events import CANON_TYPES, extract_canon_events, scan_canon_signals
from .recall import RecallPack, gather_hard_context, rrf_fuse, search_similar_scenes
from .vector_store import SceneVectorStore

__all__ = [
    "Embedder", "HashEmbedder", "OpenAIEmbedder", "build_embedder",
    "cosine", "get_default_embedder",
    "aggregate_chapter_scenes", "pending_l2_chapters", "run_l2_window",
    "should_run_l2", "split_scenes",
    "CANON_TYPES", "extract_canon_events", "scan_canon_signals",
    "RecallPack", "gather_hard_context", "rrf_fuse", "search_similar_scenes",
    "SceneVectorStore",
]
