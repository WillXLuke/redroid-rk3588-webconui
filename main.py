from flask import Flask, render_template, request, redirect, url_for
import docker, os, shutil, json

app = Flask(__name__)
client = docker.from_env()

# 确保配置目录存在
V2RAY_BASE_PATH = "/v2ray-configs"
REDROID_BASE_PATH = "/redroid-data"
os.makedirs(V2RAY_BASE_PATH, exist_ok=True)
os.makedirs(REDROID_BASE_PATH, exist_ok=True)

def get_v2ray_containers():
    """获取所有 v2ray 容器"""
    return [c for c in client.containers.list(all=True) if c.name.startswith("v2ray-")]

def list_v2ray_profiles():
    """列出所有 v2ray 配置信息"""
    v2rays = []
    for c in get_v2ray_containers():
        v2ray_name = c.name
        config_dir = os.path.join(V2RAY_BASE_PATH, v2ray_name)
        info_path = os.path.join(config_dir, "info.json")
        info = {"name": v2ray_name, "status": c.status, "id": c.id[:12]}

        # 尝试读取 info.json 获取用户配置
        try:
            with open(info_path, "r") as f:
                user_cfg = json.load(f)
                info.update(user_cfg)
        except Exception:
            pass

        # 尝试读取 config.json 获取监听端口和协议
        try:
            with open(os.path.join(config_dir, "config.json"), "r") as f:
                v2cfg = json.load(f)
                inbound = v2cfg.get("inbounds", [{}])[0]
                info["listen_port"] = inbound.get("port", 10809)
                info["protocol"] = inbound.get("protocol", "")
        except Exception:
            info["listen_port"] = ""
            info["protocol"] = ""

        # 尝试获取容器 IP 地址
        try:
            info["ip"] = c.attrs['NetworkSettings']['IPAddress']
            if not info["ip"]:
                networks = c.attrs['NetworkSettings']['Networks']
                info["ip"] = list(networks.values())[0].get('IPAddress')
        except Exception:
            info["ip"] = ""

        v2rays.append(info)
    return v2rays

def get_redroid_containers():
    """获取所有 redroid 容器"""
    return [c for c in client.containers.list(all=True) if c.name.startswith("redroid-")]

def find_v2ray_by_ip_port(ip, port, v2ray_list=None):
    """根据 IP 和端口查找 v2ray 实例"""
    v2rays = v2ray_list if v2ray_list is not None else list_v2ray_profiles()
    for v in v2rays:
        if v.get("ip") == ip and str(v.get("listen_port")) == str(port):
            return v
    return None

@app.route("/")
def index():
    """主页，显示所有容器列表"""
    v2rays = list_v2ray_profiles()
    redroids = []
    for c in get_redroid_containers():
        cmd_args = c.attrs.get("Config", {}).get("Cmd", []) or []
        proxy_info = {"type": "", "host": "", "port": ""}
        for arg in cmd_args:
            if arg.startswith("androidboot.redroid_net_proxy_type="):
                proxy_info["type"] = arg.split("=", 1)[1]
            elif arg.startswith("androidboot.redroid_net_proxy_host="):
                proxy_info["host"] = arg.split("=", 1)[1]
            elif arg.startswith("androidboot.redroid_net_proxy_port="):
                proxy_info["port"] = arg.split("=", 1)[1]

        proxy_v2ray = None
        if proxy_info["host"] and proxy_info["port"]:
            proxy_v2ray = find_v2ray_by_ip_port(proxy_info["host"], proxy_info["port"], v2rays)

        port_bindings = c.attrs['HostConfig'].get('PortBindings', {})
        port = port_bindings.get('5555/tcp', [{'HostPort': '未知'}])[0]['HostPort']
        redroids.append({
            "name": c.name,
            "status": c.status,
            "port": port,
            "proxy_v2ray": proxy_v2ray
        })
    return render_template("index.html", v2rays=v2rays, redroids=redroids)

