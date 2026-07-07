"""Local recovery HTTP API and minimal hotspot upload page."""

from __future__ import annotations

import html
import json
import logging
import mimetypes
import ssl
import threading
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .security import bearer_token, token_matches


class RecoveryApiServer:
    def __init__(
        self,
        controller: Any,
        host: str,
        port: int,
        prefer_fastapi: bool = True,
        tls_enabled: bool = False,
        tls_cert_file: str = "",
        tls_key_file: str = "",
    ):
        self.controller = controller
        self.host = host
        self.port = port
        self.prefer_fastapi = prefer_fastapi
        self.tls_enabled = tls_enabled
        self.tls_cert_file = tls_cert_file
        self.tls_key_file = tls_key_file
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
        tls_files = self._tls_files()
        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            ssl_certfile=tls_files[0] if tls_files else None,
            ssl_keyfile=tls_files[1] if tls_files else None,
        )
        self.uvicorn_server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.uvicorn_server.run, name="orange-recovery-fastapi", daemon=True)
        self.thread.start()
        return True

    def _start_builtin(self) -> None:
        handler = self._handler_class()
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self.port = int(self.httpd.server_address[1])
        tls_files = self._tls_files()
        if tls_files:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certfile=tls_files[0], keyfile=tls_files[1])
            self.httpd.socket = context.wrap_socket(self.httpd.socket, server_side=True)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="orange-recovery-http", daemon=True)
        self.thread.start()

    def _tls_files(self) -> tuple[str, str] | None:
        if not self.tls_enabled:
            return None
        cert = Path(self.tls_cert_file)
        key = Path(self.tls_key_file)
        if cert.is_file() and key.is_file():
            return str(cert), str(key)
        self.logger.warning(
            "TLS is enabled but certificate files are missing; serving recovery API over HTTP. cert=%s key=%s",
            cert,
            key,
        )
        return None

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
                path = urlparse(self.path).path
                if path in {"/", "/index.html"}:
                    controller.record_api_activity()
                    self._html(upload_page(controller.session_token, controller.status(include_token=False)))
                    return
                if not self._authorized():
                    return
                controller.record_api_activity()
                if path == "/status":
                    self._json(controller.status(include_token=False))
                elif path == "/progress":
                    self._json(controller.progress())
                elif path == "/result":
                    self._json(controller.result)
                elif path == "/diagnostics":
                    self._file(controller.diagnostics_zip(), "application/zip")
                else:
                    self._json({"ok": False, "error": "not_found"}, HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:
                path = urlparse(self.path).path
                if not self._authorized():
                    return
                if path == "/upload-repair":
                    self._upload_file(controller.save_and_validate_upload)
                elif path == "/upload-repo":
                    self._upload_file(controller.save_and_apply_repo_bundle)
                elif path == "/apply-repair":
                    payload = self._json_body()
                    self._json(controller.apply_repair(confirm=bool(payload.get("confirm"))))
                elif path == "/restart-service":
                    self._json(controller.restart_service())
                elif path == "/rollback":
                    self._json(controller.rollback())
                elif path == "/exit-recovery":
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

            def _upload_file(self, callback: Any) -> None:
                max_bytes = controller.config.api.max_upload_mb * 1024 * 1024
                length = int(self.headers.get("Content-Length") or "0")
                if length <= 0:
                    self._json({"ok": False, "error": "empty_upload"}, HTTPStatus.BAD_REQUEST)
                    return
                if length > max_bytes + 4096:
                    self._json({"ok": False, "error": "upload_too_large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                    return
                content_type = self.headers.get("Content-Type", "")
                raw = self.rfile.read(length)
                if "multipart/form-data" not in content_type:
                    self._json({"ok": False, "error": "multipart_required"}, HTTPStatus.BAD_REQUEST)
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
                    self._json(callback(filename, body))
                    return
                self._json({"ok": False, "error": "file_field_missing"}, HTTPStatus.BAD_REQUEST)

            def _json(self, payload: dict[str, Any], status: int | HTTPStatus = HTTPStatus.OK) -> None:
                body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _html(self, markup: str, status: int | HTTPStatus = HTTPStatus.OK) -> None:
                body = markup.encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "text/html; charset=utf-8")
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


def upload_page(session_token: str, status: dict[str, Any]) -> str:
    token_json = json.dumps(session_token)
    machine_id = html.escape(str(status.get("machine_id") or "this dispenser"))
    state = html.escape(str(status.get("state") or "RECOVERY"))
    message = html.escape(str(status.get("message") or "Choose the recovery repo ZIP downloaded from Range."))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Orange Recovery Upload</title>
<style>
:root {{ color-scheme: light; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
body {{ background: #f4f8f8; color: #12343b; margin: 0; padding: 18px; }}
main {{ background: #fff; border: 1px solid #d6e3e4; border-radius: 10px; box-shadow: 0 12px 30px rgba(18,52,59,.12); margin: 0 auto; max-width: 560px; padding: 18px; }}
h1 {{ font-size: 1.45rem; margin: 0 0 6px; }}
p {{ line-height: 1.45; }}
.meta {{ background: #eef6f6; border-radius: 8px; display: grid; gap: 4px; margin: 14px 0; padding: 10px; }}
label {{ display: grid; gap: 8px; font-weight: 700; margin: 16px 0; }}
input[type=file] {{ border: 1px solid #bdd4d7; border-radius: 8px; padding: 10px; }}
button {{ background: #247c86; border: 0; border-radius: 8px; color: #fff; font-size: 1rem; font-weight: 800; min-height: 44px; padding: 10px 14px; width: 100%; }}
button:disabled {{ opacity: .65; }}
pre {{ background: #102a30; border-radius: 8px; color: #e8fbfb; overflow: auto; padding: 10px; white-space: pre-wrap; }}
</style>
</head>
<body>
<main>
<h1>Orange Recovery Upload</h1>
<p>Upload the recovery repo ZIP that was downloaded from Range while the phone was on mobile data.</p>
<div class="meta">
<span><strong>Machine:</strong> {machine_id}</span>
<span><strong>State:</strong> {state}</span>
<span>{message}</span>
</div>
<form data-upload-form>
<label>Recovery repo ZIP<input type="file" name="file" accept=".zip,application/zip" required></label>
<button type="submit">Upload to Pi</button>
</form>
<p data-status>Waiting for ZIP file.</p>
<pre data-result hidden></pre>
</main>
<script>
(function () {{
  var token = {token_json};
  var form = document.querySelector('[data-upload-form]');
  var button = form.querySelector('button');
  var file = form.querySelector('input[type="file"]');
  var status = document.querySelector('[data-status]');
  var result = document.querySelector('[data-result]');
  form.addEventListener('submit', function (event) {{
    event.preventDefault();
    if (!file.files.length) {{
      status.textContent = 'Choose the ZIP file first.';
      return;
    }}
    var data = new FormData();
    data.append('file', file.files[0]);
    button.disabled = true;
    status.textContent = 'Uploading...';
    result.hidden = true;
    fetch('/upload-repo', {{
      method: 'POST',
      headers: {{'Authorization': 'Bearer ' + token}},
      body: data
    }}).then(function (response) {{
      return response.json().then(function (payload) {{ return {{ok: response.ok, payload: payload}}; }});
    }}).then(function (response) {{
      var payload = response.payload || {{}};
      result.hidden = false;
      result.textContent = JSON.stringify(payload, null, 2);
      status.textContent = payload.ok ? 'Upload installed. The Pi will restore its normal network shortly.' : 'Upload failed: ' + (payload.error || payload.message || 'unknown error');
    }}).catch(function (error) {{
      status.textContent = 'Upload failed: ' + error.message;
    }}).finally(function () {{
      button.disabled = false;
    }});
  }});
}}());
</script>
</body>
</html>"""


def create_fastapi_app(controller: Any) -> Any:
    try:
        from fastapi import FastAPI, File, Header, HTTPException, UploadFile  # type: ignore
        from fastapi.responses import FileResponse, HTMLResponse, JSONResponse  # type: ignore
    except Exception:
        return None

    app = FastAPI(title="Orange Recovery API", docs_url=None, redoc_url=None, openapi_url=None)

    def require_auth(authorization: str = Header(default="")) -> None:
        if controller.config.api.require_token and not token_matches(bearer_token({"Authorization": authorization}), controller.session_token):
            raise HTTPException(status_code=401, detail="unauthorized")
        controller.record_api_activity()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        controller.record_api_activity()
        return upload_page(controller.session_token, controller.status(include_token=False))

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

    @app.post("/upload-repo")
    async def upload_repo(file: UploadFile = File(...), authorization: str = Header(default="")) -> JSONResponse:
        require_auth(authorization)
        body = await file.read()
        return JSONResponse(controller.save_and_apply_repo_bundle(file.filename or "orange-device-recovery.zip", body))

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
