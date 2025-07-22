from flask import Flask, render_template, request, redirect, url_for, jsonify
import docker, os, shutil, ipaddress, socket, time

app = Flask(__name__)
# 从环境中初始化 Docker 客户端
client = docker.from_env()

# 定义 Redroid 数据存储的基础路径
DATA_BASE_PATH = "/redroid-data"
# 定义用于 tun2socks 网络的父网段
TUN_BASE_SUBNET = ipaddress.IPv4Network("172.28.0.0/16")

def is_port_used(port):
    """检查指定的 ADB 端口是否已被任何 redroid 容器占用。"""
    for c in client.containers.list(all=True):
        if c.name.startswith("redroid-"):
            # 获取容器的端口绑定信息
            port_bindings = c.attrs['HostConfig']['PortBindings'] or {}
            if '5555/tcp' in port_bindings and str(port) == port_bindings['5555/tcp'][0]['HostPort']:
                return True
    return False

def get_next_subnet():
    """从 TUN_BASE_SUBNET 中查找并返回一个未被使用的 /24 子网。"""
    used_subnets = []
    # 遍历所有网络，记录已使用的 redroidnet 子网
    for n in client.networks.list():
        if n.attrs['Driver'] == 'bridge' and n.name.startswith('redroidnet-'):
            # 确保 IPAM 配置存在
            if n.attrs['IPAM']['Config'] and len(n.attrs['IPAM']['Config']) > 0:
                cfg = n.attrs['IPAM']['Config'][0].get('Subnet')
                if cfg:
                    used_subnets.append(ipaddress.IPv4Network(cfg))
    
    # 从父网段中查找可用的 /24 子网
    for subnet in TUN_BASE_SUBNET.subnets(new_prefix=24):
        if subnet not in used_subnets:
            return subnet
            
    raise RuntimeError("没有可用的子网了 (No available subnets)")

