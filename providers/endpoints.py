"""将平台 BaseURL 转换为相应 HTTP / WebSocket 操作地址。"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def api_url(base: str, prefix: str, path: str, *, websocket: bool = False, model: str = "") -> str:
    parsed = urlsplit(base.rstrip("/"))
    root = parsed.path.rstrip("/")
    # 阿里云两个协议共用同一业务空间域名，保留反向代理的路径前缀。
    if websocket:
        if root.endswith("/api/v1"):
            root = root[:-7] + "/api-ws/v1"
        elif not root:
            root = "/api-ws/v1"
    elif root.endswith("/api-ws/v1"):
        root = root[:-10] + "/api/v1"
    if not root:
        root = prefix
    scheme = (
        {"https": "wss", "http": "ws"}.get(parsed.scheme, parsed.scheme)
        if websocket
        else {"wss": "https", "ws": "http"}.get(parsed.scheme, parsed.scheme)
    )
    query = [(key, value) for key, value in parse_qsl(parsed.query) if not (model and key == "model")]
    if model:
        query.append(("model", model))
    return urlunsplit((scheme, parsed.netloc, root + "/" + path.lstrip("/"), urlencode(query), ""))
