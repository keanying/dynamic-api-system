"""
页面视图路由 - 渲染 Jinja2 模板
"""

from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.core.config import settings, BASE_DIR
from app.core.database import get_db
from app.core.logging import get_logger
from app.models.models import Project

log = get_logger("views")

templates = Jinja2Templates(directory=str(BASE_DIR / "app" / "templates"))

router = APIRouter(tags=["页面视图"])


def _compute_static_ver() -> str:
    """静态资源版本号：取 style.css / app.js 的最大修改时间，
    文件一变版本号就变，浏览器自动拉最新（避免 CSS/JS 缓存导致看到旧界面）。"""
    import os
    paths = [
        BASE_DIR / "app" / "static" / "css" / "style.css",
        BASE_DIR / "app" / "static" / "js" / "app.js",
        BASE_DIR / "app" / "static" / "css" / "code_editor.css",
        BASE_DIR / "app" / "static" / "js" / "code_editor.js",
    ]
    latest = 0
    for p in paths:
        try:
            latest = max(latest, int(os.path.getmtime(p)))
        except Exception:
            pass
    return str(latest or 1)


# 进程启动时计算一次（生产环境静态文件不变；开发热重载会重启进程，自然刷新）
STATIC_VER = _compute_static_ver()

def _approval_rule() -> str:
    from app.api.approvals import online_rule
    return online_rule()


# 公共模板上下文
def _ctx(extra: dict = None) -> dict:
    """构建公共模板上下文"""
    from app.api.releases import env_info
    ctx = {
        "gateway_prefix": settings.gateway.prefix.rstrip("/"),
        "static_ver": STATIC_VER,
        "app_env": env_info(),   # 当前环境（预发/生产），布局横幅与跨环境跳转用
        "approval_rule": _approval_rule(),   # 上线审批通过条件：any / both
    }
    if extra:
        ctx.update(extra)
    return ctx


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    log.debug("渲染登录页面")
    return templates.TemplateResponse(request=request, name="pages/login.html", context=_ctx())


@router.get("/sso", response_class=HTMLResponse)
async def sso_exchange_page(request: Request):
    """单点登录落地页：用票据换取本环境登录凭证后跳到 next。"""
    return templates.TemplateResponse(request=request, name="pages/sso.html", context=_ctx({"sso_mode": "exchange"}))


@router.get("/sso/issue", response_class=HTMLResponse)
async def sso_issue_page(request: Request):
    """另一环境的登录页跳来：本环境已登录则签发票据跳回去，未登录则回对方登录页。"""
    return templates.TemplateResponse(request=request, name="pages/sso.html", context=_ctx({"sso_mode": "issue"}))


@router.get("/sso/logout", response_class=HTMLResponse)
async def sso_logout_page(request: Request):
    """另一环境退出登录时顺带退出本环境，再回对方登录页。"""
    return templates.TemplateResponse(request=request, name="pages/sso.html", context=_ctx({"sso_mode": "logout"}))


