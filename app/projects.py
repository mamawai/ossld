"""收录的项目：提交检查、数据库存取、封面图和头像。"""

import hashlib
import json
import os
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException

from .auth import User
from .linuxdo import avatar_url, parse_topic_id, topic_url, user_url

REQUIRED_TAG = "开源推广"
BEIJING = timezone(timedelta(hours=8))  # 中国没有夏令时，固定 +8 就行

# 站内固定分类，作者提交时选一个；顺序就是首页分类按钮的顺序
CATEGORIES = ["AI 应用", "开发者工具", "自托管服务", "效率工具", "浏览器扩展与脚本", "桌面应用", "移动应用", "其他"]

# 内容都是作者自己填的，给长度和个数设个上限，免得把页面撑坏
TITLE_MAX = 100
MAX_REPOS = 5

# GitHub 的 owner、repo 允许的字符
_GITHUB_NAME = re.compile(r"[A-Za-z0-9._-]+")

# github.com 下这些第一级路径是 GitHub 自己的页面，不是仓库
_GITHUB_RESERVED = {"orgs", "sponsors", "user-attachments", "topics", "features", "marketplace", "settings", "apps"}

DB_PATH = Path(os.environ.get("DATABASE_PATH", Path(__file__).resolve().parent.parent / "data" / "app.db"))
# 封面图在收录时下载到本地，访客从我们这里加载，不直连 GitHub（国内经常打不开）。
# 头像不下载，页面直接引用 linux.do 的地址，服务器不碰 linux.do
COVERS_DIR = DB_PATH.parent / "covers"
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}

# 标题开头的【开源推广】【开源】[开源自荐] 这类前缀；不带"开源"的方括号可能是项目名，比如 [MetaGateway]，保留
_TITLE_PREFIX = re.compile(r"^(?:\s*[【\[][^】\]]*开源[^】\]]*[】\]])+\s*")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    topic_id      INTEGER PRIMARY KEY,
    title         TEXT    NOT NULL,  -- 原帖标题，原样存，展示时再去前缀
    author_id     INTEGER NOT NULL,
    author        TEXT    NOT NULL,
    author_avatar TEXT    NOT NULL,  -- 头像地址模板，可能为空
    created_at    TEXT    NOT NULL,  -- 原帖发布时间，UTC，ISO 格式；作者手填的只有日期，存北京时间当天 0 点
    github_repos  TEXT    NOT NULL,  -- JSON 数组
    added_at      TEXT    NOT NULL,  -- 收录时间，UTC，ISO 格式
    category      TEXT    NOT NULL DEFAULT '其他'
);

