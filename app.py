"""Start with: python app.py --port 8000 (Python 3.10+, no dependencies)."""
import argparse
import json
import os
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from quantify.data import DemoDataProvider
from quantify.research import demo_csv, evaluate
from quantify.service import analyze, export_csv
from quantify.lab import Lab
from quantify.workbench import Workbench
from quantify.datasets import SCHEMAS

ROOT = Path(__file__).resolve().parent
WORK = threading.BoundedSemaphore(1)
STATIC = {"/forward": ("forward.html", "text/html"), "/forward.js": ("forward.js", "text/javascript"), "/portfolio-validation": ("portfolio_validation.html", "text/html"), "/portfolio-validation.js": ("portfolio_validation.js", "text/javascript"), "/increments": ("increments.html", "text/html"), "/increments.js": ("increments.js", "text/javascript"), "/task-watcher.js": ("task_watcher.js", "text/javascript"), "/diagnostics": ("diagnostics.html", "text/html"), "/diagnostics.js": ("diagnostics.js", "text/javascript"),
          "/portfolio": ("portfolio.html", "text/html"), "/portfolio.js": ("portfolio.js", "text/javascript"),
          "/strategies": ("strategies.html", "text/html"), "/strategies.js": ("strategies.js", "text/javascript"),
          "/market": ("market.html", "text/html"), "/market.js": ("market.js", "text/javascript"),
          "/lab": ("lab.html", "text/html"), "/lab.js": ("lab.js", "text/javascript"),
          "/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/style.css": ("style.css", "text/css"),
          "/workspace": ("workspace.html", "text/html"),
          "/workspace.js": ("workspace.js", "text/javascript"),
          "/workspace.css": ("workspace.css", "text/css")}

WORKSPACE_INIT = threading.Lock()


def reject_constant(value):
    raise ValueError("JSON 不接受 NaN 或 Infinity")


