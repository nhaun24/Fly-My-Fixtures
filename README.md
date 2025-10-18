# FollowSpot Web + sACN (Joystick → Fixtures)

Headless Python app that reads a joystick (e.g., Thrustmaster HOTAS), drives fixtures over **sACN (E1.31)**, and serves a **web UI** to configure fixtures dynamically (add/edit/toggle, single or multi-universe). Supports **GPIO LEDs on Raspberry Pi** (Green=Active, Red=Error). Settings persist to `settings.json` and auto-backup to `fixtures.csv`.

Originally developed with the idea we would use a Raspberry Pi 3B+ with POE hat to power this, we were Hit with a driver issue. As there are no official drivers for debian, or any other flavor of linux, We are currently forced to use Windows with the official windows drivers available from the website.
https://ts.thrustmaster.com/download/pub/webupdate/TFlightHotas/2025_TFHT_4.exe

 We have found this reddit article on the linux drivers and will be attempting as development continues
https://www.reddit.com/r/hotas/comments/v0ud87/to_use_the_thrustmaster_tflight_4_hotas_on_linux/


## Features

- Joystick control (Pan/Tilt with expo & fine mode)
- Throttle → **Dimmer**, hold “Zoom Mod” → **Zoom**
- “Trigger” flash @ ~10% while held
- Multi-fixture, multi-universe fan-out
- Web UI: live logs, status lights, **add/edit/toggle fixtures**
- GPIO LEDs (Pi): **Green=Active**, **Red=Error**
- Persists settings to `settings.json` + CSV backup `fixtures.csv`
- sACN priority default **150** (merges above a desk at 100)

---

## Requirements

### OS
Ubuntu Server 20.04+ (or Raspberry Pi OS). Works headless.

### System Packages
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv \
                    libsdl2-2.0-0 \
                    python3-gpiozero python3-lgpio \
                    git

    libsdl2-2.0-0 is required for pygame’s joystick API (works without X).
    On non-Pi machines, the GPIO packages are harmless; the app degrades gracefully.

Python Packages (inside venv)

    flask

    pygame

    sacn (python-sACN)

Clone & Install

mkdir -p ~/followspot && cd ~/followspot
git clone https://github.com/<yourname>/<repo>.git .
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install flask pygame sacn

First Run

source ~/followspot/.venv/bin/activate
python followspot_server.py

Then open a browser to:

    http://<server-ip>:80/ (default)

    or change to port 8080 if preferred

Configure

    Use the web UI to add fixtures, change mappings, and toggle multi-universe.

    GPIO LEDs:

        GPIO17 → Green LED → resistor → GND

        GPIO27 → Red LED → resistor → GND

        Change pins in UI if wired differently.

Persistence

    Writes settings.json and auto-creates fixtures.csv

    Auto-loads from CSV if JSON is missing or empty

Run on Boot (systemd)
1️⃣ Change port if desired

Edit the bottom of followspot_server.py:

APP.run(host="0.0.0.0", port=8080, threaded=True)

2️⃣ Create the service

sudo tee /etc/systemd/system/followspot.service >/dev/null <<'EOF'
[Unit]
Description=FollowSpot Web + sACN
After=network-online.target
Wants=network-online.target

[Service]
User=%i
WorkingDirectory=/home/%i/followspot
Environment=SDL_VIDEODRIVER=dummy
Environment=SDL_AUDIODRIVER=dummy
ExecStart=/home/%i/followspot/.venv/bin/python /home/%i/followspot/followspot_server.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
sudo sed -i "s/%i/$USER/g" /etc/systemd/system/followspot.service

3️⃣ Enable & start

sudo systemctl daemon-reload
sudo systemctl enable --now followspot.service
systemctl status followspot.service

4️⃣ Open firewall

sudo ufw allow 8080/tcp

Browse to http://<server-ip>:8080/.
Port 80 (Optional)

To bind port 80 without root:

sudo setcap 'cap_net_bind_service=+ep' ~/followspot/.venv/bin/python

Then keep the port 80 line in followspot_server.py and restart the service.
Updating

cd ~/followspot
git pull
source .venv/bin/activate
pip install -U flask pygame sacn
sudo systemctl restart followspot.service

Troubleshooting
No joystick detected

Run:

lsusb

Ensure USB passthrough if using a VM or container.
Port in use

sudo lsof -i :80

Change to port 8080 if needed.
No sACN output

    Verify correct universes in the fixture list.

    Test with a tool like sACNView on another device.

Files
File	Purpose
followspot_server.py	Main program
settings.json	Persistent settings (includes fixture list)
fixtures.csv	Auto-generated backup / import-export list
followspot.service	Systemd unit file
License

MIT




