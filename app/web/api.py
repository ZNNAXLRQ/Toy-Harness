import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from app.web.runtime import ProviderUnavailable, Runtime

STATIC = Path(__file__).resolve().parents[2] / "static"
LOCAL_HOST = re.compile(r"(?:localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", re.IGNORECASE)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)
    prompt: str = Field(min_length=1, max_length=8192)


def reject(status, code):
    return JSONResponse({"error": code}, status_code=status, headers={"Cache-Control": "no-store"})


def create_app(workspace, *, offline=False, fake_provider_factory=None):
    runtime = Runtime(workspace, offline, fake_provider_factory)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        hosts = request.headers.getlist("host")
        if len(hosts) != 1 or not LOCAL_HOST.fullmatch(hosts[0]):
            return reject(403, "invalid_host")
        if request.url.path.startswith("/api/"):
            origins = request.headers.getlist("origin")
            fetch_sites = request.headers.getlist("sec-fetch-site")
            authority = hosts[0].lower()
            scheme = request.scope["scheme"]
            default_port = ":443" if scheme == "https" else ":80"
            expected_origin = f"{scheme}://{authority.removesuffix(default_port)}"
            if ((origins and origins != [expected_origin])
                    or (fetch_sites and fetch_sites not in (["same-origin"], ["none"]))):
                return reject(403, "cross_origin")
            if request.method == "POST" and request.url.path == "/api/runs":
                if request.url.query:
                    return reject(422, "invalid_request")
                types = request.headers.getlist("content-type")
                if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
                    return reject(415, "json_required")
            response = await call_next(request)
            response.headers["Cache-Control"] = "no-store"
            return response
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        return reject(422, "invalid_request")

    @app.get("/", include_in_schema=False)
    def index():
        return RedirectResponse("/html/index.html?mode=web")

    @app.get("/api/runtime")
    def capabilities():
        return {
            "provider_mode": "offline" if runtime.offline else "configured",
            "capabilities": {
                "single_turn": True, "tool_policy": "read_only",
                "event_delivery": "with_result", "session_resume": False,
            },
        }

    @app.get("/api/tools")
    def tools():
        return {"tools": runtime.directory()}

    @app.post("/api/runs")
    def run(body: RunRequest):
        # A synchronous route runs the blocking Provider and Agent in a worker thread.
        try:
            status, result = runtime.run(body.prompt)
            return JSONResponse(result, status_code=status)
        except ProviderUnavailable:
            return reject(503, "provider_unavailable")
        except Exception:
            return reject(500, "internal_error")

    for directory in ("html", "css", "js"):
        app.mount(f"/{directory}", StaticFiles(directory=STATIC / directory), name=directory)
    return app