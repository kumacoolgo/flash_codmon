#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
img_downloader_codmon_zip.py

Codmon 图片批量下载器：
- 登录保护：APP_USERNAME / APP_PASSWORD / SECRET_KEY
- Codmon 入口：https://parents.codmon.com/
- 支持 jpg/jpeg/png/webp/gif/bmp/heic/heif，含大写后缀
- 按最终文件名去重：同名只保留第一张，不生成 _1/_2
- 进度写入磁盘 JSON，适配 Zeabur/Gunicorn 多 worker
- 自动移除 Codmon URL 里的 forceJpg=true，避免 CloudFront 签名不匹配导致 403
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
from flask import Flask, render_template_string, request, Response, send_file, jsonify, redirect, url_for, session

TMP_DIR = os.environ.get("TMP_DIR", "tmp_zip")
PROGRESS_DIR = os.path.join(TMP_DIR, "progress")
ZIP_TTL_SECONDS = int(os.environ.get("ZIP_TTL_SECONDS", "3600"))
CLEANUP_INTERVAL = int(os.environ.get("CLEANUP_INTERVAL", "600"))
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "25"))
CHUNK_SIZE = 1024 * 32

APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "password")

os.makedirs(TMP_DIR, exist_ok=True)
os.makedirs(PROGRESS_DIR, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "CHANGE_ME_SECRET_KEY")

_FILENAME_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".heif"}
SIGNED_PARAM_NAMES = {"policy", "signature", "key-pair-id", "expires"}


def sanitize_filename(name: str, max_len: int = 200) -> str:
    name = urllib.parse.unquote(name or "file.jpg")
    name = _FILENAME_BAD.sub("_", name).strip().strip(".") or "file.jpg"
    if len(name) > max_len:
        stem, ext = os.path.splitext(name)
        name = stem[: max(1, max_len - len(ext))] + ext
    return name


def extract_filename_from_url(url: str) -> str:
    try:
        parsed = urllib.parse.urlparse((url or "").strip())
        basename = os.path.basename(parsed.path) or "file.jpg"
        basename = urllib.parse.unquote(basename)
        stem, ext = os.path.splitext(basename)
        if not stem:
            basename = "file.jpg"
        elif ext.lower() not in IMAGE_EXTS:
            basename += ".jpg"
        return sanitize_filename(basename)
    except Exception:
        return "file.jpg"


def strip_query_param_preserve_url(url: str, param_name: str) -> str:
    """删除一个 query 参数，但其它参数原样保留，避免破坏 Policy/Signature。"""
    try:
        parsed = urllib.parse.urlsplit((url or "").strip())
        if not parsed.query:
            return url
        target = param_name.lower()
        kept = []
        for part in parsed.query.split("&"):
            key = part.split("=", 1)[0]
            if urllib.parse.unquote_plus(key).lower() == target:
                continue
            kept.append(part)
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "&".join(kept), parsed.fragment))
    except Exception:
        return url


def normalize_download_url(url: str) -> str:
    """
    关键修复：Codmon 有些 URL 末尾带 forceJpg=true。
    但签名 Policy 里通常只允许 width/height，保留 forceJpg 会导致 image.codmon.com 返回 403。
    所以下载时移除 forceJpg，其它签名参数完全保留原样。
    """
    fixed = (url or "").strip()
    try:
        parsed = urllib.parse.urlparse(fixed)
        if parsed.netloc.lower().endswith("image.codmon.com"):
            fixed = strip_query_param_preserve_url(fixed, "forceJpg")
    except Exception:
        pass
    return fixed


def is_signed_codmon_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        qs_names = {p.split("=", 1)[0].lower() for p in parsed.query.split("&") if p}
        return parsed.netloc.lower().endswith("image.codmon.com") and bool(qs_names & SIGNED_PARAM_NAMES)
    except Exception:
        return False


def dedupe_urls_by_filename(urls):
    seen = set()
    unique = []
    skipped = []
    for url in urls:
        name = extract_filename_from_url(url)
        key = name.lower()
        if key in seen:
            skipped.append(name)
            continue
        seen.add(key)
        unique.append(url)
    return unique, skipped


def progress_path(task_id: str) -> str:
    return os.path.join(PROGRESS_DIR, f"{task_id}.json")


