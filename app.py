import os
import sys
import socket
import threading
import webbrowser
import time
import logging
import collections
import json
import argparse
import random
import subprocess
import platform
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, send_from_directory, jsonify, abort

# --- 新增依赖 ---
from pystray import Icon, MenuItem as item, Menu
from PIL import Image, ImageDraw

# ================= 项目打包辅助 =================
def resource_path(relative_path):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.abspath("."), relative_path)

# ================= 配置加载逻辑 =================
DEFAULT_CONFIG = {
    "port": 8000,
    "release_dir": "release",
    "received_dir": "received"
}

def load_config():
    parser = argparse.ArgumentParser(description="PondCast - 局域网文件池")
    parser.add_argument('--port', type=int, help='指定服务端口')
    parser.add_argument('--config', type=str, default='config.json', help='指定配置文件路径')
    args = parser.parse_args()

    config = DEFAULT_CONFIG.copy()
    if os.path.exists(args.config):
        try:
            with open(args.config, 'r', encoding='utf-8') as f:
                config.update(json.load(f))
        except: pass

    if args.port: config['port'] = args.port
    return config

CONFIG = load_config()
RELEASE_DIR = CONFIG['release_dir']
RECEIVED_DIR = CONFIG['received_dir']
MAX_EVENTS = 50 

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

# 全局状态
SERVER_STATE = {
    'locked': False,
    'file_pool': False,
    'current_port': CONFIG['port']
}

ACTIVE_PEERS = {}
PEER_TIMEOUT = 8 
EVENT_LOG = collections.deque(maxlen=MAX_EVENTS)
LAST_ONLINE_IPS = set()

# ================= 核心工具 =================

def ensure_directories():
    if not os.path.exists(RELEASE_DIR): os.makedirs(RELEASE_DIR)
    if not os.path.exists(RECEIVED_DIR): os.makedirs(RECEIVED_DIR)

def get_local_ips():
    ip_list = []
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            if not ip.startswith("127."): ip_list.append(ip)
    except:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip_list.append(s.getsockname()[0])
            s.close()
        except: pass
    return ip_list

def is_port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0

def find_available_port(start_port):
    if not is_port_in_use(start_port): return start_port
    for _ in range(100):
        random_port = random.randint(20000, 60000)
        if not is_port_in_use(random_port): return random_port
    raise RuntimeError("无法找到可用端口")

def is_local_admin():
    return request.remote_addr == '127.0.0.1' or request.remote_addr == 'localhost'

def get_masked_name(filename):
    if not filename: return "***"
    try:
        name, ext = os.path.splitext(filename)
        if len(name) <= 2: return name + "***" + ext
        return name[:2] + "***" + ext
    except: return "***"

def add_event(type, msg, ip=None, filename=None):
    EVENT_LOG.appendleft({
        'id': int(time.time() * 1000000), 
        'time': datetime.now().strftime("%H:%M:%S"),
        'type': type, 'msg': msg, 'ip': ip, 'filename': filename
    })

def record_activity(ip, action=None, device_type=None):
    now = datetime.now()
    if not device_type: device_type = "desktop"
    if ip not in ACTIVE_PEERS:
        ACTIVE_PEERS[ip] = {'last_seen': now, 'action': 'idle', 'action_time': now, 'device_type': device_type}
    else:
        ACTIVE_PEERS[ip]['last_seen'] = now
        if action:
            ACTIVE_PEERS[ip]['action'] = action
            ACTIVE_PEERS[ip]['action_time'] = now + timedelta(seconds=3)
        if device_type: ACTIVE_PEERS[ip]['device_type'] = device_type

def check_peers_lifecycle():
    global LAST_ONLINE_IPS
    now = datetime.now()
    to_remove = []
    current_ips = set()
    for ip, data in ACTIVE_PEERS.items():
        if data['action'] != 'idle' and now > data['action_time']: data['action'] = 'idle'
        if now - data['last_seen'] > timedelta(seconds=PEER_TIMEOUT): to_remove.append(ip)
        else: current_ips.add(ip)
    for ip in to_remove:
        del ACTIVE_PEERS[ip]
        add_event('leave', '已离线', ip)
    new_ips = current_ips - LAST_ONLINE_IPS
    for ip in new_ips:
        if ip != '127.0.0.1': add_event('join', '加入网络', ip)
    LAST_ONLINE_IPS = current_ips

