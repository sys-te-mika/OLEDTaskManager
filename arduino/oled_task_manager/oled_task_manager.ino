// Must be defined before any Arduino headers are included.
// Packet with 4 disk fields is ~97 bytes worst-case; buffer must exceed that.
#define SERIAL_RX_BUFFER_SIZE 256
#define SERIAL_BUF_MAX        128

/*
 * OLEDTaskManager - Arduino Uno
 *
 * Receives PC system metrics over serial and displays them on a
 * 1.3" SH1106 OLED (I2C: SDA=A4, SCL=A5).
 *
 * Library required:
 *   U8g2  by oliver  (install via Arduino Library Manager)
 *
 * Serial protocol sent by sender.py (115200 baud, newline-terminated):
 *   CPU:72.5,RAM:43.1,GPU:51.0,DC:21.3,DD:45.2,DE:67.8,DF:12.1,CT:65.0,GT:72.0,NU:1024.50,ND:2048.30
 *   A value of -1 means that metric is unavailable on the host machine.
 *
 * Display layout (128x64, font 6x10):
 *   Page 0 – System Monitor : CPU / RAM / GPU progress bars
 *   Page 1 – Disk Usage     : C / D / E / F progress bars
 *   Page 2 – Temps & Network: temperatures + upload/download speed
 *   Pages rotate every PAGE_SWITCH_MS milliseconds.
 *
 * Memory note:
 *   Uses U8G2_..._1_... (page-buffer) mode — only 128 bytes for the display
 *   buffer instead of 1024 bytes, saving ~896 bytes of precious SRAM.
 *   Serial buffer uses a plain char[] instead of String to avoid heap
 *   fragmentation on the 2 KB Uno.
 */

#include <Wire.h>
#include <U8g2lib.h>

// --------------------------------------------------------------------------
// Hardware
// --------------------------------------------------------------------------
// SH1106 1.3" OLED — I2C (default address 0x3C)
// On Arduino Uno: SDA = A4, SCL = A5
// _1_ = page-buffer mode (128 bytes RAM vs 1024 for _F_ full-buffer)
U8G2_SH1106_128X64_NONAME_1_HW_I2C u8g2(U8G2_R0, /* reset= */ U8X8_PIN_NONE);

// --------------------------------------------------------------------------
// Configuration
// --------------------------------------------------------------------------
#define SERIAL_BAUD     115200
#define PAGE_SWITCH_MS  4000UL   // rotate pages every 4 s
#define DATA_TIMEOUT_MS 10000UL  // show "disconnected" after 10 s of silence
// SERIAL_BUF_MAX defined at top of file (128 bytes)

// --------------------------------------------------------------------------
// Data model
// --------------------------------------------------------------------------
struct SystemData {
  float cpu     = 0.0f;
  float ram     = 0.0f;
  float gpu     = -1.0f;  // -1 = unavailable
  float diskC   = -1.0f;
  float diskD   = -1.0f;
  float diskE   = -1.0f;
  float diskF   = -1.0f;
  float cpuTemp = -1.0f;  // -1 = unavailable
  float gpuTemp = -1.0f;  // -1 = unavailable
  float netUp   = 0.0f;   // KB/s
  float netDown = 0.0f;   // KB/s
} sysData;

char     serialBuf[SERIAL_BUF_MAX];
uint8_t  serialBufLen  = 0;
bool     dataReceived  = false;
uint32_t lastDataTime  = 0;
uint8_t  currentPage   = 0;
uint32_t lastPageSwitch = 0;
bool     needsRedraw   = true;   // render on first loop iteration
bool     wasConnected  = false;

// --------------------------------------------------------------------------
// Serial parsing
// --------------------------------------------------------------------------

// Extract the float value that follows "KEY:" in a comma-separated line.
// Operates on a raw char* to avoid any String/heap allocation.
// Returns -1.0 if the key is not found.
float extractFloat(const char* line, const char* key) {
  // Build search token "KEY:" on the stack
  char token[8];
  strncpy(token, key, sizeof(token) - 2);
  token[sizeof(token) - 2] = '\0';
  strncat(token, ":", 1);

  const char* p = strstr(line, token);
  if (!p) return -1.0f;
  p += strlen(token);
  return atof(p);
}

