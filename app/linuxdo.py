"""linux.do 的地址：帖子、用户主页、头像，以及从帖子链接里取出帖子编号。

服务器不请求 linux.do 的帖子，项目信息都由作者提交时自己填。
"""

import re
from urllib.parse import quote, urlparse

BASE = "https://linux.do"

# 帖子路径：/t/<slug>/<编号>，后面可能跟楼层号。中文标题的 slug 都是 "topic"
_TOPIC_PATH = re.compile(r"/t/[^/]+/(\d+)(?:/\d+)?/?")


def topic_url(topic_id: int) -> str:
    return f"{BASE}/t/topic/{topic_id}"


def user_url(username: str) -> str:
    return f"{BASE}/u/{quote(username)}"


def avatar_url(template: str, size: int) -> str:
    """头像地址模板 → 实际地址。上传过头像的是站内相对路径，没上传的字母头像可能是完整地址。"""
    url = template.replace("{size}", str(size))
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return BASE + url
    return url


def parse_topic_id(link: str) -> int | None:
    """从帖子链接里取出帖子编号；不是 linux.do 的帖子链接返回 None。"""
    u = urlparse(link.strip())
    m = _TOPIC_PATH.fullmatch(u.path)
    if u.scheme not in ("http", "https") or u.hostname != "linux.do" or not m:
        return None
    return int(m.group(1))