class Handler(BaseHTTPRequestHandler):
    def workbench(self):
        # Scoped to this server so tests and multiple instances can use separate stores.
        with WORKSPACE_INIT:
            if not hasattr(self.server, 'workbench'):
                directory = getattr(self.server, 'data_dir', os.environ.get('QUANTIFY_DATA_DIR', str(ROOT / 'runtime')))
                self.server.workbench = Workbench(directory)
        return self.server.workbench

    def lab(self):
        with WORKSPACE_INIT:
            if not hasattr(self.server, 'lab'):
                directory = getattr(self.server, 'data_dir', os.environ.get('QUANTIFY_DATA_DIR', str(ROOT / 'runtime')))
                self.server.lab = Lab(directory)
        return self.server.lab

    def send_data(self, status, content, mime="application/json"):
        raw = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def send_json(self, status, value):
        self.send_data(status, json.dumps(value, ensure_ascii=False, allow_nan=False))

    def do_GET(self):
        if self.path.startswith('/api/workspace/templates/'):
            kind = self.path.removeprefix('/api/workspace/templates/').removesuffix('.csv')
            if kind not in SCHEMAS:
                self.send_json(404, {'error': '模板不存在'})
            else:
                self.send_data(200, ','.join(SCHEMAS[kind]) + '\n', 'text/csv')
            return
        if self.path.startswith(('/api/workspace/', '/api/lab/', '/api/market/', '/api/strategies/', '/api/portfolio/', '/api/diagnostics/', '/api/increments/', '/api/portfolio-validation/', '/api/pipelines/')):
            try:
                self.send_json(200, (self.lab().pipelines if self.path.startswith('/api/pipelines/') else self.lab().portfolio_lifecycle if self.path.startswith('/api/portfolio-validation/') else self.lab().increments if self.path.startswith('/api/increments/') else self.lab().diagnostics if self.path.startswith('/api/diagnostics/') else self.lab().portfolio if self.path.startswith('/api/portfolio/') else self.lab().lifecycle if self.path.startswith('/api/strategies/') else self.lab().market if self.path.startswith('/api/market/') else self.lab() if self.path.startswith('/api/lab/') else self.workbench()).get(self.path))
            except ValueError as exc:
                self.send_json(400, {'error': str(exc)})
            except (OSError, sqlite3.Error):
                self.send_json(503, {'error': '本地存储不可用，请检查运行目录权限或备份'})
            return
        if self.path == "/api/stocks":
            self.send_json(200, DemoDataProvider().list_stocks())
        elif self.path == "/api/research-template.csv":
            self.send_data(200, demo_csv(), "text/csv")
        elif self.path in STATIC:
            filename, mime = STATIC[self.path]
            self.send_data(200, (ROOT / "static" / filename).read_text(encoding="utf-8"), mime)
        else:
            self.send_json(404, {"error": "路径不存在"})

    def do_POST(self):
        is_pipeline = self.path in ('/api/pipelines/specs', '/api/pipelines/config', '/api/pipelines/run')
        is_portfolio_validation = self.path in ('/api/portfolio-validation/freeze', '/api/portfolio-validation/evaluate', '/api/portfolio-validation/change')
        is_increment = self.path in ('/api/increments/blends', '/api/increments/reports')
        is_diagnostic = self.path == '/api/diagnostics/reports'
        is_lab = self.path in {'/api/lab/' + name for name in ('experiments', 'sessions', 'proposals', 'control', 'retry', 'validate', 'models')}
        is_portfolio = self.path in {'/api/portfolio/' + name for name in ('specs', 'runs', 'demo')}
        is_strategy = self.path in {'/api/strategies/' + name for name in ('freeze', 'evaluate', 'change', 'signal', 'observe')}
        is_market = self.path in {'/api/market/' + name for name in ('config', 'refresh', 'history')}
        if not is_pipeline and not is_portfolio_validation and not is_increment and not is_diagnostic and not is_lab and not is_market and not is_strategy and not is_portfolio and self.path not in ("/api/analyze", "/api/research") and self.path not in {
                "/api/workspace/" + name for name in ("import", "demo", "monthly", "as-of", "factors", "experiments", "compare", "replay")}:
            self.send_json(404, {"error": "路径不存在"})
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json(415, {"error": "仅接受 application/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            limit = 10_000_000 if self.path.startswith("/api/workspace/") else 2_000_000
            if not 0 < length <= limit:
                raise ValueError(f"请求大小必须为 1 字节至 {limit // 1_000_000} MB")
            self.connection.settimeout(10)
            payload = json.loads(self.rfile.read(length).decode("utf-8"), parse_constant=reject_constant)
            if not isinstance(payload, dict):
                raise ValueError("请求必须为 JSON 对象")
            if is_pipeline:
                result = self.lab().pipelines.post(self.path, payload)
            elif is_portfolio_validation:
                result = self.lab().portfolio_lifecycle.post(self.path, payload)
            elif is_increment:
                result = self.lab().increments.post(self.path, payload)
            elif is_diagnostic:
                result = self.lab().diagnostics.post(self.path, payload)
            elif is_portfolio and self.path != '/api/portfolio/demo':
                result = self.lab().portfolio.post(self.path, payload)
            elif is_market:
                result = self.lab().market.post(self.path, payload)
            elif is_lab:
                result = self.lab().post(self.path, payload)
            else:
                if not WORK.acquire(blocking=False):
                    self.send_json(429, {"error": "当前有计算正在运行，请稍后重试"})
                    return
                try:
                    result = self.compute_request(payload)
                finally:
                    # A slow response consumer must not hold the computation slot.
                    WORK.release()
            self.send_json(200, result)
        except (ValueError, TypeError, UnicodeError) as exc:
            self.send_json(400, {"error": str(exc)})
        except (OSError, sqlite3.Error) as exc:
            if isinstance(exc, TimeoutError):
                self.send_json(408, {"error": "读取请求超时"})
            else:
                self.send_json(503, {"error": "本地存储不可用，请检查运行目录权限或备份"})

    def compute_request(self, payload):
        if self.path.startswith("/api/portfolio/"):
            return self.lab().portfolio.post(self.path, payload)
        if self.path.startswith("/api/strategies/"):
            return self.lab().lifecycle.post(self.path, payload)
        if self.path.startswith("/api/workspace/"):
            return self.workbench().post(self.path, payload)
        if self.path == "/api/analyze":
            report = analyze(payload)
        else:
            if set(payload) - {"csv_text", "horizon", "top_k", "cost_bps", "source"}:
                raise ValueError("研究参数字段无效")
            if payload.get("csv_text") is not None and (not isinstance(payload.get("source"), str) or not payload["source"].strip()):
                raise ValueError("导入数据必须声明来源与价格复权/分红口径")
            report = evaluate(**payload)
        return {"report": report, "csv": export_csv(report)}


def main():
    parser = argparse.ArgumentParser(description="本地个人量化研究 demo")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", default=os.environ.get("QUANTIFY_DATA_DIR", str(ROOT / "runtime")), help="研究数据库目录")
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.data_dir = args.data_dir
    server.lab = Lab(args.data_dir)
    print(f"Quantify demo: http://127.0.0.1:{server.server_port}  (Ctrl+C 停止)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.lab.close()
        server.server_close()


if __name__ == "__main__":
    main()
