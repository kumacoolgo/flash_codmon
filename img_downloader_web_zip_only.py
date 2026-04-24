#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
img_downloader_web_zip_only.py

说明:
- 访问 /login 登录后，才能使用下载页面（/）。
- 账号密码从环境变量读取：
  - APP_USERNAME
  - APP_PASSWORD
- 文件下载逻辑与原版一致：
  - 前端粘贴多行 URL
  - 后端为每个任务生成 UUID task_id
  - 每张图片下载 + 打包 ZIP（按最终文件名去重，同名只保留第一张）
  - 提供 SSE 进度 /progress/<task_id>
  - 提供下载 /download_final/<task_id>
"""

import os
import re
import time
import uuid
import json
import queue
import threading
import urllib.parse
from io import BytesIO
from zipfile import ZipFile

import requests
from flask import (
    Flask,
    render_template_string,
    request,
    Response,
    send_file,
    jsonify,
    redirect,
    url_for,
    session,
)

# ====== 配置 ======
# 临时 zip 存放目录，可以用环境变量 TMP_DIR 覆盖（在 Zeabur 上可以挂到 /data/tmp_zip）
TMP_DIR = os.environ.get("TMP_DIR", "tmp_zip")
ZIP_TTL_SECONDS = 3600       # ZIP 存放时间（秒），超过将被后台清理
CLEANUP_INTERVAL = 600       # 清理线程间隔（秒）
DOWNLOAD_TIMEOUT = 20        # requests 超时时间（秒）
CHUNK_SIZE = 1024            # 下载分块大小（字节）

# 登录账号密码（在 Zeabur 环境变量设置）
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "password")

os.makedirs(TMP_DIR, exist_ok=True)

app = Flask(__name__)

# Flask session 加密密钥，务必在 Zeabur 上设置 SECRET_KEY
app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_ME_SECRET_KEY")

# task_id -> queue.Queue()（用于 SSE 推送进度）
progress_queues = {}

# ====== 辅助函数 ======
_filename_sanitize_re = re.compile(r'[^A-Za-z0-9._\-]')

def sanitize_filename(name: str, max_len: int = 200) -> str:
    """将文件名中不安全字符替换为下划线，并限制长度。"""
    if not name:
        name = "file"
    name = _filename_sanitize_re.sub("_", name)
    if len(name) > max_len:
        name = name[:max_len]
    return name

def extract_filename_from_url(url: str) -> str:
    """
    从图片 URL 中提取文件名。

    适配 Codmon 这种格式：
    https://image.codmon.com/codmon/13774/albums/A7V08093.jpg?Policy=...&width=500...

    规则：
    - 忽略 ? 后面的签名参数
    - 从 path 中取最后一段文件名
    - 例如只截取 A7V08093.jpg
    - 若没有扩展名则追加 .jpg
    - 最后做安全字符过滤
    """
    try:
        parsed = urllib.parse.urlparse(url.strip())
        basename = os.path.basename(parsed.path) or ""

        # 极端情况下，如果用户粘贴的 URL 没有被正常解析，再手动去 query 后取最后一段
        if not basename:
            clean = url.split("?", 1)[0]
            basename = clean.rstrip("/").split("/")[-1]

        if not basename:
            basename = "file.jpg"

        # 处理被 URL 编码的文件名
        basename = urllib.parse.unquote(basename)

        if "." not in basename:
            basename = basename + ".jpg"

        return sanitize_filename(basename)
    except Exception:
        return sanitize_filename("file.jpg")

# ====== 登录相关 ======
def is_logged_in() -> bool:
    return bool(session.get("logged_in"))

def login_required(view_func):
    """简单的登录保护装饰器。未登录时跳转到 /login"""
    from functools import wraps
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            # 对 API（JSON/SSE）来说，直接返回 401 更合适
            if request.path.startswith("/start") or request.path.startswith("/progress") or request.path.startswith("/download_final"):
                # 返回 JSON 错误（SSE 的话前端会触发 onerror）
                return jsonify({"error": "unauthorized"}), 401
            # 普通页面跳登录页
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapper

LOGIN_PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>登录 - 图片批量下载器</title>
<style>
body{font-family:Arial,Helvetica,sans-serif;margin:0;background:#f0f2f5;display:flex;justify-content:center;align-items:center;height:100vh}
.box{background:#fff;padding:24px 28px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.1);width:320px}
h2{margin-top:0;margin-bottom:16px;text-align:center}
label{display:block;margin-top:8px}
input{width:100%;padding:8px;margin-top:4px;box-sizing:border-box;border-radius:4px;border:1px solid #ddd}
button{width:100%;margin-top:16px;padding:10px;background:#007bff;color:#fff;border:none;border-radius:4px;cursor:pointer}
button:hover{background:#0069d9}
.error{color:#e53935;font-size:13px;margin-top:8px;text-align:center}
.tip{font-size:12px;color:#888;margin-top:12px;text-align:center}
</style>
</head>
<body>
<div class="box">
  <h2>图片批量下载器</h2>
  <form method="post" action="/login">
    <label for="username">账号</label>
    <input id="username" name="username" required autocomplete="username">
    <label for="password">密码</label>
    <input id="password" type="password" name="password" required autocomplete="current-password">
    {% if error %}
    <div class="error">{{ error }}</div>
    {% endif %}
    <button type="submit">登录</button>
    {% if next_path %}
    <input type="hidden" name="next" value="{{ next_path }}">
    {% endif %}
  </form>
  <div class="tip">账号密码由管理员通过环境变量配置。</div>
</div>
</body>
</html>
"""

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    next_path = request.args.get("next") or request.form.get("next") or "/"
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        if username == APP_USERNAME and password == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(next_path or "/")
        else:
            error = "账号或密码错误"
    return render_template_string(LOGIN_PAGE, error=error, next_path=next_path)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ====== 下载工作线程 ======
