#!/bin/bash -e
# Runs inside the target filesystem, so pip resolves against the image's Python.

# --system-site-packages so numpy and the Sense HAT stack come from apt. Building
# them in a clean venv on ARM means compiling RTIMULib and numpy from source,
# which is an ordeal on a Zero 2 W and pointless when Debian ships both.
python3 -m venv --system-site-packages /opt/ashvale/.venv
/opt/ashvale/.venv/bin/pip install --no-cache-dir --upgrade pip
# Plain uvicorn, never uvicorn[standard]: the extra pulls watchfiles and uvloop,
# both of which compile Rust and C from source on ARM for features unused here.
/opt/ashvale/.venv/bin/pip install --no-cache-dir -r /opt/ashvale/requirements.txt

install -d -o 1000 -g 1000 /opt/ashvale/data

systemctl enable ashvale-firstboot.service
systemctl enable ashvale.service

# Belt and braces. Raspberry Pi OS regenerates these on first boot, but an image
# that shipped real host keys would give every flasher the same identity, so
# make certain none are present in the artifact.
rm -f /etc/ssh/ssh_host_*