void parsePacket(const char* line) {
  sysData.cpu     = extractFloat(line, "CPU");
  sysData.ram     = extractFloat(line, "RAM");
  sysData.gpu     = extractFloat(line, "GPU");
  sysData.diskC   = extractFloat(line, "DC");
  sysData.diskD   = extractFloat(line, "DD");
  sysData.diskE   = extractFloat(line, "DE");
  sysData.diskF   = extractFloat(line, "DF");
  sysData.cpuTemp = extractFloat(line, "CT");
  sysData.gpuTemp = extractFloat(line, "GT");
  sysData.netUp   = extractFloat(line, "NU");
  sysData.netDown = extractFloat(line, "ND");
}

// --------------------------------------------------------------------------
// Drawing helpers
// --------------------------------------------------------------------------

// Draw a progress bar at (x, y) with size (w × h), filled to pct [0-100].
void drawBar(int x, int y, int w, int h, float pct) {
  pct = constrain(pct, 0.0f, 100.0f);
  u8g2.drawFrame(x, y, w, h);
  int filled = (int)((w - 2) * pct / 100.0f);
  if (filled > 0) {
    u8g2.drawBox(x + 1, y + 1, filled, h - 2);
  }
}

// Format a KB/s value: "999.9KB" or " 1.23MB".
// NOTE: snprintf %f is NOT supported on AVR (Arduino Uno) by default.
//       Use dtostrf() instead for float-to-string conversion.
void formatSpeed(float kbs, char* buf, uint8_t bufLen) {
  char tmp[10];
  if (kbs >= 1000.0f) {
    dtostrf(kbs / 1024.0f, 5, 2, tmp);
    snprintf(buf, bufLen, "%sMB", tmp);
  } else {
    dtostrf(kbs, 5, 1, tmp);
    snprintf(buf, bufLen, "%sKB", tmp);
  }
}

// --------------------------------------------------------------------------
// Page 0 — CPU / RAM / GPU  (3 rows, 16 px spacing for readability)
// --------------------------------------------------------------------------
//   y= 9  — title "System Monitor"
//   y=11  — horizontal divider
//   y=22  — CPU row  (bar top y=14)
//   y=38  — RAM row  (bar top y=30)
//   y=54  — GPU row  (bar top y=46)

void drawPage1() {
  char buf[8];
  u8g2.setFont(u8g2_font_6x10_tf);

  u8g2.drawStr(22, 9, "System Monitor");
  u8g2.drawHLine(0, 11, 128);

  struct Row {
    const char* label;
    float       val;
    bool        available;
  } rows[3] = {
    { "CPU", sysData.cpu, true },
    { "RAM", sysData.ram, true },
    { "GPU", sysData.gpu, sysData.gpu >= 0.0f },
  };

  for (uint8_t i = 0; i < 3; i++) {
    int textY = 22 + i * 16;   // baselines: 22, 38, 54
    int barY  = textY - 8;     // bar tops:  14, 30, 46

    u8g2.drawStr(0, textY, rows[i].label);
    drawBar(20, barY, 80, 8, rows[i].available ? rows[i].val : 0.0f);

    if (rows[i].available) {
      snprintf(buf, sizeof(buf), "%3d%%", (int)rows[i].val);
    } else {
      strcpy(buf, " N/A");
    }
    u8g2.drawStr(103, textY, buf);
  }
}

// --------------------------------------------------------------------------
// Page 1 — Disk Usage  C / D / E / F
// --------------------------------------------------------------------------
//   y= 9  — title "Disk Usage"
//   y=11  — divider
//   y=22,34,46,58 — rows for C: D: E: F:

void drawPage2() {
  char buf[8];
  u8g2.setFont(u8g2_font_6x10_tf);

  // "Disk Usage" = 10 chars × 6 = 60 px; centered: (128-60)/2 = 34
  u8g2.drawStr(34, 9, "Disk Usage");
  u8g2.drawHLine(0, 11, 128);

  struct DiskRow {
    const char* label;
    float       val;
  } rows[4] = {
    { "C:", sysData.diskC },
    { "D:", sysData.diskD },
    { "E:", sysData.diskE },
    { "F:", sysData.diskF },
  };

  for (uint8_t i = 0; i < 4; i++) {
    int textY = 22 + i * 12;
    int barY  = textY - 8;
    bool avail = rows[i].val >= 0.0f;

    u8g2.drawStr(0, textY, rows[i].label);
    drawBar(15, barY, 85, 8, avail ? rows[i].val : 0.0f);

    if (avail) {
      snprintf(buf, sizeof(buf), "%3d%%", (int)rows[i].val);
    } else {
      strcpy(buf, " N/A");
    }
    u8g2.drawStr(103, textY, buf);
  }
}