def download_worker(urls, q: queue.Queue, task_id: str):
    """
    异步下载所有 URL，实时将 items（每张图片的 name/status/progress）放入队列推送给前端。
    最后在 tmp_zip/<task_id>.zip 写入打包结果并发送 done=True。
    """
    items = []  # 每个元素: {"name": filename, "status": "...", "progress": 0..100}
    zip_buffer = BytesIO()

    seen_filenames = set()

    with ZipFile(zip_buffer, "w") as zf:
        for i, url in enumerate(urls, start=1):
            filename = extract_filename_from_url(url)

            # 按最终文件名去重：
            # 例如 A7V08093.jpg 出现多次，只下载/打包第一张，不再生成 A7V08093_1.jpg。
            if filename in seen_filenames:
                continue
            seen_filenames.add(filename)

            item = {"name": filename, "status": "下载中", "progress": 0}
            items.append(item)
            q.put({"items": items.copy(), "done": False})

            try:
                # stream 下载以便计算进度
                with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length") or 0)
                    downloaded = 0
                    chunks = []
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        chunks.append(chunk)
                        downloaded += len(chunk)
                        if total:
                            item["progress"] = int(downloaded * 100 / total)
                        else:
                            item["progress"] = 100  # 无 content-length 的情况下直接置 100
                        # 推送当前状态（浅拷贝）
                        q.put({"items": items.copy(), "done": False})

                    content_bytes = b"".join(chunks)

                # 写入 ZIP（使用去重后的原始文件名）
                zf.writestr(filename, content_bytes)
                item["status"] = "完成"
                item["progress"] = 100
                q.put({"items": items.copy(), "done": False})

            except requests.exceptions.RequestException as e:
                item["status"] = f"失败: {str(e)}"
                item["progress"] = 100
                q.put({"items": items.copy(), "done": False})
            except Exception as e:
                item["status"] = f"失败: {str(e)}"
                item["progress"] = 100
                q.put({"items": items.copy(), "done": False})

    # 写入磁盘
    zip_buffer.seek(0)
    zip_path = os.path.join(TMP_DIR, f"{task_id}.zip")
    with open(zip_path, "wb") as f:
        f.write(zip_buffer.read())

    # 通知前端完成 (done=True)
    q.put({"items": items.copy(), "done": True})