def save_progress(task_id: str, data: dict):
    path = progress_path(task_id)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def load_progress(task_id: str):
    path = progress_path(task_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def is_logged_in() -> bool:
    return bool(session.get("logged_in"))


def login_required(func):
    from functools import wraps

    @wraps(func)
    def wrapper(*args, **kwargs):
        if not is_logged_in():
            if request.path.startswith(("/start", "/progress", "/download_final")):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login", next=request.path))
        return func(*args, **kwargs)

    return wrapper


LOGIN_PAGE = r"""
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>登录 - 图片批量下载器</title>
<style>body{font-family:Arial,Helvetica,sans-serif;margin:0;background:#f0f2f5;display:flex;justify-content:center;align-items:center;height:100vh}.box{background:#fff;padding:24px 28px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.1);width:320px}h2{text-align:center;margin-top:0}label{display:block;margin-top:8px}input{width:100%;padding:8px;margin-top:4px;box-sizing:border-box;border-radius:4px;border:1px solid #ddd}button{width:100%;margin-top:16px;padding:10px;background:#007bff;color:#fff;border:none;border-radius:4px;cursor:pointer}.error{color:#e53935;font-size:13px;margin-top:8px;text-align:center}.tip{font-size:12px;color:#888;margin-top:12px;text-align:center}</style></head>
<body><div class="box"><h2>图片批量下载器</h2><form method="post" action="/login"><label>账号</label><input name="username" required autocomplete="username"><label>密码</label><input type="password" name="password" required autocomplete="current-password">{% if error %}<div class="error">{{ error }}</div>{% endif %}<button type="submit">登录</button>{% if next_path %}<input type="hidden" name="next" value="{{ next_path }}">{% endif %}</form><div class="tip">账号密码由环境变量配置。</div></div></body></html>
"""


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    next_path = request.args.get("next") or request.form.get("next") or "/"
    if request.method == "POST":
        if request.form.get("username", "").strip() == APP_USERNAME and request.form.get("password", "").strip() == APP_PASSWORD:
            session["logged_in"] = True
            return redirect(next_path or "/")
        error = "账号或密码错误"
    return render_template_string(LOGIN_PAGE, error=error, next_path=next_path)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def short_error(e: Exception) -> str:
    text = str(e)
    return text if len(text) <= 220 else text[:220] + " ..."


def download_worker(urls, task_id: str, skipped_names=None):
    skipped_names = skipped_names or []
    items = []
    zip_buffer = BytesIO()
    success_count = 0

    def push(done=False, message=""):
        save_progress(task_id, {
            "items": items.copy(),
            "done": done,
            "message": message,
            "skipped_count": len(skipped_names),
            "skipped_names": skipped_names[:50],
            "success_count": success_count,
            "updated_at": time.time(),
        })

    push(False, f"任务已开始。按文件名去重，跳过 {len(skipped_names)} 个重复链接。")

    with ZipFile(zip_buffer, "w") as zf:
        for url in urls:
            filename = extract_filename_from_url(url)
            item = {"name": filename, "status": "下载中", "progress": 0}
            items.append(item)
            push(False)

            try:
                download_url = normalize_download_url(url)
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
                    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                    "Referer": "https://parents.codmon.com/",
                    "Origin": "https://parents.codmon.com",
                }
                with requests.get(download_url, stream=True, timeout=DOWNLOAD_TIMEOUT, headers=headers, allow_redirects=True) as r:
                    if r.status_code == 403 and "forceJpg" in url:
                        item["status"] = "失败: 403 Forbidden。已自动移除 forceJpg=true 后仍失败，通常是链接已过期或签名无效，请重新从 Codmon 复制最新图片链接。"
                        item["progress"] = 100
                        push(False)
                        continue
                    r.raise_for_status()
                    total = int(r.headers.get("content-length") or 0)
                    chunks = []
                    downloaded = 0
                    for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                        if not chunk:
                            continue
                        chunks.append(chunk)
                        downloaded += len(chunk)
                        item["progress"] = min(99, int(downloaded * 100 / total)) if total else 50
                        push(False)
                    content = b"".join(chunks)

                if not content:
                    item["status"] = "失败: 空文件"
                    item["progress"] = 100
                    push(False)
                    continue

                zf.writestr(filename, content)
                success_count += 1
                item["status"] = "完成"
                item["progress"] = 100
                push(False)

            except requests.exceptions.HTTPError as e:
                if getattr(e.response, "status_code", None) == 403:
                    if is_signed_codmon_url(url):
                        item["status"] = "失败: 403 Forbidden。Codmon 签名链接可能已过期/无效，请重新打开 Codmon 页面复制最新链接。"
                    else:
                        item["status"] = "失败: 403 Forbidden。该链接可能需要登录权限或 Cookie。"
                else:
                    item["status"] = "失败: " + short_error(e)
                item["progress"] = 100
                push(False)
            except requests.exceptions.RequestException as e:
                item["status"] = "失败: " + short_error(e)
                item["progress"] = 100
                push(False)
            except Exception as e:
                item["status"] = "失败: " + short_error(e)
                item["progress"] = 100
                push(False)

    zip_path = os.path.join(TMP_DIR, f"{task_id}.zip")
    zip_buffer.seek(0)
    with open(zip_path, "wb") as f:
        f.write(zip_buffer.read())

    if success_count:
        push(True, f"处理完成。成功 {success_count} 张，跳过重复 {len(skipped_names)} 个。")
    else:
        push(True, "处理完成，但没有成功下载的图片。请重新从 Codmon 页面复制最新图片链接后再试。")


