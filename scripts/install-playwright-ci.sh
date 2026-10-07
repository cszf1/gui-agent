#!/usr/bin/env bash
set -euo pipefail

# Hosted Ubuntu's mirror list can choose an unreachable Azure HTTP mirror and
# leave apt-get update waiting indefinitely. Use the official HTTPS archive;
# apt still verifies signed metadata and installs all Playwright dependencies.
if [[ -f /etc/apt/apt-mirrors.txt ]]; then
    printf '%s\n' 'https://archive.ubuntu.com/ubuntu' |
        sudo tee /etc/apt/apt-mirrors.txt >/dev/null
fi
printf '%s\n' 'Acquire::http::Timeout "30";' 'Acquire::https::Timeout "30";' 'Acquire::Retries "2";' |
    sudo tee /etc/apt/apt.conf.d/80-gui-agent-timeouts >/dev/null
python -m playwright install --with-deps chromium