# ====== SSE 进度流 ======
@app.route("/progress/<task_id>")
@login_required
def progress_stream(task_id):
    """
    Server-Sent Events (SSE) 端点，前端通过 EventSource 订阅此端点来接收实时进度。
    """
    def event_stream():
        q = progress_queues.get(task_id)
        if not q:
            yield f"data: {json.dumps({'error': 'task not found'})}\n\n"
            return
        while True:
            try:
                data = q.get(timeout=0.1)
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                if data.get("done"):
                    break
            except queue.Empty:
                continue

    return Response(event_stream(), mimetype="text/event-stream")

# ====== 启动任务 ======
@app.route("/start", methods=["POST"])
@login_required
def start():
    """
    接受 JSON: {"urls": "url1\nurl2\nurl3"} ，返回 {"task_id": "..."}。
    """
    payload = request.get_json(force=True)
    raw = payload.get("urls", "")
    urls = [u.strip() for u in raw.splitlines() if u.strip()]
    if not urls:
        return jsonify({"error": "no urls provided"}), 400

    task_id = str(uuid.uuid4())
    q = queue.Queue()
    progress_queues[task_id] = q

    thread = threading.Thread(target=download_worker, args=(urls, q, task_id), daemon=True)
    thread.start()

    return jsonify({"task_id": task_id})

# ====== 下载最终 ZIP ======
@app.route("/download_final/<task_id>")
@login_required
def download_final(task_id):
    zip_path = os.path.join(TMP_DIR, f"{task_id}.zip")
    if not os.path.exists(zip_path):
        return "File not found", 404
    return send_file(zip_path, mimetype="application/zip", as_attachment=True, download_name="images.zip")

