"""LINUX DO 登录（Connect OAuth2）和表单防伪造。

配置从环境变量读：
  LINUXDO_CLIENT_ID / LINUXDO_CLIENT_SECRET  在 connect.linux.do 申请的应用凭据
  OAUTH_REDIRECT_URI                         和申请时填的回调地址一字不差，默认 https://ossld.com/auth/callback
  SESSION_SECRET                             给登录 Cookie 签名的密钥，线上必须固定，不然每次重启所有人都掉登录
  COOKIE_SECURE                              默认 1（Cookie 只走 https）；本地用 http 调试时设成 0
"""

import os
import secrets
from dataclasses import dataclass
from urllib.parse import urlencode

from curl_cffi import requests
from curl_cffi.requests.exceptions import RequestException
from starlette.requests import Request

AUTHORIZE_URL = "https://connect.linux.do/oauth2/authorize"
TOKEN_URL = "https://connect.linux.do/oauth2/token"
USER_URL = "https://connect.linux.do/api/user"

CLIENT_ID = os.environ.get("LINUXDO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("LINUXDO_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get("OAUTH_REDIRECT_URI", "https://ossld.com/auth/callback")
# 没配就临时生成一个：本地调试够用，代价是重启后要重新登录
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1") != "0"


class LoginError(Exception):
    """登录过程中出错；异常信息直接展示给用户。"""


@dataclass
class User:
    id: int
    username: str
    trust_level: int
    silenced: bool


def enabled() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def current_user(request: Request) -> User | None:
    data = request.session.get("user")
    return User(**data) if data else None


def safe_next(path: str | None) -> str:
    """登录后跳回的地址只认站内路径，防止被人拿来跳到别的网站。"""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path:
        return path
    return "/"


def login_url(request: Request, next_path: str | None) -> str:
    """生成去 LINUX DO 授权的地址；state 存进会话，回调时核对，防止登录被伪造。"""
    state = secrets.token_urlsafe(24)
    request.session["oauth"] = {"state": state, "next": safe_next(next_path)}
    query = urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "user",
        "state": state,
    })
    return f"{AUTHORIZE_URL}?{query}"


def finish_login(request: Request, code: str, state: str) -> str:
    """回调：核对 state → 用 code 换 token → 取用户信息存进会话。返回登录后要跳回的地址。"""
    pending = request.session.pop("oauth", None)
    if not pending or not code or not secrets.compare_digest(pending["state"], state or ""):
        raise LoginError("登录请求已失效，请重新登录")
    try:
        r = requests.post(
            TOKEN_URL,
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT_URI},
            auth=(CLIENT_ID, CLIENT_SECRET),
            impersonate="chrome",
            timeout=15,
        )
        if r.status_code != 200:
            raise LoginError(f"LINUX DO 拒绝了登录（HTTP {r.status_code}），请重试")
        token = r.json()["access_token"]
        r = requests.get(USER_URL, headers={"Authorization": f"Bearer {token}"}, impersonate="chrome", timeout=15)
        if r.status_code != 200:
            raise LoginError(f"拿不到 LINUX DO 账号信息（HTTP {r.status_code}），请重试")
        info = r.json()
    except RequestException as e:
        raise LoginError("连不上 LINUX DO，请稍后再试") from e
    if not info.get("active", True):
        raise LoginError("这个 LINUX DO 账号还没激活")
    # 只存认人要用的几项；access token 用完就丢，不存
    request.session["user"] = {
        "id": info["id"],
        "username": info["username"],
        "trust_level": info.get("trust_level", 0),
        "silenced": bool(info.get("silenced")),
    }
    return pending["next"]


def logout(request: Request) -> None:
    request.session.pop("user", None)


def csrf_token(request: Request) -> str:
    """表单里带的防伪造令牌，跟会话绑定。别的网站拿不到它，就没法替用户提交。"""
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


def csrf_ok(request: Request, token: str) -> bool:
    expected = request.session.get("csrf")
    return bool(expected and token and secrets.compare_digest(expected, token))
