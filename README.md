# redroid-rk3588-webconui
A local web ui for controlling Redroid containers.
#Web service based on Flask(Just for local use).

You'll need a SBC with rk3588/rk3588s chip to run this!

How to build:

git clone https://github.com/WillXLuke/redroid-rk3588-webconui.git

cd redroid-rk3588-webconui

docker build -t webconui .

Run:

docker run -d --name webconui -v /dev/net/tun:/dev/net/tun -v /var/run/docker.sock:/var/run/docker.sock -v /redroid-data:/redroid-data -p 80:5000 webconui
  
Then type your device IP in a browser the page will show up after pressing enter.
