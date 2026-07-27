import socket
import json
import urllib.request
import threading
import time
import os

UDP_PORT = int(os.getenv("UDP_PORT", 53259))

ACTIVE_HOST_IP = os.getenv("HOST_IP")
is_managed_by_hub = False
hub_url = None

def get_local_ip():
    global ACTIVE_HOST_IP
    if ACTIVE_HOST_IP:
        return ACTIVE_HOST_IP
    env_ip = os.getenv("HOST_IP")
    if env_ip:
        return env_ip
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
        return local_ip
    except Exception:
        return "127.0.0.1"

def set_active_ip(new_ip):
    global ACTIVE_HOST_IP
    if not new_ip: return
    ACTIVE_HOST_IP = new_ip
    print(f"[Wan2GP Discovery] Active IP switched to {new_ip}. Triggering re-registration.", flush=True)
    try:
        temp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        temp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        send_startup_announcement(temp_sock)
        temp_sock.close()
    except Exception as e:
        print(f"[Wan2GP Discovery] Failed to re-register after IP switch: {e}", flush=True)

def get_node_info():
    local_ip = get_local_ip()
    hostname = os.getenv("HOST_NAME") or os.getenv("COMPUTERNAME") or socket.gethostname()
    api_port = os.getenv("GATEWAY_PORT", "58080")
    base_url = os.getenv("WAN2GP_BASE_URL", f"http://{local_ip}:{api_port}")
    ip_slug = local_ip.replace('.', '_')
    
    return {
        "id": f"app_gateway_image_{ip_slug}",
        "name": f"app gateway (Wan2GP 生圖 @ {hostname})",
        "type": "image",
        "service_source": "app_gateway",
        "hostname": hostname,
        "base_url": base_url,
        "health_endpoint": "/health",
        "priority": 1,
        "metadata": {
            "provider": "Wan2GP Gateway",
            "hostname": hostname,
            "capabilities": ["image_generation", "sd", "flux"]
        }
    }

last_registered = {}

def register_to_fb_vba(register_url):
    now = time.time()
    if register_url in last_registered and now - last_registered[register_url] < 10:
        return
    
    node_info = get_node_info()
    try:
        data = json.dumps(node_info, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(
            register_url,
            data=data,
            headers={'Content-Type': 'application/json; charset=utf-8'}
        )
        with urllib.request.urlopen(req, timeout=5) as response:
            res = json.loads(response.read().decode('utf-8'))
            if res.get('success'):
                print(f"[Wan2GP Discovery] Registered image node ({node_info['name']}) to FB-VBA host: {register_url}", flush=True)
                last_registered[register_url] = now
            else:
                print(f"[Wan2GP Discovery] Registration response: {res}", flush=True)
    except urllib.error.HTTPError as e:
        err_msg = str(e)
        try: err_msg = e.read().decode('utf-8')
        except Exception: pass
        print(f"[Wan2GP Discovery] Registration HTTP Error {e.code} for ({register_url}): {err_msg}", flush=True)
    except Exception as e:
        print(f"[Wan2GP Discovery] Registration failed ({register_url}): {e}", flush=True)

def send_startup_announcement(sock):
    """啟動時主動對區網發送 UDP 上線宣告報文，告知 FB-VBA 有新節點加入"""
    try:
        node_info = get_node_info()
        payload = json.dumps({
            "action": "register",
            **node_info
        }).encode('utf-8')
        sock.sendto(payload, ('255.255.255.255', UDP_PORT))
        local_ip = get_local_ip()
        if local_ip.startswith('192.168.') or local_ip.startswith('10.'):
            subnet_bc = local_ip.rsplit('.', 1)[0] + '.255'
            sock.sendto(payload, (subnet_bc, UDP_PORT))
        print(f"[Wan2GP Discovery] Sent active startup registration broadcast to LAN", flush=True)

        # Proactively register to FB-VBA default HTTP endpoints
        probe_url = f"http://{local_ip}:3333/api/ai-nodes/register"
        try:
            register_to_fb_vba(probe_url)
        except Exception:
            pass
    except Exception as e:
        print(f"[Wan2GP Discovery] Failed to send active startup broadcast: {e}", flush=True)

def listen_fb_vba_broadcast():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, 'SO_REUSEPORT'):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except Exception:
            pass
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except Exception:
        pass
    
    try:
        sock.bind(('', UDP_PORT))
        print(f"[Wan2GP Discovery] UDP broadcast listener started on port {UDP_PORT}", flush=True)
        send_startup_announcement(sock)
    except Exception as e:
        print(f"[Wan2GP Discovery] Failed to bind UDP port {UDP_PORT}: {e}", flush=True)
        return

    while True:
        try:
            data, addr = sock.recvfrom(2048)
            msg = json.loads(data.decode('utf-8'))
            if isinstance(msg, dict) and msg.get('service') == 'FB-VBA' and 'register_url' in msg:
                reg_url = msg['register_url']
                if '172.' in reg_url:
                    parts = reg_url.split('/')
                    port = parts[2].split(':')[1] if ':' in parts[2] else '3333'
                    reg_url = f"http://{addr[0]}:{port}/api/ai-nodes/register"
                
                global is_managed_by_hub, hub_url
                if msg.get('is_hub_proxy'):
                    is_managed_by_hub = True
                    hub_url = reg_url
                    print(f"[Wan2GP Discovery] Broadcast from Service_LM Hub ({addr[0]}), registering image node...", flush=True)
                else:
                    print(f"[Wan2GP Discovery] Broadcast received from {addr[0]}, registering image node to {reg_url}...", flush=True)
                    
                register_to_fb_vba(reg_url)
        except Exception as e:
            time.sleep(1)