@router.get("/admin/dashboard", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    log.debug("渲染仪表盘页面")
    return templates.TemplateResponse(request=request, name="pages/dashboard.html", context=_ctx({
        "active_page": "dashboard",
        "username": "Admin",
    }))


@router.get("/admin/approvals", response_class=HTMLResponse)
async def approval_center_page(request: Request):
    log.debug("渲染审批中心页面")
    return templates.TemplateResponse(request=request, name="pages/approval_center.html", context=_ctx({
        "active_page": "approvals",
    }))


@router.get("/admin/releases")
async def releases_page(request: Request):
    """发布审核已并入审批中心 (v2.23)：旧地址跳到审批中心的「发布到生产」页签。"""
    rid = request.query_params.get("id", "")
    return RedirectResponse(url="/admin/approvals?tab=release" + (f"&id={int(rid)}" if rid.isdigit() else ""), status_code=302)


@router.get("/admin/projects", response_class=HTMLResponse)
async def projects_page(request: Request):
    log.debug("渲染项目管理页面")
    return templates.TemplateResponse(request=request, name="pages/projects.html", context=_ctx({
        "active_page": "projects",
        "username": "Admin",
    }))


@router.get("/admin/projects/{project_id}", response_class=HTMLResponse)
async def project_detail_page(request: Request, project_id: int, db: AsyncSession = Depends(get_db, scope="function")):
    log.debug(f"渲染项目详情页面 | project_id={project_id}")
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    project_code = project.code if project else ""
    return templates.TemplateResponse(request=request, name="pages/project_detail.html", context=_ctx({
        "active_page": "projects",
        "username": "Admin",
        "project_id": project_id,
        "project_code": project_code,
    }))


@router.get("/admin/projects/{project_id}/approvals", response_class=HTMLResponse)
async def project_approvals_page(request: Request, project_id: int, db: AsyncSession = Depends(get_db, scope="function")):
    log.debug(f"渲染审批中心页面 | project_id={project_id}")
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    return templates.TemplateResponse(request=request, name="pages/approvals.html", context=_ctx({
        "active_page": "projects",
        "project_id": project_id,
        "project_name": project.name if project else "",
    }))


@router.get("/admin/projects/{project_id}/members", response_class=HTMLResponse)
async def project_members_page(request: Request, project_id: int, db: AsyncSession = Depends(get_db, scope="function")):
    log.debug(f"渲染项目成员页面 | project_id={project_id}")
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    return templates.TemplateResponse(request=request, name="pages/project_members.html", context=_ctx({
        "active_page": "projects",
        "project_id": project_id,
        "project_name": project.name if project else "",
    }))


@router.get("/admin/projects/{project_id}/apis/new", response_class=HTMLResponse)
async def api_create_page(request: Request, project_id: int, db: AsyncSession = Depends(get_db, scope="function")):
    log.debug(f"渲染 API 创建页面 | project_id={project_id}")
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    project_code = project.code if project else ""
    log.debug(f"项目编码 | project_id={project_id} | project_code={project_code}")
    return templates.TemplateResponse(request=request, name="pages/api_editor.html", context=_ctx({
        "active_page": "projects",
        "username": "Admin",
        "project_id": project_id,
        "project_code": project_code,
        "api_id": None,
    }))


@router.get("/admin/projects/{project_id}/apis/{api_id}", response_class=HTMLResponse)
async def api_edit_page(request: Request, project_id: int, api_id: int, db: AsyncSession = Depends(get_db, scope="function")):
    log.debug(f"渲染 API 编辑页面 | project_id={project_id} | api_id={api_id}")
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    project_code = project.code if project else ""
    log.debug(f"项目编码 | project_id={project_id} | project_code={project_code}")
    return templates.TemplateResponse(request=request, name="pages/api_editor.html", context=_ctx({
        "active_page": "projects",
        "username": "Admin",
        "project_id": project_id,
        "project_code": project_code,
        "api_id": api_id,
    }))


@router.get("/admin/datasources", response_class=HTMLResponse)
async def datasources_page(request: Request):
    log.debug("渲染数据源管理页面")
    return templates.TemplateResponse(request=request, name="pages/datasources.html", context=_ctx({
        "active_page": "datasources",
        "username": "Admin",
    }))


@router.get("/admin/users", response_class=HTMLResponse)
async def users_page(request: Request):
    log.debug("渲染用户管理页面")
    return templates.TemplateResponse(request=request, name="pages/users.html", context=_ctx({
        "active_page": "users",
        "username": "Admin",
    }))


@router.get("/admin/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    log.debug("渲染调用日志页面")
    return templates.TemplateResponse(request=request, name="pages/logs.html", context=_ctx({
        "active_page": "logs",
        "username": "Admin",
    }))


@router.get("/admin/plugin-libraries", response_class=HTMLResponse)
async def plugin_libraries_page(request: Request):
    log.debug("渲染插件库页面")
    return templates.TemplateResponse(request=request, name="pages/plugin_libraries.html", context=_ctx({
        "active_page": "plugin_libraries",
        "username": "Admin",
    }))


@router.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """根路径重定向到仪表盘"""
    log.debug("根路径重定向到仪表盘")
    return RedirectResponse(url="/admin/dashboard")
