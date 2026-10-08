# gua 沙箱电脑镜像（v0.6）：Xvfb + openbox + x11vnc/noVNC + AT-SPI + gua 守护进程 + 演示 GTK 应用
# 构建：  docker build -t gua-sandbox -f sandbox/Dockerfile .
# 运行：  docker run --rm -p 127.0.0.1:8765:8765 -p 127.0.0.1:6080:6080 \
#           -e GUA_SANDBOX_TOKEN=change-me --cap-drop ALL --security-opt no-new-privileges \
#           --pids-limit 512 --memory 2g gua-sandbox
# 客户端：gua run --platform remote --remote-url http://127.0.0.1:8765 ...（token 从 GUA_SANDBOX_TOKEN 读取）
# 说明：本仓库的 CI / 开发沙箱里没有 Docker，这个镜像未在本次提交中实际构建；同一守护进程已用
#       gua/sandbox/local.py（本机 Xvfb）端到端测试过（tests/test_v06_remote.py）。
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 DISPLAY=:1 \
    GTK_MODULES=gail:atk-bridge NO_AT_BRIDGE=0 GUA_SANDBOX_PORT=8765 GUA_NOVNC_PORT=6080 \
    GUA_SCREEN=1280x800x24

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb openbox x11vnc novnc websockify xdotool wmctrl x11-utils imagemagick \
      dbus-x11 at-spi2-core python3 python3-gi gir1.2-gtk-3.0 python3-pyatspi gir1.2-atspi-2.0 \
      python3-pil fonts-noto-cjk ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash sandbox

COPY gua/sandbox/daemon.py /opt/gua/daemon.py
COPY gua/sandbox/apps/gua_form.py /opt/gua/apps/gua_form.py
COPY sandbox/entrypoint.sh /opt/gua/entrypoint.sh
RUN printf '#!/bin/sh\nexec python3 /opt/gua/apps/gua_form.py "$@"\n' > /usr/local/bin/gua-form \
    && chmod 755 /usr/local/bin/gua-form /opt/gua/entrypoint.sh

USER sandbox
WORKDIR /home/sandbox
EXPOSE 8765 6080
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/gua/entrypoint.sh"]