// --------------------------------------------------------------------------
// Page 2 — Temps & Network
//   y=11  — divider
//   y=24  — CPU Temp
//   y=36  — GPU Temp
//   y=48  — Upload speed
//   y=60  — Download speed

void drawPage3() {
  char buf[16];
  u8g2.setFont(u8g2_font_6x10_tf);

  // Title (centered: "Temps & Network" = 15 chars × 6 = 90px, offset = 19)
  u8g2.drawStr(19, 9, "Temps & Network");
  u8g2.drawHLine(0, 11, 128);

  // CPU Temperature
  u8g2.drawStr(0, 24, "CPU Temp:");
  if (sysData.cpuTemp >= 0.0f) {
    snprintf(buf, sizeof(buf), "%d C", (int)sysData.cpuTemp);
  } else {
    strcpy(buf, "N/A");
  }
  u8g2.drawStr(57, 24, buf);

  // GPU Temperature
  u8g2.drawStr(0, 36, "GPU Temp:");
  if (sysData.gpuTemp >= 0.0f) {
    snprintf(buf, sizeof(buf), "%d C", (int)sysData.gpuTemp);
  } else {
    strcpy(buf, "N/A");
  }
  u8g2.drawStr(57, 36, buf);

  // Upload speed
  u8g2.drawStr(0, 48, "Upload: ");
  formatSpeed(sysData.netUp, buf, sizeof(buf));
  u8g2.drawStr(48, 48, buf);
  u8g2.drawStr(92, 48, "/s");

  // Download speed
  u8g2.drawStr(0, 60, "Downld: ");
  formatSpeed(sysData.netDown, buf, sizeof(buf));
  u8g2.drawStr(48, 60, buf);
  u8g2.drawStr(92, 60, "/s");
}

// --------------------------------------------------------------------------
// Disconnected / waiting screen
// --------------------------------------------------------------------------
void drawDisconnected() {
  u8g2.setFont(u8g2_font_6x10_tf);
  u8g2.drawStr(10, 24, "Waiting for PC...");
  u8g2.setFont(u8g2_font_5x7_tr);
  u8g2.drawStr(5,  40, "Run sender.py on");
  u8g2.drawStr(5,  51, "your computer");
}

// --------------------------------------------------------------------------
// setup / loop
// --------------------------------------------------------------------------
void setup() {
  Serial.begin(SERIAL_BAUD);
  serialBuf[0] = '\0';
  Serial.println(F("OLEDTaskManager starting..."));

  Wire.begin();
  Wire.setClock(100000);  // 100 kHz — more reliable than 400 kHz for cheap OLEDs
  u8g2.begin();

  // Splash screen
  u8g2.firstPage();
  do {
    u8g2.setFont(u8g2_font_6x10_tf);
    u8g2.drawStr(15, 28, "OLEDTaskManager");
    u8g2.drawStr(20, 43, "Initializing...");
  } while (u8g2.nextPage());
  delay(1500);

  // Drain any bytes that arrived during splash (partial/garbage packets).
  while (Serial.available()) Serial.read();
  serialBufLen = 0;
}

void loop() {
  // ---- Read serial data ----
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n') {
      if (serialBufLen > 0) {
        serialBuf[serialBufLen] = '\0';
        parsePacket(serialBuf);
        serialBufLen = 0;
        lastDataTime = millis();
        dataReceived = true;
        needsRedraw  = true;
      }
    } else if (c != '\r') {
      if (serialBufLen < SERIAL_BUF_MAX - 1) {
        serialBuf[serialBufLen++] = c;
      } else {
        serialBufLen = 0;  // discard oversized / corrupt line
      }
    }
  }

  // ---- Page rotation ----
  uint32_t now = millis();
  if (dataReceived && (now - lastPageSwitch >= PAGE_SWITCH_MS)) {
    currentPage    = (currentPage + 1) % 3;
    lastPageSwitch = now;
    needsRedraw    = true;
  }

  // ---- Connection state change triggers a redraw ----
  bool connected = dataReceived && ((millis() - lastDataTime) < DATA_TIMEOUT_MS);
  if (connected != wasConnected) {
    wasConnected = connected;
    needsRedraw  = true;
  }

  // ---- Render only when something changed ----
  if (needsRedraw) {
    needsRedraw = false;
    u8g2.firstPage();
    do {
      if (connected) {
        if      (currentPage == 0) drawPage1();
        else if (currentPage == 1) drawPage2();
        else                       drawPage3();
      } else {
        drawDisconnected();
      }
    } while (u8g2.nextPage());
  }
}
