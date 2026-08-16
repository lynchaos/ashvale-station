#!/bin/bash -e
# Runs on the build host with ${ROOTFS_DIR} pointing at the target filesystem.

install -d "${ROOTFS_DIR}/opt/ashvale"

# The application source, straight from the repository being built.
#
# The filter honours .gitignore rather than listing excludes by hand, and that
# is a security property, not tidiness. A hand-written list missed HANDOVER.md,
# which is gitignored precisely because it contains LAN addresses and SSH
# details: a local build would have baked one person's network into an image
# other people flash. Anything not fit to commit is not fit to ship.
rsync -a --delete \
	--filter=':- .gitignore' \
	--exclude '.git/' --exclude '.gitignore' --exclude 'deploy/' \
	--exclude 'tests/' --exclude '.github/' --exclude '.DS_Store' \
	"${ASHVALE_SRC}/" "${ROOTFS_DIR}/opt/ashvale/"

install -m 644 files/ashvale.service   "${ROOTFS_DIR}/etc/systemd/system/ashvale.service"
install -m 755 files/ashvale-firstboot "${ROOTFS_DIR}/usr/local/sbin/ashvale-firstboot"
install -m 644 files/ashvale-firstboot.service "${ROOTFS_DIR}/etc/systemd/system/ashvale-firstboot.service"
install -m 755 files/motd.sh           "${ROOTFS_DIR}/etc/update-motd.d/20-ashvale"
install -m 644 files/README.first-boot "${ROOTFS_DIR}/opt/ashvale/README.first-boot"

# I2C is not optional: without it the Sense HAT is invisible and the station
# silently falls back to its simulator, which looks like it works and is not
# measuring anything.
CONFIG_TXT="${ROOTFS_DIR}/boot/firmware/config.txt"
[ -f "$CONFIG_TXT" ] || CONFIG_TXT="${ROOTFS_DIR}/boot/config.txt"
if ! grep -q '^dtparam=i2c_arm=on' "$CONFIG_TXT"; then
	cat >> "$CONFIG_TXT" <<'CFG'

# --- Ashvale Station ---
# Sense HAT sits on I2C. Without this the board is not detected at all.
dtparam=i2c_arm=on
# Uncomment for a DS18B20 outdoor probe on GPIO4. Left off by default because
# it claims that pin whether or not a sensor is attached.
#dtoverlay=w1-gpio
CFG
fi
