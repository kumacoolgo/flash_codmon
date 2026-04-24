#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
img_downloader_codmon_zip.py

说明:
- 访问 /login 登录后，才能使用下载页面（/）。
- 账号密码从环境变量读取：APP_USERNAME / APP_PASSWORD。
- 适配 Codmon 图片链接：https://image.codmon.com/...
- 文件名从 URL path 最后一段提取，支持 .jpg / .jpeg / .png / .webp / .gif / .bmp / .heic / .heif。
- 按最终文件名去重：同名只保留第一张，不生成 _1、_2。
- 进度写入磁盘 JSON，避免 Zeabur/Gunicorn 多 worker 时 SSE 出现 task not found。
"""

import os
import re
import time
import uuid
import json
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
TMP_DIR = os.environ.get("TMP_DIR", "tmp_zip")
PROGRESS_DIR = os.path.join(TMP_DIR, "progress")
ZIP_TTL_SECONDS = int(os.environ.get("ZIP_TTL_SECONDS", "3600"))
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "600"))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "20"))
CHUNK_SIZE = 1024

APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "password")

os.makedirs(TMP_DIR, exist_ok=True)
os.makedirs(PROGRESS_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_ME_SECRET_KEY")

# ====== 文件名处理 ======
# 只替换路径分隔符、控制字符和 Windows 不允许的字符；保留中文/日文。
_filename_sanitize_re = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
SUPPORTED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif"}


def sanitize_filename(name: str, max_len: int = 200) -> str:
    if not name:
        name = "file.jpg"
    name = urllib.parse.unquote(name)
    name = _filename_sanitize_re.sub("_", name).strip().strip(".")
    if not name:
        name = "file.jpg"
    if len(name) > max_len:
        stem, ext = os.path.splitext(name)
        keep = max_len - len(ext)
        name = stem[:max(1, keep)] + ext
    return name


def extract_filename_from_url(url: str) -> str:
    """
    从 Codmon 图片 URL 提取最终文件名。

    例：
    https://image.codmon.com/codmon/13774/albums/A7V08093.jpg?Policy=...
    -> A7V08093.jpg

    https://image.codmon.com/codmon/13774/documentations/113598/128361306-IMG_6495.jpeg?Policy=...
    -> 128361306-IMG_6495.jpeg
    """
    try:
        raw_url = (url or "").strip()
        parsed = urllib.parse.urlparse(raw_url)
        basename = os.path.basename(parsed.path) or ""

        if not basename:
            clean = raw_url.split("?", 1)[0]
            basename = clean.rstrip("/").split("/")[-1]

        basename = urllib.parse.unquote(basename or "file.jpg")
        stem, ext = os.path.splitext(basename)

        if not stem:
            basename = "file.jpg"
        elif ext.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            basename = basename + ".jpg"

        return sanitize_filename(basename)
    except Exception:
        return "file.jpg"


def dedupe_urls_by_filename(urls):
    """按最终文件名去重：同名只保留第一次出现的 URL。"""
    seen = set()
    unique_urls = []
    skipped_names = []

    for url in urls:
        filename = extract_filename_from_url(url)
        key = filename.lower()
        if key in seen:
            skipped_names.append(filename)
            continue
        seen.add(key)
        unique_urls.append(url)

    return unique_urls, skipped_names


# ====== 磁盘进度 ======
def progress_file_path(task_id: str) -> str:
    return os.path.join(PROGRESS_DIR, f"{task_id}.json")


def save_progress(task_id: str, data: dict) -> None:
    path = progress_file_path(task_id)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, path)


def load_progress(task_id: str):
    path = progress_file_path(task_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ====== 登录相关 ======
def is_logged_in() -> bool:
    return bool(session.get("logged_in"))


def login_required(view_func):
    from functools import wraps

    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            if request.path.startswith(("/start", "/progress", "/download_final")):
                return jsonify({"error": "unauthorized"}), 401
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
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    <button type="submit">登录</button>
    {% if next_path %}<input type="hidden" name="next" value="{{ next_path }}">{% endif %}
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
        error = "账号或密码错误"
    return render_template_string(LOGIN_PAGE, error=error, next_path=next_path)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ====== 下载工作线程 ======
def download_worker(urls, task_id: str, skipped_names=None):
    skipped_names = skipped_names or []
    items = []
    zip_buffer = BytesIO()

    def push(done=False, message=""):
        save_progress(task_id, {
            "items": items.copy(),
            "done": done,
            "message": message,
            "skipped_count": len(skipped_names),
            "skipped_names": skipped_names[:50],
            "updated_at": time.time(),
        })

    first_msg = f"已按文件名去重，跳过 {len(skipped_names)} 个重复链接。" if skipped_names else "任务已开始。"
    push(False, first_msg)

    with ZipFile(zip_buffer, "w") as zf:
        for url in urls:
            filename = extract_filename_from_url(url)
            item = {"name": filename, "status": "下载中", "progress": 0}
            items.append(item)
            push(False)

            try:
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                    "Referer": "https://parents.codmon.com/",
                }
                with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT, headers=headers) as r:
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
                            item["progress"] = min(99, int(downloaded * 100 / total))
                        else:
                            item["progress"] = 50
                        push(False)
                    content_bytes = b"".join(chunks)

                zf.writestr(filename, content_bytes)
                item["status"] = "完成"
                item["progress"] = 100
                push(False)

            except requests.exceptions.RequestException as e:
                item["status"] = f"失败: {str(e)}"
                item["progress"] = 100
                push(False)
            except Exception as e:
                item["status"] = f"失败: {str(e)}"
                item["progress"] = 100
                push(False)

    zip_buffer.seek(0)
    zip_path = os.path.join(TMP_DIR, f"{task_id}.zip")
    with open(zip_path, "wb") as f:
        f.write(zip_buffer.read())

    final_msg = f"处理完成。已跳过 {len(skipped_names)} 个重复链接。" if skipped_names else "处理完成。"
    push(True, final_msg)


# ====== SSE 进度流 ======
@app.route("/progress/<task_id>")
@login_required
def progress_stream(task_id):
    def event_stream():
        last_payload = None
        not_found_wait = 0
        while True:
            data = load_progress(task_id)
            if data is None:
                not_found_wait += 1
                if not_found_wait > 50:
                    yield f"data: {json.dumps({'error': 'task not found'}, ensure_ascii=False)}\n\n"
                    return
                time.sleep(0.1)
                continue

            payload = json.dumps(data, ensure_ascii=False)
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload

            if data.get("done"):
                break
            time.sleep(0.3)

    return Response(event_stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ====== 启动任务 ======
@app.route("/start", methods=["POST"])
@login_required
def start():
    payload = request.get_json(force=True)
    raw = payload.get("urls", "")
    urls = [u.strip() for u in raw.splitlines() if u.strip()]
    if not urls:
        return jsonify({"error": "no urls provided"}), 400

    unique_urls, skipped_names = dedupe_urls_by_filename(urls)
    if not unique_urls:
        return jsonify({"error": "all urls were duplicates or invalid"}), 400

    task_id = str(uuid.uuid4())
    save_progress(task_id, {
        "items": [],
        "done": False,
        "message": f"已按文件名去重，跳过 {len(skipped_names)} 个重复链接。" if skipped_names else "任务已创建。",
        "skipped_count": len(skipped_names),
        "skipped_names": skipped_names[:50],
        "updated_at": time.time(),
    })

    thread = threading.Thread(target=download_worker, args=(unique_urls, task_id, skipped_names), daemon=True)
    thread.start()

    return jsonify({"task_id": task_id, "skipped_count": len(skipped_names)})


# ====== 下载最终 ZIP ======
@app.route("/download_final/<task_id>")
@login_required
def download_final(task_id):
    zip_path = os.path.join(TMP_DIR, f"{task_id}.zip")
    if not os.path.exists(zip_path):
        return "File not found", 404
    return send_file(zip_path, mimetype="application/zip", as_attachment=True, download_name="codmon_images.zip")


# ====== 主界面 ======
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
button:hover{background:#0069d9}.progress{width:100%;height:18px;background:#eee;border-radius:9px;overflow:hidden;margin-top:8px}.bar{height:100%;width:0;background:#4caf50}
.log{background:#fafafa;border:1px solid #eee;padding:10px;border-radius:6px;max-height:360px;overflow:auto}.item{padding:6px 0;border-bottom:1px dashed #eee}.status-ok{color:green}.status-fail{color:#e53935}
.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}.topbar a{font-size:14px;color:#007bff;text-decoration:none}.topbar a:hover{text-decoration:underline}
.small{font-size:13px;color:#666}
</style>
</head>
<body>
<div class="container">
  <div class="topbar"><h2>图片批量下载器（ZIP）</h2><a href="/logout">退出登录</a></div>
  <p><a href="https://parents.codmon.com/" target="_blank" rel="noopener noreferrer">parents.codmon.com</a></p>
  <p>粘贴图片 URL（每行一个），点击“开始下载”。文件名自动从 URL 提取，例如 <code>A7V08093.jpg</code>、<code>128361306-IMG_6495.jpeg</code>。同名会自动去重，只保留第一张。</p>

  <label for="urls">图片 URL（每行一个）</label>
  <textarea id="urls" rows="10" placeholder="https://image.codmon.com/codmon/13774/albums/A7V08093.jpg?Policy=...
https://image.codmon.com/codmon/13774/documentations/113598/128361306-IMG_6495.jpeg?Policy=..."></textarea>

  <button id="startBtn">开始下载</button>
  <div class="progress" style="margin-top:12px;"><div id="bar" class="bar"></div></div>
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
  if (es) { es.close(); es = null; }
  task_id = null;
}

function escapeHtml(s) {
  return String(s || "").replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
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
      appendLog("<span class='status-fail'>提交失败: " + escapeHtml(text) + "</span>");
      return;
    }
    const data = await res.json();
    if (data.error) {
      appendLog("<span class='status-fail'>提交失败: "+escapeHtml(data.error)+"</span>");
      return;
    }
    task_id = data.task_id;
    appendLog("任务 ID: " + escapeHtml(task_id));
    if (data.skipped_count) appendLog("♻️ 已按文件名去重，跳过 " + data.skipped_count + " 个重复链接");
  } catch (e) {
    appendLog("<span class='status-fail'>提交请求出错: "+escapeHtml(e)+"</span>");
    return;
  }

  es = new EventSource("/progress/" + task_id);
  es.onmessage = function(e){
    try {
      const msg = JSON.parse(e.data);
      if (msg.error) {
        appendLog("<span class='status-fail'>错误: "+escapeHtml(msg.error)+"</span>");
        es.close();
        return;
      }
      const items = msg.items || [];
      const done = !!msg.done;
      const logDiv = document.getElementById("log");
      logDiv.innerHTML = "";

      if (msg.message) logDiv.insertAdjacentHTML("beforeend", "<div class='item small'>" + escapeHtml(msg.message) + "</div>");
      if (msg.skipped_count) logDiv.insertAdjacentHTML("beforeend", "<div class='item small'>♻️ 去重跳过：" + msg.skipped_count + " 个重复文件名</div>");

      let totalProgress = 0;
      items.forEach(function(it){
        totalProgress += (it.progress || 0);
        const cls = (it.status && it.status.startsWith("完成")) ? "status-ok" :
                    (it.status && it.status.startsWith("失败")) ? "status-fail" : "";
        logDiv.insertAdjacentHTML("beforeend",
          "<div class='item'><b>"+escapeHtml(it.name)+"</b> - <span class='"+cls+"'>"+escapeHtml(it.status||"")+
          "</span> ("+(it.progress||0)+"%)</div>");
      });
      if (items.length) {
        const percent = Math.floor(totalProgress / items.length);
        document.getElementById("bar").style.width = percent + "%";
      }
      if (done) {
        appendLog("✅ 所有文件已处理，准备下载 ZIP...");
        const a = document.createElement("a");
        a.href = "/download_final/" + task_id;
        a.download = "codmon_images.zip";
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
            for folder in (TMP_DIR, PROGRESS_DIR):
                if not os.path.isdir(folder):
                    continue
                for fname in os.listdir(folder):
                    full = os.path.join(folder, fname)
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


if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "5000"))
    print(f"Starting server: http://{host}:{port}")
    app.run(host=host, port=port, threaded=True)
