"""Local recovery HTTP API.

The service has no browser UI and no template rendering. It exposes only JSON
and ZIP responses for the phone/admin app.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .security import bearer_token, token_matches


class RecoveryApiServer:
    def __init__(self, controller: Any, host: str, port: int, prefer_fastapi: bool = True):
        self.controller = controller
        self.host = host
        self.port = port
        self.prefer_fastapi = prefer_fastapi
        self.logger = logging.getLogger("orange_recovery.api")
        self.httpd: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.uvicorn_server: Any = None

    def start(self) -> None:
        if self.prefer_fastapi and self._start_fastapi():
            return
        self._start_builtin()

    def _start_fastapi(self) -> bool:
        try:
            import uvicorn  # type: ignore
        except Exception:
            return False
        try:
            app = create_fastapi_app(self.controller)
        except Exception:
            self.logger.exception("FastAPI recovery server setup failed; falling back to built-in HTTP server")
            return False
        if app is None:
            return False
        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="warning", access_log=False)
        self.uvicorn_server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.uvicorn_server.run, name="orange-recovery-fastapi", daemon=True)
        self.thread.start()
        return True

    def _start_builtin(self) -> None:
        handler = self._handler_class()
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = int(self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="orange-recovery-http", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.uvicorn_server is not None:
            self.uvicorn_server.should_exit = True
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        controller = self.controller

        class Handler(BaseHTTPRequestHandler):
            server_version = "OrangeRecovery/1.0"

            def log_message(self, format: str, *args: Any) -> None:
                logging.getLogger("orange_recovery.api").info(format, *args)

            def do_GET(self) -> None:
                if not self._authorized():
                    return
                controller.record_api_activity()
                if self.path == "/status":
                    self._json(controller.status(include_token=False))
                elif self.path == "/progress":
                    self._json(controller.progress())
                elif self.path == "/result":
                    self._json(controller.result)
                elif self.path == "/diagnostics":
                    self._file(controller.diagnostics_zip(), "application/zip")
                else:
                    self._json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:
                if not self._authorized():
                    return
                if self.path == "/upload-repair":
                    self._upload_repair()
                elif self.path == "/apply-repair":
                    payload = self._json_body()
                    self._json(controller.apply_repair(confirm=bool(payload.get("confirm"))))
                elif self.path == "/restart-service":
                    self._json(controller.restart_service())
                elif self.path == "/rollback":
                    self._json(controller.rollback())
                elif self.path == "/exit-recovery":
                    controller.record_api_activity()
                    self._json({"ok": True, "state": "RESTORING_NETWORK", "message": "Recovery mode is stopping."})
                    controller.exit_recovery_async()
                else:
                    self._json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)

            def _authorized(self) -> bool:
                if not controller.config.api.require_token:
                    return True
                headers = {key: value for key, value in self.headers.items()}
                if token_matches(bearer_token(headers), controller.session_token):
                    return True
                self._json({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
                return False

            def _json_body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or "0")
                if length <= 0:
                    return {}
                raw = self.rfile.read(min(length, 1024 * 1024))
                try:
                    payload = json.loads(raw.decode("utf-8"))
                    return payload if isinstance(payload, dict) else {}
                except json.JSONDecodeError:
                    return {}

            def _upload_repair(self) -> None:
                max_bytes = controller.config.api.max_upload_mb * 1024 * 1024
                length = int(self.headers.get("Content-Length") or "0")
                if length <= 0:
                    self._json({"ok": False, "package_valid": False, "error": "empty_upload"}, HTTPStatus.BAD_REQUEST)
                    return
                if length > max_bytes + 4096:
                    self._json({"ok": False, "package_valid": False, "error": "upload_too_large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                content_type = self.headers.get("Content-Type", "")
                raw = self.rfile.read(length)
                if "multipart/form-data" not in content_type:
                    self._json({"ok": False, "package_valid": False, "error": "multipart_required"}, HTTPStatus.BAD_REQUEST)
                    return
                message = BytesParser(policy=email_policy).parsebytes(
                    ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode("utf-8") + raw
                )
                for part in message.iter_parts():
                    disposition = part.get_content_disposition()
                    if disposition != "form-data":
                        continue
                    if part.get_param("name", header="content-disposition") != "file":
                        continue
                    filename = part.get_filename() or "repair_package.zip"
                    body = part.get_payload(decode=True) or b""
                    self._json(controller.save_and_validate_upload(filename, body))
                    return
                self._json({"ok": False, "package_valid": False, "error": "file_field_missing"}, HTTPStatus.BAD_REQUEST)

            def _json(self, payload: dict[str, Any], status: int | HTTPStatus = HTTPStatus.OK) -> None:
                body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _file(self, path: str, content_type: str | None = None) -> None:
                file_path = Path(path)
                if not file_path.exists():
                    self._json({"ok": False, "error": "file_not_found"}, HTTPStatus.NOT_FOUND)
                    return
                body = file_path.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type or mimetypes.guess_type(path)[0] or "application/octet-stream")
                self.send_header("Content-Disposition", f'attachment; filename="{file_path.name}"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def create_fastapi_app(controller: Any) -> Any:
    try:
        from fastapi import FastAPI, File, Header, HTTPException, UploadFile  # type: ignore
        from fastapi.responses import FileResponse, JSONResponse  # type: ignore
    except Exception:
        return None

    app = FastAPI(title="Orange Recovery API", docs_url=None, redoc_url=None, openapi_url=None)

    def require_auth(authorization: str = Header(default="")) -> None:
        if controller.config.api.require_token and not token_matches(bearer_token({"Authorization": authorization}), controller.session_token):
            raise HTTPException(status_code=401, detail="unauthorized")
        controller.record_api_activity()

    @app.get("/status")
    def status(authorization: str = Header(default="")) -> dict[str, Any]:
        require_auth(authorization)
        return controller.status(include_token=False)

    @app.get("/progress")
    def progress(authorization: str = Header(default="")) -> dict[str, Any]:
        require_auth(authorization)
        return controller.progress()

    @app.get("/result")
    def result(authorization: str = Header(default="")) -> dict[str, Any]:
        require_auth(authorization)
        return controller.result

    @app.get("/diagnostics")
    def diagnostics(authorization: str = Header(default="")) -> Any:
        require_auth(authorization)
        return FileResponse(controller.diagnostics_zip(), media_type="application/zip")

    @app.post("/upload-repair")
    async def upload_repair(file: UploadFile = File(...), authorization: str = Header(default="")) -> JSONResponse:
        require_auth(authorization)
        body = await file.read()
        return JSONResponse(controller.save_and_validate_upload(file.filename or "repair_package.zip", body))

    @app.post("/apply-repair")
    def apply_repair(payload: dict[str, Any], authorization: str = Header(default="")) -> dict[str, Any]:
        require_auth(authorization)
        return controller.apply_repair(confirm=bool(payload.get("confirm")))

    @app.post("/restart-service")
    def restart_service(authorization: str = Header(default="")) -> dict[str, Any]:
        require_auth(authorization)
        return controller.restart_service()

    @app.post("/rollback")
    def rollback(authorization: str = Header(default="")) -> dict[str, Any]:
        require_auth(authorization)
        return controller.rollback()

    @app.post("/exit-recovery")
    def exit_recovery(authorization: str = Header(default="")) -> dict[str, Any]:
        require_auth(authorization)
        controller.exit_recovery_async()
        return {"ok": True, "state": "RESTORING_NETWORK", "message": "Recovery mode is stopping."}

    return app
