#!/usr/bin/env python3
"""归位 · 控制台 —— 给 organize.py 套一个本地面板:配置、索引、运行情况一眼看全。

零依赖:只用标准库(http.server + 原生前端),和引擎本身一样不引任何第三方包。
    python ui.py --root ~/Inbox
    python ui.py --root ~/Inbox --port 8787 --open

安全:只绑 127.0.0.1;配置里的 key 只显示长度,不回显;POST 做同源检查。
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import organize as gw  # noqa: E402  引擎本体,复用它的配置/索引/判定逻辑

ROOT = Path(os.path.expanduser("~/Inbox"))
JOBS = {}
JOBS_LOCK = threading.Lock()
COMMANDS = {
    "scan": ["scan"],                       # dry-run:只写 _预览.md
    "apply": ["apply"],                     # 实跑:改名 + 归档 + 写索引
    "undo": ["undo"],
    "reindex": ["reindex"],
    "reconcile": ["reconcile", "--dry-run"],
    "folders": ["folders"],
    "init": ["init"],
}


def _cfg_or_error():
    """返回 (cfg, None) 或 (None, 错误文案)。配错也能进面板——正好把错误摆在首页。"""
    try:
        cfg = gw.load_config(str(ROOT))
        # 和 CLI 的 main() 一样:--root 优先于 规则.json 里的 general.root,
        # 否则台账/索引库会指向 ~/Inbox 而不是面板正在看的这个收件夹。
        cfg["general"]["root"] = str(ROOT)
        return cfg, None
    except SystemExit as e:
        return None, str(e)
    except Exception as e:  # 解析失败等
        return None, f"{type(e).__name__}: {e}"


def _cred_source():
    if os.environ.get("LLM_API_KEY"):
        return "环境变量 LLM_API_KEY"
    if os.environ.get("GUIWEI_SETTINGS"):
        return f"GUIWEI_SETTINGS={os.environ['GUIWEI_SETTINGS']}"
    local = HERE / "settings.local.json"
    if local.exists():
        return str(local)
    home = Path.home() / ".claude" / "settings.local.json"
    if home.exists():
        return str(home)
    return "未配置"


def _brain_state(cfg):
    b = cfg.get("brain", {})
    backend = b.get("backend", "?")
    group = b.get("credential_group") or "llm_relay"
    key, base = None, ""
    try:
        key, base = gw.read_credential(group)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    local_only = bool(b.get("local_only"))
    loop = gw.is_loopback(base)
    if backend == "claude_cli":
        verdict, on_machine = "本机 Claude 订阅", True
    elif loop:
        verdict, on_machine = "本机端点(loopback)", True
    else:
        verdict, on_machine = "远端端点", False
    return {
        "backend": backend,
        "model": b.get("model", ""),
        "base_url": base or "",
        "loopback": loop,
        "verdict": verdict,
        "on_machine": on_machine,
        "local_only": local_only,
        "local_only_ok": on_machine or not local_only,
        "key_set": bool(key),
        "key_len": len(key) if key else 0,
        "cred_source": _cred_source(),
        "timeout": b.get("timeout"),
        "max_concurrent": b.get("max_concurrent"),
        "gemini_set": bool(os.environ.get("GEMINI_API_KEY")),
    }


def _ledger(cfg):
    try:
        return gw.live_records(cfg) or []
    except Exception:
        return []


def _db_rows(cfg, q="", cat="", limit=300):
    path = gw.db_path(cfg)
    if not Path(path).exists():
        return []
    where, args = [], []
    if q:
        like = f"%{q}%"
        where.append("(new_name LIKE ? OR summary LIKE ? OR kehu LIKE ? OR renming LIKE ? OR "
                     "xiangmu LIKE ? OR leixing LIKE ? OR orig_name LIKE ?)")
        args += [like] * 7
    if cat:
        where.append("category = ?")
        args.append(cat)
    sql = ("SELECT date,category,new_name,kehu,renming,xiangmu,leixing,summary,new_path,sensitive "
           "FROM idx")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY date DESC, new_name LIMIT ?"
    args.append(limit)
    try:
        con = sqlite3.connect(path)
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, args)]
    except Exception:
        return []
    finally:
        try:
            con.close()
        except Exception:
            pass


def _read_text(*parts):
    p = Path(ROOT).joinpath(*parts)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return p.read_text(encoding="utf-8", errors="replace")


def build_status():
    cfg, err = _cfg_or_error()
    out = {"root": str(ROOT), "ok": err is None, "error": err, "now": time.strftime("%Y-%m-%d %H:%M:%S")}
    if cfg is None:
        out["brain"] = {"backend": "?", "verdict": "配置无法加载", "local_only_ok": False}
        return out
    ledger = _ledger(cfg)
    cats = Counter((r.get("category") or "?") for r in ledger)
    out["brain"] = _brain_state(cfg)
    out["stats"] = {
        "ledger": len(ledger),
        "db": len(_db_rows(cfg, limit=100000)),
        "categories": [{"name": k, "n": v} for k, v in cats.most_common()],
        "recent": ledger[-6:][::-1],
        "last_ts": (ledger[-1].get("ts") if ledger else ""),
    }
    out["general"] = {
        "system_dir": cfg["general"].get("system_dir"),
        "stable_seconds": cfg["general"].get("stable_seconds"),
        "root_abs": str(Path(cfg["general"].get("root", "")).expanduser()),
    }
    out["files"] = {
        "preview": bool(Path(ROOT, cfg["general"]["system_dir"], "_预览.md").exists()),
        "catalog": bool(Path(ROOT, cfg["general"]["system_dir"], "目录.md").exists()),
        "ledger_file": bool(Path(gw.ledger_path(cfg)).exists()),
        "db_file": bool(Path(gw.db_path(cfg)).exists()),
    }
    return out


# ---------------------------------------------------------------- 子进程任务
class Job:
    def __init__(self, key, cmd, root):
        self.key = key
        self.cmd = cmd
        self.root = str(root)
        self.started = time.strftime("%H:%M:%S")
        self.lines = []
        self.done = False
        self.code = None
        self.cv = threading.Condition()

    def push(self, line):
        with self.cv:
            self.lines.append(line)
            self.cv.notify_all()

    def finish(self, code):
        with self.cv:
            self.done = True
            self.code = code
            self.cv.notify_all()


def run_job(cmd):
    args = COMMANDS.get(cmd)
    if not args:
        return None, f"未知命令: {cmd}"
    with JOBS_LOCK:
        for j in JOBS.values():
            if not j.done:
                return None, "已有一个任务在跑，等它结束再来。"
        key = f"{int(time.time() * 1000)}"
        job = Job(key, cmd, ROOT)
        JOBS[key] = job

    def worker():
        env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
        argv = [sys.executable, str(HERE / "organize.py"), *args, "--root", str(ROOT)]
        job.push(f"$ organize.py {' '.join(args)} --root {ROOT}")
        try:
            p = subprocess.Popen(argv, cwd=str(HERE), env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                 errors="replace", bufsize=1)
            for line in p.stdout:
                job.push(line.rstrip("\n"))
            code = p.wait()
        except Exception as e:
            job.push(f"[启动失败] {type(e).__name__}: {e}")
            code = -1
        job.push(f"[结束] 退出码 {code}")
        job.finish(code)

    threading.Thread(target=worker, daemon=True).start()
    return key, None


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "guiwei-ui"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):  # 安静点
        pass

    def _send(self, obj, code=200, ctype="application/json; charset=utf-8"):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        return urlparse(origin).netloc == host

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            html = (HERE / "ui.html").read_bytes()
            return self._send(html, ctype="text/html; charset=utf-8")
        if u.path == "/api/status":
            return self._send(build_status())
        if u.path == "/api/config":
            cfg, err = _cfg_or_error()
            text = _read_text(cfg["general"]["system_dir"], "规则.json") if cfg else ""
            return self._send({"text": text, "error": err,
                               "brain": _brain_state(cfg) if cfg else None})
        if u.path == "/api/index":
            cfg, err = _cfg_or_error()
            if not cfg:
                return self._send({"rows": [], "error": err}, 500)
            rows = _db_rows(cfg, q=(qs.get("q") or [""])[0], cat=(qs.get("cat") or [""])[0])
            return self._send({"rows": rows})
        if u.path in ("/api/preview", "/api/catalog"):
            cfg, err = _cfg_or_error()
            if not cfg:
                return self._send({"text": "", "error": err}, 500)
            name = "_预览.md" if u.path.endswith("preview") else "目录.md"
            return self._send({"text": _read_text(cfg["general"]["system_dir"], name)})
        if u.path == "/api/job":
            key = (qs.get("id") or [""])[0]
            return self._sse(key)
        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        if not self._same_origin():
            return self._send({"error": "跨站请求被拒"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send({"error": "请求体不是合法 JSON"}, 400)
        if u.path == "/api/run":
            key, err = run_job(payload.get("cmd", ""))
            return self._send({"id": key} if key else {"error": err}, 200 if key else 400)
        if u.path == "/api/open":
            # 在资源管理器里打开收件夹。用 explorer.exe 直接起进程:
            # ShellExecute(os.startfile) 在受限环境下会被拒(WinError 5),这条不会。
            try:
                if sys.platform.startswith("win"):
                    subprocess.Popen(["explorer.exe", str(ROOT)])
                else:
                    subprocess.Popen(["xdg-open", str(ROOT)])
                return self._send({"ok": True})
            except Exception as e:
                return self._send({"error": f"{type(e).__name__}: {e}"}, 500)
        if u.path == "/api/config":
            return self._save_config(payload.get("text", ""))
        return self._send({"error": "not found"}, 404)

    def _save_config(self, text):
        cfg, err = _cfg_or_error()
        if not cfg:
            return self._send({"error": err}, 400)
        target = Path(ROOT, cfg["general"]["system_dir"], "规则.json")
        try:
            json.loads(gw.strip_jsonc(text))          # 先验语法,坏 JSON 不落盘
        except Exception as e:
            return self._send({"error": f"JSON 解析失败(支持整行 // 注释): {e}"}, 400)
        old = target.read_text(encoding="utf-8") if target.exists() else ""
        try:
            target.write_text(text, encoding="utf-8")
            gw.load_config(str(ROOT))                 # 再跑一遍引擎的启动校验(local_only 等)
        except SystemExit as e:                       # 冲突:回滚,把引擎原话给到界面
            target.write_text(old, encoding="utf-8")
            return self._send({"error": str(e)}, 400)
        except Exception as e:
            target.write_text(old, encoding="utf-8")
            return self._send({"error": f"{type(e).__name__}: {e}"}, 400)
        return self._send({"ok": True})

    def _sse(self, key):
        job = JOBS.get(key)
        if not job:
            return self._send({"error": "任务不存在"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        sent = 0
        try:
            while True:
                with job.cv:
                    while sent >= len(job.lines) and not job.done:
                        job.cv.wait(timeout=15)
                    if sent >= len(job.lines) and job.done:
                        break
                    batch = job.lines[sent:]
                for line in batch:
                    self.wfile.write(f"data: {json.dumps({'line': line}, ensure_ascii=False)}\n\n".encode())
                sent += len(batch)
                if job.done and sent >= len(job.lines):
                    break
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        try:
            self.wfile.write(f"event: done\ndata: {json.dumps({'code': job.code})}\n\n".encode())
            self.wfile.flush()
        except Exception:
            pass


def main():
    global ROOT
    ap = argparse.ArgumentParser(description="归位 · 控制台")
    ap.add_argument("--root", default=os.environ.get("GUIWEI_ROOT") or str(ROOT))
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--open", action="store_true", help="起来后自动开浏览器")
    a = ap.parse_args()
    ROOT = Path(a.root).expanduser().resolve()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://127.0.0.1:{a.port}/"
    print(f"归位 · 控制台  {url}   收件夹: {ROOT}")
    print("Ctrl+C 退出。只监听 127.0.0.1，key 不回显。")
    if a.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n再见。")


if __name__ == "__main__":
    main()