# ====== 主界面（内嵌前端） ======
HTML_PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>图片批量下载器（ZIP 模式）</title>
<style>
body{font-family:Arial,Helvetica,sans-serif;margin:20px;background:#f7f7f7}
.container{max-width:900px;margin:0 auto;background:#fff;padding:20px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.06)}
textarea,input{width:100%;padding:8px;margin:6px 0;border:1px solid #ddd;border-radius:6px;box-sizing:border-box}
button{padding:10px 16px;background:#007bff;color:#fff;border:none;border-radius:6px;cursor:pointer}
button:hover{background:#0069d9}
.progress{width:100%;height:18px;background:#eee;border-radius:9px;overflow:hidden;margin-top:8px}
.bar{height:100%;width:0;background:#4caf50}
.log{background:#fafafa;border:1px solid #eee;padding:10px;border-radius:6px;max-height:360px;overflow:auto}
.item{padding:6px 0;border-bottom:1px dashed #eee}
.status-ok{color:green}
.status-fail{color:#e53935}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.topbar a{font-size:14px;color:#007bff;text-decoration:none}
.topbar a:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="container">
  <div class="topbar">
    <h2>图片批量下载器（ZIP）</h2>
    <a href="/logout">退出登录</a>
  </div>
  <p><a href="https://parents.codmon.com/site/organizations" target="_blank" rel="noopener noreferrer">parents.codmon.com</a></p>
  <p>粘贴图片 URL（每行一个），点击“开始下载”。文件名自动从 URL 提取（例如 <code>A7V08093.jpg</code>）。同名文件会自动去重，只保留第一张。</p>

  <label for="urls">图片 URL（每行一个）</label>
  <textarea id="urls" rows="10" placeholder="https://image.codmon.com/codmon/13774/albums/A7V08093.jpg?Policy=..."></textarea>

  <button id="startBtn">开始下载</button>

  <div class="progress" style="margin-top:12px;">
    <div id="bar" class="bar"></div>
  </div>

  <h3 style="margin-top:14px">进度 / 日志</h3>
  <div id="log" class="log"></div>
</div>

<script>
let task_id = null;
let es = null;

function appendLog(html) {
  const log = document.getElementById("log");
  log.insertAdjacentHTML("beforeend", html + "<br>");
  log.scrollTop = log.scrollHeight;
}

function resetUI() {
  document.getElementById("bar").style.width = "0%";
  document.getElementById("log").innerHTML = "";
  if (es) {
    es.close();
    es = null;
  }
  task_id = null;
}

document.getElementById("startBtn").addEventListener("click", async function(){
  resetUI();
  const raw = document.getElementById("urls").value;
  if (!raw.trim()) { alert("请粘贴至少一个 URL"); return; }
  appendLog("🚀 任务提交中...");

  try {
    const res = await fetch("/start", {
      method: "POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({urls: raw})
    });
    if (!res.ok) {
      const text = await res.text();
      appendLog("<span class='status-fail'>提交失败: " + text + "</span>");
      return;
    }
    const data = await res.json();
    if (data.error) {
      appendLog("<span class='status-fail'>提交失败: "+data.error+"</span>");
      return;
    }
    task_id = data.task_id;
    appendLog("任务 ID: " + task_id);
  } catch (e) {
    appendLog("<span class='status-fail'>提交请求出错: "+e+"</span>");
    return;
  }

  // 订阅 SSE
  es = new EventSource("/progress/" + task_id);
  es.onmessage = function(e){
    try {
      const msg = JSON.parse(e.data);
      if (msg.error) {
        appendLog("<span class='status-fail'>错误: "+msg.error+"</span>");
        es.close();
        return;
      }
      const items = msg.items || [];
      const done = !!msg.done;
      const logDiv = document.getElementById("log");
      logDiv.innerHTML = "";
      let totalProgress = 0;
      items.forEach(function(it){
        totalProgress += (it.progress || 0);
        const cls = (it.status && it.status.startsWith("完成")) ? "status-ok" :
                    (it.status && it.status.startsWith("失败")) ? "status-fail" : "";
        logDiv.insertAdjacentHTML("beforeend",
          "<div class='item'><b>"+it.name+"</b> - <span class='"+cls+"'>"+(it.status||"")+
          "</span> ("+(it.progress||0)+"%)</div>");
      });
      if (items.length) {
        const percent = Math.floor(totalProgress / items.length);
        document.getElementById("bar").style.width = percent + "%";
      }
      if (done) {
        appendLog("✅ 所有文件已处理，准备打包并提供下载...");
        const a = document.createElement("a");
        a.href = "/download_final/" + task_id;
        a.download = "images.zip";
        document.body.appendChild(a);
        a.click();
        a.remove();
        es.close();
      }
    } catch (err) {
      console.error("SSE parse error", err);
    }
  };
  es.onerror = function() {
    appendLog("<span class='status-fail'>SSE 连接出错或被断开。</span>");
    es.close();
  };
});
</script>
</body>
</html>
"""

@app.route("/")
@login_required
def index():
    return render_template_string(HTML_PAGE)

# ====== 自动清理线程 ======
def cleanup_zip_files():
    while True:
        now = time.time()
        try:
            for fname in os.listdir(TMP_DIR):
                full = os.path.join(TMP_DIR, fname)
                if not os.path.isfile(full):
                    continue
                if now - os.path.getmtime(full) > ZIP_TTL_SECONDS:
                    try:
                        os.remove(full)
                    except Exception:
                        pass
        except Exception:
            pass
        time.sleep(CLEANUP_INTERVAL)

threading.Thread(target=cleanup_zip_files, daemon=True).start()

# ====== 启动 ======
if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "5000"))
    print(f"Starting server: http://{host}:{port}")
    app.run(host=host, port=port, threaded=True)
