"""业务路由汇总：固定前缀的专项路由先注册，通用资源路由最后兜底。"""
from __future__ import annotations

from app.routers import resource, snapshot_recall

# resource 含 /api/{module} 和 /api/{module}/{entry_id} 动态段，会吞掉
# /api/snapshot-recall/catalog 这类两段路径，所以专项路由必须排在它前面。
# 两者都必须晚于 main.py 里的 /api/health、/api/overview 固定路由。
ROUTERS = [snapshot_recall, resource]
