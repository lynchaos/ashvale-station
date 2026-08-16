# Getting Ashvale Station onto a Pi

Two routes. Pick the second one unless the card is empty.

| | Use when | Cost |
|---|---|---|
| **Prebuilt image** | A blank SD card, or you want a known-good starting point | ~500 MB download, reflashes the card |
| **Install script** | You already have a working Pi OS | ~2 minutes, keeps everything else |

---

## Install script (recommended)

Works on any Raspberry Pi already running Raspberry Pi OS (Bookworm or Trixie).

```bash
curl -fsSL https://raw.githubusercontent.com/lynchaos/ashvale-station/main/deploy/install.sh | sudo bash
```

Installs the apt dependencies, clones into `/opt/ashvale`, builds a venv with
`--system-site-packages`, enables I2C, and installs and starts a systemd unit.
Then it **polls the HTTP endpoint** rather than trusting systemd, because a unit
that is crash-looping on a port clash reports `active` for the instant between
exec and its first failed bind.

Re-running it upgrades in place. It never touches `data/` or `config.yaml`,
because those are your history and your coordinates and neither is recoverable.

Environment overrides, mostly useful for running a second instance beside a
live one:

```bash
ASHVALE_DEST=/opt/ashvale-2 ASHVALE_SERVICE=ashvale-2 ASHVALE_PORT=8099 \
  sudo -E bash deploy/install.sh
```

Reboot afterwards if it told you it enabled I2C: the Sense HAT is not detected
until you do, and until then the station silently runs its simulator, which
looks like it is working and is measuring nothing.

---

## Prebuilt image

Download the `.img.xz` and its `.sha256` from
[Releases](https://github.com/lynchaos/ashvale-station/releases), verify, and
flash with Raspberry Pi Imager.

```bash
sha256sum -c ashvale-station-*.img.xz.sha256
```

**Set your username, password and WiFi in Imager's customisation dialog.** The
image deliberately contains none of them. It also contains no SSH host keys:
those are generated on first boot, because an image shipping real host keys
would give every person who flashed it the same identity and make them trivially
impersonable on their own network.

Boot it, wait a minute or two for the first-boot expansion, then open
`http://<your-pi>:8000`.

### What is in it

Raspberry Pi OS Lite, Trixie, arm64, plus:

- the application in `/opt/ashvale` with its venv already built
- `ashvale.service`, enabled
- I2C enabled in `config.txt`, which the Sense HAT needs
- a login banner with the address and the security caveat
- `/opt/ashvale/README.first-boot`

Lite, not Desktop: the station is headless and a desktop would eat the 512 MB
budget the whole project is designed around.

### What is deliberately not in it

No password, no WiFi credentials, no SSH host keys, no database, no trained
model state, and coordinates set to Greenwich at 0 m. That last one is wrong for
everybody on purpose: altitude feeds the sea-level pressure reduction on every
stored row, and pressure tendency is what drives the precipitation forecast, so
a plausible-looking wrong altitude is worse than an obviously wrong one.

The build copies the working tree through a `.gitignore` filter rather than a
hand-written exclude list. That is a security property: a hand-written list
missed `HANDOVER.md`, which is gitignored precisely because it holds LAN
addresses and SSH details, and a local build would have baked one person's
network into an image other people flash.

---

## Building the image yourself

CI does it on every tag via `.github/workflows/image.yml`, on a pinned pi-gen
commit so the output does not move when an upstream branch does. To build
locally you need a Linux host (or Docker) with `qemu-user-static`:

```bash
git clone --branch arm64 https://github.com/RPi-Distro/pi-gen
cp deploy/pi-image/config pi-gen/config
cp -r deploy/pi-image/stage-ashvale pi-gen/
touch pi-gen/stage3/SKIP pi-gen/stage4/SKIP pi-gen/stage5/SKIP
rm -f pi-gen/stage2/EXPORT_IMAGE
echo "ASHVALE_SRC=$PWD" >> pi-gen/config
cd pi-gen && sudo -E ./build.sh
```

Expect roughly an hour and about 10 GB of scratch space.

---

## Licensing

Ashvale Station is Apache 2.0. The image also contains Raspberry Pi OS and
Debian, which carry their own licences including some non-free firmware. It is
an unofficial image and is not endorsed by or affiliated with Raspberry Pi Ltd
or the Debian project.
