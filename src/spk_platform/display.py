import hmac
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

STATIC = Path(__file__).with_name("static")
TEMPLATES = Environment(loader=FileSystemLoader(STATIC), autoescape=select_autoescape(["html"]))
STATES = {
    "queued": "排队中",
    "resolving": "准备中",
    "buffering": "命令已提交",
    "playing": "播放器报告播放",
    "stopping": "正在停止",
    "completed": "已结束",
    "failed": "准备失败",
    "cancelled": "已取消",
    "unknown": "状态未知",
}


def render_html(name, **values):
    return HTMLResponse(TEMPLATES.get_template(name).render(**values))


def display_router(state, auth, player):
    router = APIRouter()

    @router.get("/display", response_class=HTMLResponse)
    async def display(
        request: Request, layout: Literal["auto", "portrait", "landscape"] | None = None
    ):
        revision, config = state.config()
        layout = layout or config.appearance.eink_layout
        try:
            auth.session(request)
        except HTTPException:
            response = HTMLResponse("")
            session = auth.issue(response)
            response.body = (
                TEMPLATES.get_template("display-login.html")
                .render(csrf=session["csrf"], configured=auth.configured, layout=layout)
                .encode()
            )
            response.headers["Content-Length"] = str(len(response.body))
            return response
        snapshot = player.snapshot()
        current = snapshot["current"] or {}
        track = current.get("intent") or {}
        title = track.get("title") or ("语音播报" if current.get("kind") == "tts" else "等待播放")
        return render_html(
            "display.html",
            title=title,
            artist=track.get("artist") or "",
            source={
                "ui": "网页点歌",
                "voice": "语音",
                "collection": "歌单",
                "alarm": "闹钟",
                "queue": "队列",
            }.get(current.get("source"), ""),
            status=STATES.get(current.get("status"), "空闲"),
            device=STATES.get(snapshot["device_state"], "设备状态未知"),
            queue=snapshot["queue"],
            revision=revision,
            refresh=config.appearance.eink_refresh_seconds,
            layout=layout,
        )

    @router.post("/display/login")
    async def login(request: Request, layout: Literal["auto", "portrait", "landscape"] = "auto"):
        form = await request.form()
        session = auth.session(request, authenticated=False)
        origin = request.headers.get("Origin", "")
        if not origin:
            referrer = urlsplit(request.headers.get("Referer", ""))
            origin = "%s://%s" % (referrer.scheme, referrer.netloc)
        if origin != auth.origin or not hmac.compare_digest(
            str(form.get("csrf", "")).encode(), session["csrf"].encode()
        ):
            raise HTTPException(403, "Invalid display login origin or CSRF token")
        auth.rates.check("display-login:" + request.client.host, 10)
        auth.login(str(form.get("password", "")))
        response = RedirectResponse("/display?layout=" + layout, status_code=303)
        auth.issue(response, authenticated=True)
        return response

    @router.post("/display/logout")
    async def logout(request: Request):
        form = await request.form()
        session = auth.session(request)
        if not hmac.compare_digest(str(form.get("csrf", "")).encode(), session["csrf"].encode()):
            raise HTTPException(403)
        auth.logout(request)
        response = RedirectResponse("/display", status_code=303)
        response.delete_cookie("spk_session")
        return response

    return router