# ================= 系统操作工具 =================

def open_local_folder(path):
    """跨平台打开文件夹"""
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path): os.makedirs(abs_path)
    
    system_name = platform.system()
    try:
        if system_name == 'Windows':
            os.startfile(abs_path)
        elif system_name == 'Darwin': # macOS
            subprocess.call(['open', abs_path])
        else: # Linux
            subprocess.call(['xdg-open', abs_path])
    except Exception as e:
        print(f"打开文件夹失败: {e}")

# ================= Web 路由 =================

@app.before_request
def global_intercept():
    if SERVER_STATE['locked'] and not is_local_admin():
        if request.endpoint != 'static': abort(403, description="Maintenance Mode")
    sender_ip = request.remote_addr
    if sender_ip != '127.0.0.1' and sender_ip != 'localhost':
        if request.path.startswith('/api/') or request.path.startswith('/upload') or request.path.startswith('/download'):
            record_activity(sender_ip)

@app.route('/')
def index():
    template_path = resource_path('index.html')
    if not os.path.exists(template_path): return "Error: index.html not found", 404
    with open(template_path, 'r', encoding='utf-8') as f: html_content = f.read()
    return render_template_string(html_content, is_admin=is_local_admin(), local_ips=get_local_ips(), port=SERVER_STATE['current_port'])

@app.route('/api/status', methods=['GET'])
def api_status():
    check_peers_lifecycle()
    sender_ip = request.remote_addr
    is_admin = is_local_admin()
    topology = []
    if SERVER_STATE['file_pool']: topology.append({'ip': 'Server', 'status': 'idle', 'type': 'server', 'device_type': 'desktop'})
    for ip, data in ACTIVE_PEERS.items():
        topology.append({'ip': ip, 'status': data['action'], 'type': 'client', 'device_type': data.get('device_type', 'desktop')})
    
    raw_events = list(EVENT_LOG)
    client_safe_events = []
    should_mask = (not is_admin) and (not SERVER_STATE['file_pool'])
    for event in raw_events:
        evt_copy = event.copy()
        if should_mask and event['ip'] != sender_ip and event.get('filename'):
            evt_copy['msg'] = evt_copy['msg'].replace(event['filename'], get_masked_name(event['filename']))
            if 'filename' in evt_copy: del evt_copy['filename']
        client_safe_events.append(evt_copy)
    
    return jsonify({'locked': SERVER_STATE['locked'], 'file_pool': SERVER_STATE['file_pool'], 'ips': get_local_ips(), 'port': SERVER_STATE['current_port'], 'topology': topology, 'events': client_safe_events})

@app.route('/api/toggle_lock', methods=['POST'])
def api_toggle_lock():
    if not is_local_admin(): return jsonify({'error': '403'}), 403
    SERVER_STATE['locked'] = not SERVER_STATE['locked']
    add_event('system', '服务器锁定状态已变更', 'Server')
    return jsonify({'locked': SERVER_STATE['locked']})

@app.route('/api/toggle_pool', methods=['POST'])
def api_toggle_pool():
    if not is_local_admin(): return jsonify({'error': '403'}), 403
    SERVER_STATE['file_pool'] = not SERVER_STATE['file_pool']
    add_event('pool_toggle', f'文件池模式已{"启用" if SERVER_STATE["file_pool"] else "关闭"}', 'Server')
    return jsonify({'file_pool': SERVER_STATE['file_pool']})

@app.route('/api/shutdown', methods=['POST'])
def api_shutdown():
    """网页端的关闭入口，配合系统托盘"""
    if not is_local_admin(): return jsonify({'error': '403'}), 403
    # 这里我们只关闭 Flask，主线程的图标会随之处理
    func = request.environ.get('werkzeug.server.shutdown')
    if func: func()
    # 同时通知主线程退出
    os._exit(0)
    return jsonify({'success': True})

