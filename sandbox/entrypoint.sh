#!/bin/sh
# 与 gua/sandbox/local.py 做同样的事：虚拟显示器 → 会话总线 → AT-SPI → 窗口管理器 → VNC/noVNC → 守护进程
set -eu
: "${GUA_SANDBOX_TOKEN:?set GUA_SANDBOX_TOKEN}"
# v0.7：人工交还令牌 GUA_SANDBOX_CONTROL_TOKEN 可选；未设置时守护进程生成一个并只打印到容器日志（stderr）。
# agent 侧客户端只拿 GUA_SANDBOX_TOKEN，不能自行交还控制权。守护进程不会把两个令牌传给它启动的应用。
Xvfb "$DISPLAY" -screen 0 "$GUA_SCREEN" -nolisten tcp &
for i in $(seq 1 100); do [ -e "/tmp/.X11-unix/X${DISPLAY#:}" ] && break; sleep 0.05; done
eval "$(dbus-launch --sh-syntax)"
/usr/libexec/at-spi-bus-launcher --launch-immediately &
openbox &
x11vnc -display "$DISPLAY" -rfbport 5900 -localhost -shared -forever -nopw -quiet -viewonly &
websockify --web /usr/share/novnc "$GUA_NOVNC_PORT" 127.0.0.1:5900 &
LV="http://127.0.0.1:${GUA_NOVNC_PORT}/vnc.html?autoconnect=1&resize=scale"
mkdir -p /home/sandbox/work
exec python3 /opt/gua/daemon.py --host 0.0.0.0 --port "$GUA_SANDBOX_PORT" --display "$DISPLAY" \
  --workdir /home/sandbox/work --apps gua-form ${GUA_SANDBOX_APPS:-} ${GUA_SANDBOX_SHELL:+--shell} \
  --liveview "${LV}&view_only=1" --takeover-url "$LV"