def get_host_ip():
    """获取宿主机的主要 IPv4 地址。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "无法获取 (Cannot get IP)"

@app.route("/")
def index():
    """主页，显示容器列表。"""
    host_ip = get_host_ip()
    containers_data = []
    for c in client.containers.list(all=True):
        if not c.name.startswith("redroid-"):
            continue
        
        port_bindings = c.attrs['HostConfig']['PortBindings'] or {}
        port = port_bindings.get('5555/tcp', [{'HostPort': '未知'}])[0]['HostPort']
        labels = c.labels
        
        containers_data.append({
            "name": c.name,
            "status": c.status,
            "port": port,
            "proxy": {
                "host": labels.get("proxy_host", ""),
                "port": labels.get("proxy_port", ""),
                "user": labels.get("proxy_user", ""),
                "pass": labels.get("proxy_pass", "")
            }
        })
    return render_template("index.html", containers=containers_data, host_ip=host_ip)

@app.route("/check_port")
def check_port():
    """用于前端 Ajax 请求，检查端口是否被占用。"""
    port_to_check = request.args.get("port", "")
    return jsonify({"used": is_port_used(port_to_check)})

@app.route("/create", methods=["POST"])
def create():
    """处理创建新容器的请求。"""
    # 从表单获取数据
    name = request.form["name"].strip()
    port = request.form["port"].strip()
    width = request.form["width"].strip()
    height = request.form["height"].strip()
    ph, pp, pu, pw = [request.form[k].strip() for k in ("proxy_host", "proxy_port", "proxy_user", "proxy_pass")]

    if is_port_used(port):
        return "端口已被占用 (Port is already in use)", 400

    full_container_name = f"redroid-{name}"
    data_path = os.path.join(DATA_BASE_PATH, full_container_name)
    os.makedirs(data_path, exist_ok=True)

    net_name = f"redroidnet-{name}"
    
    # 检查网络是否已存在，如果不存在则创建
    try:
        network = client.networks.get(net_name)
        subnet_str = network.attrs['IPAM']['Config'][0]['Subnet']
        subnet = ipaddress.IPv4Network(subnet_str)
    except docker.errors.NotFound:
        subnet = get_next_subnet()
        network = client.networks.create(
            net_name, driver="bridge",
            ipam=docker.types.IPAMConfig(pool_configs=[docker.types.IPAMPool(subnet=str(subnet))])
        )

    # 从子网中分配 IP 地址：.2 给 tun2socks, .3 给 redroid (.1 通常是网关)
    tun_ip = str(list(subnet.hosts())[1])
    redroid_ip = str(list(subnet.hosts())[2])

    # 构造代理 URL，处理有无用户名密码的情况
    proxy_url = f"socks5://{pu}:{pw}@{ph}:{pp}" if pu and pw else f"socks5://{ph}:{pp}"

    # 1. 使用 client.api 辅助函数创建网络配置，以兼容不同版本的 docker-py
    tun_endpoint_config = client.api.create_endpoint_config(ipv4_address=tun_ip)
    tun_networking_config = client.api.create_networking_config({
        net_name: tun_endpoint_config
    })

    # 2. 启动 tun2socks 容器，使用修正后的参数
    client.containers.run(
        "xjasonlyu/tun2socks:latest",
        detach=True,
        name=f"tun2socks-{name}",
        cap_add=["NET_ADMIN"],  # 授予网络管理权限，比 privileged 更安全
        devices=["/dev/net/tun:/dev/net/tun:rwm"], # 映射 TUN 设备
        network_mode="host",
        command=[
            "-proxy", proxy_url,
            "-device", "tun0", # 正确的参数是 -device
            "-dns", "8.8.8.8"
        ],
        restart_policy={"Name": "unless-stopped"}
    )
    
    # 3. 为 Redroid 容器也使用辅助函数创建网络配置
    redroid_endpoint_config = client.api.create_endpoint_config(ipv4_address=redroid_ip)
    redroid_networking_config = client.api.create_networking_config({
        net_name: redroid_endpoint_config
    })

    # 4. 启动 Redroid 容器
    redroid_container = client.containers.run(
        "cnflysky/redroid-rk3588:lineage-20",
        detach=True,
        name=full_container_name,
        ports={"5555/tcp": int(port)},
        volumes={data_path: {'bind': '/data', 'mode': 'rw'}},
        restart_policy={"Name": "unless-stopped"},
        privileged=True,  # Redroid 自身运行需要特权模式
        network=net_name,
        networking_config=redroid_networking_config, # 应用修正后的配置
        extra_hosts={"host.docker.internal": "host-gateway"},
        environment=[f"androidboot.redroid_width={width}", f"androidboot.redroid_height={height}"],
        labels={"proxy_host": ph, "proxy_port": pp, "proxy_user": pu, "proxy_pass": pw}
    )

    # 5. 设置 Redroid 容器的默认路由，指向 tun2socks 容器
    # 增加短暂延时，确保容器内网络已初始化
    time.sleep(2) 
    redroid_container.exec_run("ip route del default")
    redroid_container.exec_run(f"ip route add default via {tun_ip} dev eth0")
    
    return redirect(url_for("index"))

@app.route("/start/<container_name>")
def start(container_name):
    """启动指定的容器。"""
    client.containers.get(container_name).start()
    # 启动redroid时也一并启动tun2socks
    base_name = container_name.replace("redroid-", "")
    try:
        client.containers.get(f"tun2socks-{base_name}").start()
    except docker.errors.NotFound:
        pass
    return redirect(url_for("index"))

@app.route("/stop/<container_name>")
def stop(container_name):
    """停止指定的容器。"""
    client.containers.get(container_name).stop()
    # 停止redroid时也一并停止tun2socks
    base_name = container_name.replace("redroid-", "")
    try:
        client.containers.get(f"tun2socks-{base_name}").stop()
    except docker.errors.NotFound:
        pass
    return redirect(url_for("index"))

@app.route("/restart/<container_name>")
def restart(container_name):
    """重启指定的容器。"""
    client.containers.get(container_name).restart()
    # 重启redroid时也一并重启tun2socks
    base_name = container_name.replace("redroid-", "")
    try:
        client.containers.get(f"tun2socks-{base_name}").restart()
    except docker.errors.NotFound:
        pass
    return redirect(url_for("index"))

@app.route("/delete/<container_name>")
def delete(container_name):
    """删除指定的容器及其关联资源（网络、数据卷、tun2socks容器）。"""
    # 强制删除 Redroid 容器
    client.containers.get(container_name).remove(force=True)
    
    base_name = container_name.replace("redroid-", "")
    
    # 尝试删除关联的 tun2socks 容器
    try:
        client.containers.get(f"tun2socks-{base_name}").remove(force=True)
    except docker.errors.NotFound:
        pass # 如果找不到就不处理
        
    # 尝试删除关联的网络
    try:
        client.networks.get(f"redroidnet-{base_name}").remove()
    except docker.errors.NotFound:
        pass # 如果找不到就不处理

    # 删除宿主机上的数据目录
    shutil.rmtree(os.path.join(DATA_BASE_PATH, container_name), ignore_errors=True)
    
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