@app.route("/progress/<task_id>")
@login_required
def progress_stream(task_id):
    def event_stream():
        last = None
        not_found = 0
        while True:
            data = load_progress(task_id)
            if data is None:
                not_found += 1
                if not_found > 50:
                    yield f"data: {json.dumps({'error': 'task not found'}, ensure_ascii=False)}\n\n"
                    return
                time.sleep(0.1)
                continue
            payload = json.dumps(data, ensure_ascii=False)
            if payload != last:
                yield f"data: {payload}\n\n"
                last = payload
            if data.get("done"):
                break
            time.sleep(0.3)
    return Response(event_stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
    save_progress(task_id, {"items": [], "done": False, "message": "任务已创建。", "skipped_count": len(skipped_names), "success_count": 0, "updated_at": time.time()})
    threading.Thread(target=download_worker, args=(unique_urls, task_id, skipped_names), daemon=True).start()
    return jsonify({"task_id": task_id, "skipped_count": len(skipped_names)})


@app.route("/download_final/<task_id>")
@login_required
def download_final(task_id):
    zip_path = os.path.join(TMP_DIR, f"{task_id}.zip")
    if not os.path.exists(zip_path):
        return "File not found", 404
    return send_file(zip_path, mimetype="application/zip", as_attachment=True, download_name="codmon_images.zip")


HTML_PAGE = r"""
<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>图片批量下载器（ZIP 模式）</title>
<style>body{font-family:Arial,Helvetica,sans-serif;margin:20px;background:#f7f7f7}.container{max-width:900px;margin:0 auto;background:#fff;padding:20px;border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,.06)}textarea,input{width:100%;padding:8px;margin:6px 0;border:1px solid #ddd;border-radius:6px;box-sizing:border-box}button{padding:10px 16px;background:#007bff;color:#fff;border:none;border-radius:6px;cursor:pointer}.progress{width:100%;height:18px;background:#eee;border-radius:9px;overflow:hidden;margin-top:8px}.bar{height:100%;width:0;background:#4caf50}.log{background:#fafafa;border:1px solid #eee;padding:10px;border-radius:6px;max-height:360px;overflow:auto}.item{padding:6px 0;border-bottom:1px dashed #eee;word-break:break-all}.status-ok{color:green}.status-fail{color:#e53935}.topbar{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}.topbar a{font-size:14px;color:#007bff;text-decoration:none}.small{font-size:13px;color:#666}</style></head>
<body><div class="container"><div class="topbar"><h2>图片批量下载器（ZIP）</h2><a href="/logout">退出登录</a></div>
<p><a href="https://parents.codmon.com/" target="_blank" rel="noopener noreferrer">parents.codmon.com</a></p>
<p>粘贴图片 URL（每行一个），点击“开始下载”。文件名自动从 URL 提取，例如 <code>A7V08093.jpg</code>、<code>128361306-IMG_6495.jpeg</code>。同名会自动去重，只保留第一张。</p>
<p class="small">已自动处理 Codmon 链接里的 <code>forceJpg=true</code>，可减少 403 Forbidden。</p>
<label for="urls">图片 URL（每行一个）</label><textarea id="urls" rows="10" placeholder="https://image.codmon.com/codmon/13774/albums/A7V08093.jpg?Policy=..."></textarea>
<button id="startBtn">开始下载</button><div class="progress"><div id="bar" class="bar"></div></div><h3>进度 / 日志</h3><div id="log" class="log"></div></div>
<script>
let task_id=null, es=null;
function escapeHtml(s){return String(s||"").replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));}
function appendLog(html){const log=document.getElementById('log');log.insertAdjacentHTML('beforeend',html+'<br>');log.scrollTop=log.scrollHeight;}
function resetUI(){document.getElementById('bar').style.width='0%';document.getElementById('log').innerHTML='';if(es){es.close();es=null;}task_id=null;}
document.getElementById('startBtn').addEventListener('click',async function(){
  resetUI(); const raw=document.getElementById('urls').value; if(!raw.trim()){alert('请粘贴至少一个 URL');return;} appendLog('🚀 任务提交中...');
  try{const res=await fetch('/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({urls:raw})}); if(!res.ok){appendLog("<span class='status-fail'>提交失败: "+escapeHtml(await res.text())+'</span>');return;} const data=await res.json(); if(data.error){appendLog("<span class='status-fail'>提交失败: "+escapeHtml(data.error)+'</span>');return;} task_id=data.task_id; appendLog('任务 ID: '+escapeHtml(task_id)); if(data.skipped_count) appendLog('♻️ 已跳过重复链接：'+data.skipped_count+' 个');}catch(e){appendLog("<span class='status-fail'>提交请求出错: "+escapeHtml(e)+'</span>');return;}
  es=new EventSource('/progress/'+task_id);
  es.onmessage=function(e){try{const msg=JSON.parse(e.data); if(msg.error){appendLog("<span class='status-fail'>错误: "+escapeHtml(msg.error)+'</span>');es.close();return;} const items=msg.items||[], done=!!msg.done, logDiv=document.getElementById('log'); logDiv.innerHTML=''; if(msg.message) logDiv.insertAdjacentHTML('beforeend',"<div class='item small'>"+escapeHtml(msg.message)+'</div>'); if(msg.skipped_count) logDiv.insertAdjacentHTML('beforeend',"<div class='item small'>♻️ 去重跳过："+msg.skipped_count+' 个</div>'); let total=0; items.forEach(it=>{total+=(it.progress||0); const cls=(it.status||'').startsWith('完成')?'status-ok':((it.status||'').startsWith('失败')?'status-fail':''); logDiv.insertAdjacentHTML('beforeend',"<div class='item'><b>"+escapeHtml(it.name)+"</b> - <span class='"+cls+"'>"+escapeHtml(it.status||'')+'</span> ('+(it.progress||0)+'%)</div>');}); if(items.length) document.getElementById('bar').style.width=Math.floor(total/items.length)+'%'; if(done){if((msg.success_count||0)>0){appendLog('✅ 有文件下载成功，准备下载 ZIP...'); const a=document.createElement('a'); a.href='/download_final/'+task_id; a.download='codmon_images.zip'; document.body.appendChild(a); a.click(); a.remove();} else {appendLog("<span class='status-fail'>没有成功文件，不自动下载空 ZIP。</span>");} es.close();}}catch(err){console.error(err);}};
  es.onerror=function(){appendLog("<span class='status-fail'>SSE 连接出错或被断开。</span>");es.close();};
});
</script></body></html>
"""


@app.route("/")
@login_required
def index():
    return render_template_string(HTML_PAGE)


def cleanup_files():
    while True:
        now = time.time()
        for folder in (TMP_DIR, PROGRESS_DIR):
            try:
                for fname in os.listdir(folder):
                    full = os.path.join(folder, fname)
                    if os.path.isfile(full) and now - os.path.getmtime(full) > ZIP_TTL_SECONDS:
                        try:
                            os.remove(full)
                        except Exception:
                            pass
            except Exception:
                pass
        time.sleep(CLEANUP_INTERVAL)


threading.Thread(target=cleanup_files, daemon=True).start()

if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", "5000"))
    print(f"Starting server: http://{host}:{port}")
    app.run(host=host, port=port, threaded=True)