-- 被管理员禁止提交的账号
CREATE TABLE IF NOT EXISTS banned_users (
    user_id   INTEGER PRIMARY KEY,  -- LINUX DO 用户 id
    username  TEXT    NOT NULL,
    banned_at TEXT    NOT NULL      -- UTC，ISO 格式
);
"""

# 后来加的列：老数据库里没有，启动时补上。(列名, 加列语句)
_ADDED_COLUMNS = [
    ("category", "ALTER TABLE projects ADD COLUMN category TEXT NOT NULL DEFAULT '其他'"),
]


class RuleError(Exception):
    """不符合收录规则；异常信息直接展示给用户。"""


class AlreadyCollected(RuleError):
    """帖子已经收录过。"""


@dataclass
class Project:
    topic_id: int
    title: str
    author_id: int
    author: str
    author_avatar: str  # 头像地址模板，登录时没拿到就是空
    created_at: datetime
    github_repos: list[str]
    added_at: datetime
    category: str

    @property
    def url(self) -> str:
        return topic_url(self.topic_id)

    @property
    def author_url(self) -> str:
        return user_url(self.author)

    @property
    def cover_url(self) -> str | None:
        """封面图下载好了就返回地址；还没有就返回 None，页面上显示仓库名代替。"""
        return f"/covers/{self.topic_id}.png" if cover_path(self.topic_id).exists() else None

    @property
    def avatar_src(self) -> str | None:
        """linux.do 上的头像地址；没有就返回 None，页面上显示用户名首字母。

        用 96px 的，页面上 24～32px 显示，高清屏也清楚。
        """
        return avatar_url(self.author_avatar, 96) if self.author_avatar else None


@dataclass
class BannedUser:
    user_id: int
    username: str
    banned_at: datetime


def display_title(title: str) -> str:
    """展示用的标题：去掉开头的开源类前缀，合并多余空格。"""
    shown = re.sub(r"\s+", " ", _TITLE_PREFIX.sub("", title)).strip()
    return shown or title


def block_reason(user: User) -> str | None:
    """这个账号不能提交的原因；能提交就返回 None。"""
    if user.silenced:
        return f"@{user.username} 在 LINUX DO 处于禁言状态，暂时不能提交"
    if is_banned(user.id, user.username):
        return f"@{user.username} 已被管理员禁止提交项目"
    return None


def github_repo(link: str) -> str | None:
    """GitHub 仓库地址规整成 https://github.com/owner/repo；不是仓库地址返回 None。

    后面的 /tree/...、#锚点、.git 都去掉；xxx.github.io 是网站不是仓库，不算。
    """
    u = urlparse(link.strip())
    if u.scheme not in ("http", "https") or u.hostname not in ("github.com", "www.github.com"):
        return None
    parts = [s for s in u.path.split("/") if s]
    if len(parts) < 2 or parts[0].lower() in _GITHUB_RESERVED:
        return None
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if not (_GITHUB_NAME.fullmatch(owner) and _GITHUB_NAME.fullmatch(repo)):
        return None
    return f"https://github.com/{owner}/{repo}"


def new_project(user: User, link: str, title: str, category: str, repos: list[str], published: str) -> Project:
    """作者填的表单 → 待收录的项目。不合格就抛 RuleError。

    帖子内容不去 linux.do 核对，靠提交须知约束；作者就是当前登录的账号。
    """
    topic_id = parse_topic_id(link)
    if topic_id is None:
        raise RuleError("不是 linux.do 的帖子链接，应该像 https://linux.do/t/topic/123456")
    if exists(topic_id):  # 先查重，免得填了半天才知道收录过
        raise AlreadyCollected("这个帖子已经收录过了")

    title = title.strip()
    if not title:
        raise RuleError("请填写标题")
    if len(title) > TITLE_MAX:
        raise RuleError(f"标题最多 {TITLE_MAX} 个字")

    if category not in CATEGORIES:
        raise RuleError("请选一个分类")

    chosen, seen = [], set()
    for raw in repos:
        if not raw.strip():  # 多加了输入框没填的，跳过
            continue
        repo = github_repo(raw)
        if repo is None:
            raise RuleError(f"{raw.strip()} 不是 GitHub 仓库地址，应该像 https://github.com/owner/repo")
        if repo.lower() not in seen:  # GitHub 仓库名不分大小写
            seen.add(repo.lower())
            chosen.append(repo)
    if not chosen:
        raise RuleError("至少填一个 GitHub 仓库地址")
    if len(chosen) > MAX_REPOS:
        raise RuleError(f"GitHub 仓库最多填 {MAX_REPOS} 个")

    try:
        day = date.fromisoformat(published)
    except ValueError:
        raise RuleError("请填写发布日期") from None
    if day > datetime.now(BEIJING).date():
        raise RuleError("发布日期不能晚于今天")

    return Project(
        topic_id=topic_id,
        title=title,
        author_id=user.id,
        author=user.username,
        author_avatar=user.avatar_template,
        created_at=datetime(day.year, day.month, day.day, tzinfo=BEIJING),
        github_repos=chosen,
        added_at=datetime.now(timezone.utc),
        category=category,
    )


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        with conn:  # 正常结束自动提交，出异常自动回滚
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    COVERS_DIR.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.executescript(_SCHEMA)
        have = {row["name"] for row in conn.execute("PRAGMA table_info(projects)")}
        for name, ddl in _ADDED_COLUMNS:
            if name not in have:
                conn.execute(ddl)


def exists(topic_id: int) -> bool:
    with _db() as conn:
        return conn.execute("SELECT 1 FROM projects WHERE topic_id = ?", (topic_id,)).fetchone() is not None


def add(project: Project) -> None:
    try:
        with _db() as conn:
            conn.execute(
                "INSERT INTO projects (topic_id, title, author_id, author, author_avatar, created_at, github_repos, added_at, category)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project.topic_id,
                    project.title,
                    project.author_id,
                    project.author,
                    project.author_avatar,
                    project.created_at.astimezone(timezone.utc).isoformat(),
                    json.dumps(project.github_repos),
                    project.added_at.isoformat(),
                    project.category,
                ),
            )
    except sqlite3.IntegrityError:
        # 两个人同时提交同一个帖子时会走到这里
        raise AlreadyCollected("这个帖子已经收录过了") from None


def set_category(topic_id: int, category: str) -> bool:
    with _db() as conn:
        return conn.execute("UPDATE projects SET category = ? WHERE topic_id = ?", (category, topic_id)).rowcount > 0


def remove(topic_id: int) -> bool:
    cover_path(topic_id).unlink(missing_ok=True)
    with _db() as conn:
        return conn.execute("DELETE FROM projects WHERE topic_id = ?", (topic_id,)).rowcount > 0


def remove_by_author(author_id: int, username: str) -> int:
    """删掉这个作者的全部项目，返回删了几个。认人的规则和 is_banned 一样。"""
    with _db() as conn:
        rows = conn.execute(
            "SELECT topic_id FROM projects WHERE author_id = ? OR lower(author) = lower(?)", (author_id, username)
        ).fetchall()
    for r in rows:
        remove(r["topic_id"])
    return len(rows)


def ban(user_id: int, username: str) -> None:
    with _db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO banned_users (user_id, username, banned_at) VALUES (?, ?, ?)",
            (user_id, username, datetime.now(timezone.utc).isoformat()),
        )


def unban(user_id: int) -> bool:
    with _db() as conn:
        return conn.execute("DELETE FROM banned_users WHERE user_id = ?", (user_id,)).rowcount > 0


def is_banned(user_id: int, username: str) -> bool:
    """id 或用户名对上一个就算。

    老项目存的是论坛的用户 id，和登录给的 id 是不是同一套编号没核实；
    从老项目上封禁时只比 id 可能拦不住人，所以用户名也比（不分大小写）。
    """
    with _db() as conn:
        return conn.execute(
            "SELECT 1 FROM banned_users WHERE user_id = ? OR lower(username) = lower(?)", (user_id, username)
        ).fetchone() is not None


def banned_users() -> list[BannedUser]:
    """按封禁时间从新到旧。"""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM banned_users ORDER BY banned_at DESC").fetchall()
    return [BannedUser(r["user_id"], r["username"], datetime.fromisoformat(r["banned_at"])) for r in rows]


def _to_project(r: sqlite3.Row) -> Project:
    return Project(
        topic_id=r["topic_id"],
        title=r["title"],
        author_id=r["author_id"],
        author=r["author"],
        author_avatar=r["author_avatar"],
        created_at=datetime.fromisoformat(r["created_at"]),
        github_repos=json.loads(r["github_repos"]),
        added_at=datetime.fromisoformat(r["added_at"]),
        category=r["category"],
    )


def list_all() -> list[Project]:
    """按收录时间从新到旧。"""
    with _db() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY added_at DESC").fetchall()
    return [_to_project(r) for r in rows]


def get(topic_id: int) -> Project | None:
    with _db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE topic_id = ?", (topic_id,)).fetchone()
    return _to_project(row) if row else None


def by_author(author_id: int, exclude: int) -> list[Project]:
    """作者的其他项目，按收录时间从新到旧。"""
    with _db() as conn:
        rows = conn.execute(
            "SELECT * FROM projects WHERE author_id = ? AND topic_id != ? ORDER BY added_at DESC", (author_id, exclude)
        ).fetchall()
    return [_to_project(r) for r in rows]


def related(project: Project, n: int = 3) -> list[Project]:
    """相关项目：同分类的优先，随机挑；不够 n 个就从其他分类里随机补。

    作者自己的其他项目在详情页单独一栏，这里不重复。项目不多，直接在内存里挑。
    """
    others = [p for p in list_all() if p.topic_id != project.topic_id and p.author_id != project.author_id]
    same = [p for p in others if p.category == project.category]
    rest = [p for p in others if p.category != project.category]
    picked = random.sample(same, min(n, len(same)))
    return picked + random.sample(rest, min(n - len(picked), len(rest)))


def daily_picks(projects: list[Project], day: str, n: int = 4) -> list[Project]:
    """今日推荐：按"日期 + 帖子编号"算哈希排序取前 n 个。

    同一天结果固定；当天新收录一个项目，最多挤掉一个推荐，不会整组重洗。
    """
    return sorted(projects, key=lambda p: hashlib.sha256(f"{day}:{p.topic_id}".encode()).digest())[:n]


def cover_path(topic_id: int) -> Path:
    return COVERS_DIR / f"{topic_id}.png"  # GitHub 的分享图都是 PNG


def _fetch_image(url: str, attempts: int) -> bytes | None:
    """下载一张图，429 了等一会儿再试。拿不到返回 None。"""
    for attempt in range(attempts):
        if attempt:
            time.sleep(20)
        try:
            r = requests.get(url, impersonate="chrome", timeout=30)
        except RequestException:
            continue
        content_type = r.headers.get("content-type", "").split(";")[0].strip()
        if r.status_code == 200 and content_type in _IMAGE_TYPES:
            return r.content
        if r.status_code != 429:
            break
    return None


def _write_file(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")  # 先写临时文件再改名，页面不会读到写了一半的图
    tmp.write_bytes(data)
    tmp.replace(path)


def save_cover(topic_id: int, repo_url: str) -> bool:
    """下载 GitHub 给仓库自动生成的分享图，当作项目封面。失败了页面显示仓库名，管理员可以用命令补。"""
    # 中间那段是缓存键，随便填；GitHub 第一次生成某个仓库的图时有限流，多给几次机会
    data = _fetch_image(f"https://opengraph.githubassets.com/1/{repo_url.removeprefix('https://github.com/')}", attempts=3)
    if data is None:
        return False
    _write_file(cover_path(topic_id), data)
    return True
