# OLEDTaskManager

Displays live PC system metrics on a 1.3" SH1106 OLED connected to an Arduino Uno.

```
+--- System Monitor ---+        +--- Temps & Network --+
| CPU [=========  ] 87%|        | CPU Temp: 65 C       |
| RAM [=====      ] 52%|  <-->  | GPU Temp: 72 C       |
| GPU [======     ] 61%|        | Upload:   1.23MB/s   |
| DSK [==         ] 18%|        | Downld:   5.67MB/s   |
+----------------------+        +----------------------+
```

Pages rotate every 4 seconds.

---

## Hardware

| Component | Details |
|-----------|---------|
| MCU | Arduino Uno |
| Display | 1.3" SH1106 OLED 128×64, I2C |
| Wiring | SDA → A4 · SCL → A5 · VCC → 3.3 V or 5 V · GND → GND |

---

## Arduino Setup

1. Open **Arduino IDE** and install the **U8g2** library:
   `Sketch > Include Library > Manage Libraries` → search **U8g2** by oliver → Install

2. Open `arduino/oled_task_manager/oled_task_manager.ino`.

3. Select **Board: Arduino Uno** and the correct **Port**, then upload.

4. The display will show *"Waiting for PC..."* until `sender.py` is running.

---

## PC Sender Setup

### Requirements
- Python 3.8+
- `psutil` and `pyserial` (required)
- `GPUtil` (optional — NVIDIA GPU metrics)
- `wmi` + LibreHardwareMonitor (optional — CPU/GPU temperatures on Windows)

### Install dependencies
```bash
cd pc_sender
pip install -r requirements.txt

# Optional: NVIDIA GPU support
pip install GPUtil

# Optional: temperatures on Windows
pip install wmi
```

### Temperature support on Windows
CPU and GPU temperatures are **not** directly accessible on Windows without a
helper application. For temperature readings:

1. Download and run [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
   **as Administrator**.
2. Leave it running in the background (system tray is fine).
3. Install the `wmi` Python package: `pip install wmi`

`sender.py` will automatically detect LibreHardwareMonitor and enable
temperature readings.

### Run the sender
```bash
# Auto-detect defaults (COM3, 115200 baud)
python sender.py

# Specify port explicitly
python sender.py COM5

# Specify port and baud rate
python sender.py COM5 115200

# Custom update interval (seconds)
python sender.py COM3 115200 --interval 2.0
```

Find your Arduino's COM port in **Device Manager > Ports (COM & LPT)**.

---

## Serial Protocol

`sender.py` sends one newline-terminated ASCII line per update interval:

```
CPU:72.5,RAM:43.1,GPU:51.0,DSK:21.3,CT:65.0,GT:72.0,NU:1024.50,ND:2048.30
```

| Field | Unit | Notes |
|-------|------|-------|
| CPU | % | CPU utilisation |
| RAM | % | RAM utilisation |
| GPU | % | GPU load (-1 = unavailable) |
| DSK | % | Primary disk utilisation |
| CT | °C | CPU temperature (-1 = unavailable) |
| GT | °C | GPU temperature (-1 = unavailable) |
| NU | KB/s | Network upload speed |
| ND | KB/s | Network download speed |

---

## Project Structure

```
OLEDTaskManager/
├── arduino/
│   └── oled_task_manager/
│       └── oled_task_manager.ino   # Arduino sketch
├── pc_sender/
│   ├── sender.py                   # Python host script
│   └── requirements.txt
└── README.md
```
