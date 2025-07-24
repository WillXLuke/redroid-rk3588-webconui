sudo apt update
curl -fsSL https://get.docker.com | bash -s docker --mirror Aliyun
sudo apt install -y git
git clone https://github.com/WillXLuke/redroid-rk3588-webconui.git
cd redroid-rk3588-webconui
sudo docker build -t webconui .
sudo docker run -d --name webconui --restart always -v /var/run/docker.sock:/var/run/docker.sock -v /redroid-data:/redroid-data -v /v2ray-configs:/v2ray-configs -p 80:5000 webconui