def proactive_hub_registration():
    """主動嘗試向本機的 Service_LM Hub 註冊，以繞過 Docker 廣播隔離"""
    global is_managed_by_hub, hub_url
    while True:
        try:
            # 嘗試向 Service_LM 預設的 8020 port 註冊
            local_ip = get_local_ip()
            test_urls = [
                f"http://127.0.0.1:8020/api/ai-nodes/register",
                f"http://{local_ip}:8020/api/ai-nodes/register",
                f"http://host.docker.internal:8020/api/ai-nodes/register"
            ]
            
            registered = False
            for url in test_urls:
                try:
                    node_info = get_node_info()
                    data = json.dumps(node_info, ensure_ascii=False).encode('utf-8')
                    req = urllib.request.Request(
                        url, data=data, headers={'Content-Type': 'application/json; charset=utf-8'}
                    )
                    with urllib.request.urlopen(req, timeout=10) as response:
                        res = json.loads(response.read().decode('utf-8'))
                        if res.get('success'):
                            is_managed_by_hub = True
                            hub_url = url
                            register_to_fb_vba(url)
                            registered = True
                            
                            # 向 Hub 查詢它目前使用的實體 IP，並強制同步
                            try:
                                hub_ip_url = url.replace("/api/ai-nodes/register", "/api/network/ips")
                                req_ip = urllib.request.Request(hub_ip_url, headers={'Content-Type': 'application/json'})
                                with urllib.request.urlopen(req_ip, timeout=2) as ip_resp:
                                    ip_res = json.loads(ip_resp.read().decode('utf-8'))
                                    hub_current_ip = ip_res.get('current')
                                    if hub_current_ip and hub_current_ip != get_local_ip():
                                        print(f"[Wan2GP Discovery] Syncing IP with Hub: {hub_current_ip}", flush=True)
                                        set_active_ip(hub_current_ip)
                            except Exception as e:
                                print(f"[Wan2GP Discovery] Failed to sync IP with Hub: {e}", flush=True)
                                
                            break
                except Exception as e:
                    print(f"[Wan2GP Discovery] Error trying {url}: {e}", flush=True)
            
            if not registered:
                is_managed_by_hub = False
            else:
                # 如果已經被 Hub 納管，定期（每 10 秒）去 Hub 查詢最新 IP，確保同步切換
                try:
                    if hub_url:
                        hub_ip_url = hub_url.replace("/api/ai-nodes/register", "/api/network/ips")
                        req_ip = urllib.request.Request(hub_ip_url, headers={'Content-Type': 'application/json'})
                        with urllib.request.urlopen(req_ip, timeout=5) as ip_resp:
                            ip_res = json.loads(ip_resp.read().decode('utf-8'))
                            hub_current_ip = ip_res.get('current')
                            if hub_current_ip and hub_current_ip != get_local_ip():
                                print(f"[Wan2GP Discovery] Syncing IP with Hub: {hub_current_ip}", flush=True)
                                set_active_ip(hub_current_ip)
                except Exception as e:
                    print(f"[Wan2GP Discovery] Failed to sync IP with Hub during active poll: {e}", flush=True)
                    
        except Exception as e:
            print(f"[Wan2GP Discovery] Global loop error: {e}", flush=True)
        time.sleep(10)

def start_discovery_listener():
    thread = threading.Thread(target=listen_fb_vba_broadcast, daemon=True)
    thread.start()
    
    # 啟動主動註冊執行緒
    proactive_thread = threading.Thread(target=proactive_hub_registration, daemon=True)
    proactive_thread.start()
    
    return thread