@app.route('/api/files/release', methods=['GET'])
def list_release_files():
    files = []
    if os.path.exists(RELEASE_DIR):
        for f in os.listdir(RELEASE_DIR):
            path = os.path.join(RELEASE_DIR, f)
            if os.path.isfile(path): files.append({'name': f, 'size': os.path.getsize(path), 'type': 'release'})
    return jsonify(files)

@app.route('/api/files/received', methods=['GET'])
def list_received_files():
    sender_ip = request.remote_addr
    is_admin = is_local_admin()
    if is_admin or SERVER_STATE['file_pool']:
        structure = {}
        if SERVER_STATE['file_pool'] and os.path.exists(RELEASE_DIR):
            structure['Server'] = [{'name': f, 'size': os.path.getsize(os.path.join(RELEASE_DIR, f)), 'path_key': f"__release__/{f}", 'is_server_file': True} for f in os.listdir(RELEASE_DIR) if os.path.isfile(os.path.join(RELEASE_DIR, f))]
        if os.path.exists(RECEIVED_DIR):
            for ip_dir in os.listdir(RECEIVED_DIR):
                ip_path = os.path.join(RECEIVED_DIR, ip_dir)
                if os.path.isdir(ip_path):
                    files = [{'name': f, 'size': os.path.getsize(os.path.join(ip_path, f)), 'path_key': f"{ip_dir}/{f}", 'upload_time': os.path.getmtime(os.path.join(ip_path, f))} for f in os.listdir(ip_path) if os.path.isfile(os.path.join(ip_path, f))]
                    if files: structure[ip_dir] = sorted(files, key=lambda x: x['upload_time'], reverse=True)
        return jsonify({'role': 'pool_view' if not is_admin else 'admin', 'data': structure, 'my_ip': sender_ip, 'pool_enabled': SERVER_STATE['file_pool']})
    else:
        my_dir = os.path.join(RECEIVED_DIR, sender_ip)
        files = []
        if os.path.exists(my_dir):
            files = [{'name': f, 'size': os.path.getsize(os.path.join(my_dir, f)), 'upload_time': os.path.getmtime(os.path.join(my_dir, f))} for f in os.listdir(my_dir) if os.path.isfile(os.path.join(my_dir, f))]
        return jsonify({'role': 'client', 'data': sorted(files, key=lambda x: x['upload_time'], reverse=True), 'pool_enabled': False})

@app.route('/api/file/delete', methods=['POST'])
def delete_file():
    if not is_local_admin(): return jsonify({'error': '403'}), 403
    data = request.json
    t_type, t_path = data.get('type'), data.get('path')
    full_path, fname = None, os.path.basename(t_path) if t_path else "file"
    if t_type == 'release' or (t_path and t_path.startswith('__release__/')):
        full_path = os.path.join(RELEASE_DIR, t_path.replace('__release__/', '') if t_path else '')
    elif t_type == 'received':
        full_path = os.path.join(RECEIVED_DIR, t_path)
    
    if full_path and os.path.exists(full_path):
        try:
            os.remove(full_path)
            if t_type == 'received':
                parent = os.path.dirname(full_path)
                if not os.listdir(parent): os.rmdir(parent)
            add_event('delete', f'删除了 {fname}', 'Server', filename=fname)
            return jsonify({'success': True})
        except Exception as e: return jsonify({'error': str(e)}), 500
    return jsonify({'error': '404'}), 404

@app.route('/download/<path:filename>')
def download_file(filename):
    if '..' in filename: abort(404)
    sender_ip = request.remote_addr
    if sender_ip != '127.0.0.1': record_activity(sender_ip, 'download')
    
    if filename.startswith('__release__/'):
        return send_from_directory(RELEASE_DIR, filename.replace('__release__/', ''), as_attachment=True)
    if os.path.exists(os.path.join(RELEASE_DIR, filename)):
         return send_from_directory(RELEASE_DIR, filename, as_attachment=True)
    
    if is_local_admin() or SERVER_STATE['file_pool']:
        parts = filename.split('/', 1)
        if len(parts) == 2:
            return send_from_directory(os.path.join(RECEIVED_DIR, parts[0]), parts[1], as_attachment=True)
    abort(404)

