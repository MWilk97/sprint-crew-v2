"""HTTP routes — partial task CRUD; notification endpoints in story 3."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import urlparse

from platform.config import default_config
from storage.sqlite_repo import SqliteRepository

_repo = SqliteRepository(default_config().database_path)


class TaskHandler(BaseHTTPRequestHandler):
  def _json(self, status: int, payload: dict[str, Any]) -> None:
      body = json.dumps(payload).encode()
      self.send_response(status)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)

  def do_GET(self) -> None:
      path = urlparse(self.path).path
      if path == "/tasks":
          self._json(200, {"tasks": _repo.list_tasks()})
          return
      if path.startswith("/notifications/"):
          # story 3: query delivery status
          self._json(501, {"error": "not implemented"})
          return
      self._json(404, {"error": "not found"})

  def do_POST(self) -> None:
      path = urlparse(self.path).path
      if path == "/tasks":
          length = int(self.headers.get("Content-Length", 0))
          data = json.loads(self.rfile.read(length) or b"{}")
          task_id = str(data.get("id", ""))
          title = str(data.get("title", ""))
          if not task_id or not title:
              self._json(400, {"error": "id and title required"})
              return
          _repo.create_task(task_id, title)
          self._json(201, {"id": task_id, "title": title})
          return
      if path == "/notifications":
          # story 3: enqueue notification
          self._json(501, {"error": "not implemented"})
          return
      self._json(404, {"error": "not found"})

  def log_message(self, format: str, *args: object) -> None:
      return


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    HTTPServer((host, port), TaskHandler).serve_forever()
