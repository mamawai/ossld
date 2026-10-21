"""网站：首页列表、提交页、LINUX DO 登录。

本地启动：.venv\\Scripts\\uvicorn app.main:app --reload
"""

import hashlib
import random
from datetime import datetime
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, projects
from .projects import BEIJING

HERE = Path(__file__).parent

projects.init_db()

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)  # 不对外暴露接口文档
# 登录状态存在签名过的 Cookie 里；SameSite=Lax 让别的网站发起的 POST 带不上它
app.add_middleware(
    SessionMiddleware,
    secret_key=auth.SESSION_SECRET,
    session_cookie="ossld_session",
    max_age=30 * 24 * 3600,
    same_site="lax",
    https_only=auth.COOKIE_SECURE,
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.mount("/covers", StaticFiles(directory=projects.COVERS_DIR), name="covers")

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["bjdate"] = lambda dt: dt.astimezone(BEIJING).strftime("%Y-%m-%d")
templates.env.filters["repo_name"] = lambda url: url.removeprefix("https://github.com/")
templates.env.filters["clean_title"] = projects.display_title
templates.env.globals["required_tag"] = projects.REQUIRED_TAG
templates.env.globals["categories"] = projects.CATEGORIES
templates.env.globals["title_max"] = projects.TITLE_MAX
templates.env.globals["max_repos"] = projects.MAX_REPOS
# 样式表地址带上内容指纹：文件一改地址就变，Cloudflare 和浏览器不会继续用旧缓存（Cloudflare 默认缓存 css 4 小时）
templates.env.globals["css_version"] = hashlib.sha256((HERE / "static" / "style.css").read_bytes()).hexdigest()[:8]
templates.env.globals["current_user"] = auth.current_user
templates.env.globals["csrf_token"] = auth.csrf_token


def _notice(e: projects.RuleError) -> dict:
    """不合格 → 提示框。红色 error 是收不了，蓝色 info 是已经收录过。"""
    return {"kind": "info" if isinstance(e, projects.AlreadyCollected) else "error", "text": str(e)}


_EXPIRED = {"kind": "retry", "text": "页面停留太久已失效，请重新提交"}


def _render_submit(request: Request, **context):
    user = auth.current_user(request)
    context["blocked"] = projects.block_reason(user) if user else None  # 被禁言、被封禁的直接显示原因，不显示表单
    context["today"] = datetime.now(BEIJING).date().isoformat()  # 发布日期最晚只能选到今天
    return templates.TemplateResponse(request, "submit.html", context)


def _render_message(request: Request, title: str, text: str, status_code: int = 200):
    return templates.TemplateResponse(request, "message.html", {"title": title, "text": text}, status_code=status_code)


@app.get("/")
def index(request: Request, added: int | None = None):
    items = projects.list_all()
    today = datetime.now(BEIJING)
    picks = projects.daily_picks(items, today.strftime("%Y-%m-%d"))
    # 公平展示：每次打开随机排序；刚收录的那个排第一，让作者一眼看到
    random.shuffle(items)
    items.sort(key=lambda p: p.topic_id != added)
    counts = {c: sum(p.category == c for p in items) for c in projects.CATEGORIES}
    return templates.TemplateResponse(request, "index.html", {
        "projects": items,
        "picks": picks,
        "today": f"{today.month} 月 {today.day} 日",
        "category_counts": [(c, n) for c, n in counts.items() if n],  # 只列有项目的分类
        "added": added,
    })


@app.get("/p/{topic_id}")
def project_page(request: Request, topic_id: int):
    project = projects.get(topic_id)
    if project is None:
        return _render_message(request, "没有这个项目", "可能还没收录，或者已经被删除了。", status_code=404)
    return templates.TemplateResponse(request, "project.html", {
        "p": project,
        "more": projects.by_author(project.author_id, exclude=topic_id),
        "related": projects.related(project),
    })


# ---------- 登录 ----------

@app.get("/auth/login")
def login(request: Request, next: str = "/"):
    if not auth.enabled():
        return _render_message(request, "登录暂时不可用", "网站还没配置 LINUX DO 登录。", status_code=503)
    return RedirectResponse(auth.login_url(request, next), status_code=302)


@app.get("/auth/callback")
def login_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error:  # 用户在 LINUX DO 那边点了拒绝
        return _render_message(request, "没有登录", "你取消了 LINUX DO 授权。")
    try:
        next_path = auth.finish_login(request, code, state)
    except auth.LoginError as e:
        return _render_message(request, "登录失败", str(e), status_code=400)
    return RedirectResponse(next_path, status_code=303)


@app.post("/auth/logout")
def logout(request: Request, csrf: Annotated[str, Form()] = ""):
    if auth.csrf_ok(request, csrf):
        auth.logout(request)
    return RedirectResponse("/", status_code=303)


# ---------- 提交 ----------

@app.get("/submit")
def submit_page(request: Request):
    return _render_submit(request)


@app.post("/submit")
def submit(
    request: Request,
    background_tasks: BackgroundTasks,
    csrf: Annotated[str, Form()] = "",
    link: Annotated[str, Form()] = "",
    title: Annotated[str, Form()] = "",
    category: Annotated[str, Form()] = "",
    repos: Annotated[list[str], Form()] = [],
    published: Annotated[str, Form()] = "",
    agree_own: Annotated[bool, Form()] = False,
    agree_promo: Annotated[bool, Form()] = False,
):
    """作者填表 → 检查 → 收录。不去 linux.do 核对帖子，靠两条提交须知约束。"""
    user = auth.current_user(request)
    if user is None or projects.block_reason(user):
        return _render_submit(request)  # 页面会显示"登录后才能提交"或者不能提交的原因
    # 出错时把填过的原样填回去，不用重填
    form = {
        "link": link, "title": title, "category": category, "repos": repos, "published": published,
        "agree_own": agree_own, "agree_promo": agree_promo,
    }
    if not auth.csrf_ok(request, csrf):
        return _render_submit(request, form=form, notice=_EXPIRED)
    try:
        if not (agree_own and agree_promo):
            raise projects.RuleError("请先勾选同意两条提交须知")
        project = projects.new_project(user, link, title, category, repos, published)
        projects.add(project)
    except projects.RuleError as e:
        return _render_submit(request, form=form, notice=_notice(e))
    # 封面图在后台慢慢下，不让用户等；下好之前首页显示仓库名
    background_tasks.add_task(projects.save_cover, project.topic_id, project.github_repos[0])
    # 提交成功跳回首页，刷新页面不会重复提交
    return RedirectResponse(f"/?added={project.topic_id}", status_code=303)


# ---------- 管理后台 ----------
# 用单独的管理员密码登录，跟 LINUX DO 登录无关。操作完跳回 /admin，结果放进会话里显示一次

def _back_to_admin(request: Request, text: str | None = None, kind: str = "ok"):
    if text:
        request.session["admin_notice"] = {"kind": kind, "text": text}
    return RedirectResponse("/admin", status_code=303)


def _admin_ok(request: Request, csrf: str) -> bool:
    """后台操作前的检查：是管理员、表单令牌对。令牌不对多半是页面放太久了。"""
    if not auth.is_admin(request):
        return False
    if not auth.csrf_ok(request, csrf):
        request.session["admin_notice"] = {"kind": "retry", "text": "页面停留太久已失效，请重新操作"}
        return False
    return True


@app.get("/admin")
def admin_page(request: Request):
    if not auth.is_admin(request):
        return templates.TemplateResponse(request, "admin.html", {"enabled": auth.admin_enabled()})
    bans = projects.banned_users()
    return templates.TemplateResponse(request, "admin.html", {
        "is_admin": True,
        "projects": projects.list_all(),
        "bans": bans,
        # 列表里标出哪些作者已经封禁了；认人规则和 projects.is_banned 一样
        "banned_ids": {b.user_id for b in bans},
        "banned_names": {b.username.lower() for b in bans},
        "notice": request.session.pop("admin_notice", None),
    })


@app.post("/admin/login")
def admin_login(request: Request, password: Annotated[str, Form()] = "", csrf: Annotated[str, Form()] = ""):
    try:
        if not auth.csrf_ok(request, csrf):
            raise auth.LoginError("页面停留太久已失效，请重新登录")
        auth.admin_login(request, password)
    except auth.LoginError as e:
        return templates.TemplateResponse(request, "admin.html", {
            "enabled": auth.admin_enabled(),
            "notice": {"kind": "error", "text": str(e)},
        })
    return _back_to_admin(request)


@app.post("/admin/logout")
def admin_logout(request: Request, csrf: Annotated[str, Form()] = ""):
    if auth.csrf_ok(request, csrf):
        auth.admin_logout(request)
    return _back_to_admin(request)


@app.post("/admin/delete")
def admin_delete(request: Request, topic_id: Annotated[int, Form()], csrf: Annotated[str, Form()] = ""):
    if not _admin_ok(request, csrf):
        return _back_to_admin(request)
    project = projects.get(topic_id)
    if project is None:
        return _back_to_admin(request, f"帖子 {topic_id} 没收录过，可能已经删掉了", kind="error")
    projects.remove(topic_id)
    return _back_to_admin(request, f"已删除「{projects.display_title(project.title)}」")


@app.post("/admin/ban")
def admin_ban(
    request: Request,
    topic_id: Annotated[int, Form()],
    csrf: Annotated[str, Form()] = "",
    delete_all: Annotated[bool, Form()] = False,
):
    """从某个项目上封禁它的作者；勾了 delete_all 就把这个作者的项目全删掉。"""
    if not _admin_ok(request, csrf):
        return _back_to_admin(request)
    project = projects.get(topic_id)
    if project is None:
        return _back_to_admin(request, f"帖子 {topic_id} 没收录过，可能已经删掉了", kind="error")
    projects.ban(project.author_id, project.author)
    done = f"已封禁 @{project.author}"
    if delete_all:
        done += f"，删除了 TA 的 {projects.remove_by_author(project.author_id, project.author)} 个项目"
    return _back_to_admin(request, done)


@app.post("/admin/unban")
def admin_unban(request: Request, user_id: Annotated[int, Form()], csrf: Annotated[str, Form()] = ""):
    if not _admin_ok(request, csrf):
        return _back_to_admin(request)
    projects.unban(user_id)
    return _back_to_admin(request, "已解封")