@app.route("/v2ray/create", methods=["POST"])
def v2ray_create():
    """创建 V2Ray 容器"""
    name = request.form["name"].strip()
    proxy_type = request.form["proxy_type"].strip()
    address = request.form["proxy_addr"].strip()
    port = request.form["proxy_port"].strip()
    user = request.form["proxy_user"].strip()
    password = request.form["proxy_pass"].strip()
    # 从表单获取 DNS 字符串
    dns_str = request.form.get("proxy_dns", "").strip()

    config_dir = os.path.join(V2RAY_BASE_PATH, f"v2ray-{name}")
    os.makedirs(config_dir, exist_ok=True)

    # V2Ray 配置
    inbound_port = 10809
    inbound_protocol = "http" # redroid 使用 http 代理
    inbound_settings = {"timeout": 300}

    outbound_settings = {
        "servers": [{
            "address": address,
            "port": int(port)
        }]
    }
    if user and password:
        outbound_settings["servers"][0]["users"] = [{
            "user": user,
            "pass": password
        }]

    config = {
        "inbounds": [{
            "port": inbound_port,
            "listen": "0.0.0.0",
            "protocol": inbound_protocol,
            "settings": inbound_settings
        }],
        "outbounds": [{
            "protocol": proxy_type,
            "settings": outbound_settings
        }]
    }

    # 如果填写了 DNS，则加入到配置中
    if dns_str:
        dns_servers = [d.strip() for d in dns_str.split(',') if d.strip()]
        if dns_servers:
            config["dns"] = {
                "servers": dns_servers
            }

    # 写入 config.json
    with open(os.path.join(config_dir, "config.json"), "w") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    # 新增: 将 dns 信息也保存到 info.json
    info = {
        "proxy_type": proxy_type,
        "proxy_addr": address,
        "proxy_port": port,
        "proxy_user": user,
        "proxy_pass": password,
        "dns": dns_str
    }
    with open(os.path.join(config_dir, "info.json"), "w") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    # 运行容器
    client.containers.run(
        "teddysun/v2ray", # 使用 v2fly/v2fly-core 镜像可能更佳
        detach=True,
        name=f"v2ray-{name}",
        volumes={config_dir: {'bind': '/etc/v2ray', 'mode': 'rw'}},
        restart_policy={"Name": "unless-stopped"}
    )
    return redirect(url_for("index"))

# === V2Ray 容器操作 ===
@app.route("/v2ray/delete/<name>")
def v2ray_delete(name):
    cname = f"v2ray-{name}"
    try:
        container = client.containers.get(cname)
        container.remove(force=True)
    except docker.errors.NotFound:
        pass
    shutil.rmtree(os.path.join(V2RAY_BASE_PATH, cname), ignore_errors=True)
    return redirect(url_for("index"))

@app.route("/v2ray/stop/<name>")
def v2ray_stop(name):
    try:
        client.containers.get(f"v2ray-{name}").stop()
    except docker.errors.NotFound:
        pass
    return redirect(url_for("index"))

@app.route("/v2ray/start/<name>")
def v2ray_start(name):
    try:
        client.containers.get(f"v2ray-{name}").start()
    except docker.errors.NotFound:
        pass
    return redirect(url_for("index"))

@app.route("/v2ray/restart/<name>")
def v2ray_restart(name):
    try:
        client.containers.get(f"v2ray-{name}").restart()
    except docker.errors.NotFound:
        pass
    return redirect(url_for("index"))

# === Redroid 容器操作 ===
@app.route("/redroid/create", methods=["POST"])
def redroid_create():
    v2rays = list_v2ray_profiles()
    name = request.form["name"].strip()
    width = request.form["width"].strip()
    height = request.form["height"].strip()
    port = request.form["port"].strip()
    proxy = request.form.get("proxy")

    data_path = os.path.join(REDROID_BASE_PATH, f"redroid-{name}")
    os.makedirs(data_path, exist_ok=True)

    cmd_args = [
        f"androidboot.redroid_height={height}",
        f"androidboot.redroid_width={width}",
    ]
    if proxy and proxy != "none":
        v2ray = next((v for v in v2rays if v["name"] == proxy), None)
        if v2ray:
            v2ray_ip = v2ray.get("ip")
            v2ray_port = v2ray.get("listen_port")
            if v2ray_ip and v2ray_port:
                cmd_args += [
                    "androidboot.redroid_net_proxy_type=static",
                    f"androidboot.redroid_net_proxy_host={v2ray_ip}",
                    f"androidboot.redroid_net_proxy_port={v2ray_port}"
                ]

    client.containers.run(
        "cnflysky/redroid-rk3588:lineage-20",
        detach=True,
        name=f"redroid-{name}",
        ports={"5555/tcp": int(port)},
        volumes={data_path: {'bind': '/data', 'mode': 'rw'}},
        restart_policy={"Name": "unless-stopped"},
        privileged=True,
        command=cmd_args
    )
    return redirect(url_for("index"))

@app.route("/redroid/delete/<name>")
def redroid_delete(name):
    cname = f"redroid-{name}"
    try:
        client.containers.get(cname).remove(force=True)
    except docker.errors.NotFound:
        pass
    shutil.rmtree(os.path.join(REDROID_BASE_PATH, cname), ignore_errors=True)
    return redirect(url_for("index"))

@app.route("/redroid/stop/<name>")
def redroid_stop(name):
    try:
        client.containers.get(f"redroid-{name}").stop()
    except docker.errors.NotFound:
        pass
    return redirect(url_for("index"))

@app.route("/redroid/start/<name>")
def redroid_start(name):
    try:
        client.containers.get(f"redroid-{name}").start()
    except docker.errors.NotFound:
        pass
    return redirect(url_for("index"))

@app.route("/redroid/restart/<name>")
def redroid_restart(name):
    try:
        client.containers.get(f"redroid-{name}").restart()
    except docker.errors.NotFound:
        pass
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)