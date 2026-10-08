# gua 沙箱电脑镜像（v0.6）：Xvfb + openbox + x11vnc/noVNC + AT-SPI + gua 守护进程 + 演示 GTK 应用
# 构建：  docker build -t gua-sandbox -f sandbox/Dockerfile .
# 运行：  docker run --rm -p 127.0.0.1:8765:8765 -p 127.0.0.1:6080:6080 \
#           -e GUA_SANDBOX_TOKEN=change-me -e GUA_SANDBOX_CONTROL_TOKEN=human-only --cap-drop ALL --security-opt no-new-privileges \
#           --pids-limit 512 --memory 2g gua-sandbox
# 客户端：gua run --platform remote --remote-url http://127.0.0.1:8765 ...（token 从 GUA_SANDBOX_TOKEN 读取）
# 验证：python scripts/smoke-sandbox.py --image gua-sandbox
#       检查真实 GTK/AT-SPI 结果、后台指针/前台不变、接管期间动作/截图阻断、RFB 输入限制及重置。
#       noVNC 无独立认证，示例仅发布到回环地址；容器测试不等于完成隔离安全审计。
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8 DISPLAY=:1 \
    GTK_MODULES=gail:atk-bridge NO_AT_BRIDGE=0 GUA_SANDBOX_PORT=8765 GUA_NOVNC_PORT=6080 \
    GUA_SCREEN=1280x800x24

RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
      printf 'Acquire::https::CaInfo "/run/secrets/proxy_ca";\n' > /etc/apt/apt.conf.d/99-build-ca; \
    fi \
    && apt-get update && apt-get install -y --no-install-recommends \
      xvfb openbox x11vnc novnc websockify xdotool wmctrl x11-utils imagemagick \
      dbus-x11 at-spi2-core python3 python3-gi gir1.2-gtk-3.0 python3-pyatspi gir1.2-atspi-2.0 \
      python3-pil fonts-noto-cjk ca-certificates tini \
    && rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99-build-ca \
    && useradd -m -s /bin/bash sandbox \
    && mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix

COPY --chmod=0644 gua/sandbox/daemon.py /opt/gua/daemon.py
COPY --chmod=0644 gua/sandbox/process.py /opt/gua/process.py
COPY --chmod=0644 gua/sandbox/apps/gua_form.py /opt/gua/apps/gua_form.py
COPY sandbox/entrypoint.sh /opt/gua/entrypoint.sh
RUN printf '#!/bin/sh\nexec python3 /opt/gua/apps/gua_form.py "$@"\n' > /usr/local/bin/gua-form \
    && chmod 755 /opt/gua /opt/gua/apps /usr/local/bin/gua-form /opt/gua/entrypoint.sh

USER sandbox
WORKDIR /home/sandbox
EXPOSE 8765 6080
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/gua/entrypoint.sh"]
