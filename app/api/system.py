"""
系统信息接口
- GET /api/system/version: 返回后端版本 + 支持的管线 op 清单

用途：部署后用这个接口确认代码是否更新成功，避免"配置对了但报未知 op"的困惑。
"""
from fastapi import APIRouter

from app.core.errors import R_ok

router = APIRouter(prefix="/api/system", tags=["系统"])

# 与代码实际能力保持同步；新增 transform op 时记得加进来
PIPELINE_TRANSFORM_OPS = ["join", "aggregate", "compute", "combine", "mergerows", "unpivot", "map", "filter", "pick"]
BACKEND_VERSION = "2.9.0"


@router.get("/version")
async def version():
    """返回后端版本和管线能力清单，用于部署自检。"""
    return R_ok(data={
        "version": BACKEND_VERSION,
        "pipeline_transform_ops": PIPELINE_TRANSFORM_OPS,
        "features": {
            "sql_template": True,          # $if / $for / #{}
            "like_modifier": True,         # :name|like
            "pipeline": True,              # 多步骤管线
            "pipeline_compute": True,      # transform op=compute 标量算术
            "pipeline_aggregate": True,    # transform op=aggregate 分组聚合
        },
    })