@app.route('/upload', methods=['POST'])
def upload_file():
    uploaded_files = request.files.getlist("files")
    sender_ip, is_admin = request.remote_addr, is_local_admin()
    if not is_admin: record_activity(sender_ip, 'upload')
    
    save_dir = RELEASE_DIR if is_admin else os.path.join(RECEIVED_DIR, sender_ip)
    if not os.path.exists(save_dir): os.makedirs(save_dir)

    count, last_f = 0, ""
    for file in uploaded_files:
        if file.filename:
            fn = file.filename
            fp = os.path.join(save_dir, fn)
            if os.path.exists(fp): fp = os.path.join(save_dir, datetime.now().strftime("%H%M%S_") + fn)
            file.save(fp)
            count += 1; last_f = fn

    if count > 0:
        msg = f'上传了 {count} 个文件'
        fname = last_f if count == 1 else None
        if count == 1: msg = f'上传了 {last_f}'
        add_event('upload', msg, sender_ip if not is_admin else 'Server', filename=fname)

    return jsonify({'msg': 'ok'})

# ================= 系统托盘逻辑 =================

def create_tray_icon():
    # 使用 Pillow 动态生成一个简单的图标 (青色圆角矩形)
    width, height = 64, 64
    image = Image.new('RGBA', (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # 画一个圆角矩形 (PondCast 青色)
    draw.rounded_rectangle((0, 0, width, height), radius=15, fill="#06b6d4", outline="#22d3ee", width=3)
    # 画一个 "P"
    draw.text((20, 15), "P", fill="white", font_size=40)
    return image

def run_flask_server():
    """在子线程中运行 Flask"""
    try:
        app.run(host='0.0.0.0', port=SERVER_STATE['current_port'], debug=False, use_reloader=False)
    except Exception as e:
        print(f"Server Error: {e}")

def on_open_web(icon, item):
    webbrowser.open(f'http://127.0.0.1:{SERVER_STATE["current_port"]}')

def on_open_received(icon, item):
    open_local_folder(RECEIVED_DIR)

def on_open_release(icon, item):
    open_local_folder(RELEASE_DIR)

def on_exit(icon, item):
    icon.stop()
    os._exit(0) # 强制杀掉所有线程

def setup_tray_icon():
    image = create_tray_icon()
    menu = Menu(
        item('PondCast 服务中...', lambda i,t: None, enabled=False),
        Menu.SEPARATOR,
        item('打开网页 (Web UI)', on_open_web, default=True),
        item('打开接收文件夹 (Received)', on_open_received),
        item('打开共享文件夹 (Shared)', on_open_release),
        Menu.SEPARATOR,
        item('退出 (Exit)', on_exit)
    )
    return Icon("PondCast", image, "PondCast Server", menu)

# ================= 主入口 =================

if __name__ == '__main__':
    ensure_directories()
    
    # 1. 确定端口
    final_port = find_available_port(CONFIG['port'])
    SERVER_STATE['current_port'] = final_port
    
    # 2. 启动 Flask (后台线程)
    flask_thread = threading.Thread(target=run_flask_server, daemon=True)
    flask_thread.start()
    
    # 3. 自动打开浏览器
    def initial_open():
        time.sleep(1.5)
        webbrowser.open(f'http://127.0.0.1:{final_port}')
    threading.Thread(target=initial_open, daemon=True).start()
    
    print(f"PondCast Started on Port {final_port}. Check System Tray.")
    
    # 4. 启动系统托盘 (主线程阻塞运行，必须放在最后)
    # 注意：在 macOS 上 GUI 必须在主线程运行
    tray_icon = setup_tray_icon()
    tray_icon.run()