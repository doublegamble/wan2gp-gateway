import socket
import json
import urllib.request
import threading
import time
import os

UDP_PORT = int(os.getenv("UDP_PORT", 53259))

def get_local_ip():
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

def get_node_info():
    local_ip = get_local_ip()
    hostname = os.getenv("HOST_NAME") or os.getenv("COMPUTERNAME") or socket.gethostname()
    api_port = os.getenv("GATEWAY_PORT", "50080")
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
    except Exception as e:
        print(f"[Wan2GP Discovery] Registration failed ({register_url}): {e}", flush=True)

def listen_fb_vba_broadcast():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    except Exception:
        pass
    
    try:
        sock.bind(('', UDP_PORT))
        print(f"[Wan2GP Discovery] UDP broadcast listener started on port {UDP_PORT}", flush=True)
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
                
                print(f"[Wan2GP Discovery] Broadcast received from {addr[0]}, registering image node to {reg_url}...", flush=True)
                register_to_fb_vba(reg_url)
        except Exception as e:
            time.sleep(1)

def start_discovery_listener():
    thread = threading.Thread(target=listen_fb_vba_broadcast, daemon=True)
    thread.start()
    return thread
