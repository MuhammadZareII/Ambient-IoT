/*
 * Ambient IoT sensing node - Arduino Nano R4 port of the STM32 Read_Sensors()/Transmit_Data() logic.
 *
 * Wiring (matches the hand-drawn diagram):
 *   Soil sensor VCC -> D12   (powered only during a reading, prevents probe electrolysis)
 *   Soil sensor SIG -> A0
 *   Soil sensor GND -> GND
 *   SHT40 VIN       -> 3.3V
 *   SHT40 SDA       -> A4    (Wire / I2C)
 *   SHT40 SCL       -> A5
 *   SHT40 GND       -> GND
 *   Xiao TX         -> D0/RX (Serial1)
 *   Xiao RX         <- D1/TX (Serial1)   ** use a divider if the Nano I/O is 5 V **
 *   Xiao GND        -> GND   (common ground is mandatory)
 *
 * Output to the Xiao (unchanged protocol from the manual, section VI):
 *   [0x02] "temp,hum,soil" [0x03]       e.g.  \x02 23.4,45.1,1873 \x03
 * Debug output goes to the USB serial port (Serial, 115200).
 *
 * Set BURST_BEACON_MODE to 1 when the Xiao runs optimized_BLE_burst_beaconing:
 * a 0xFF dummy byte is sent first to wake it, followed by a 10 ms gap.
 *
 * LOW POWER (manual section XII, adapted to the RA4M1):
 *   Between measurements the MCU enters Software Standby (CPU + high-speed clocks off).
 *   The RTC, clocked from the low-speed LOCO oscillator, keeps running and wakes the
 *   chip every 2 s via its periodic interrupt; after MEASURE_INTERVAL_S a measurement runs.
 *   Unused pins are driven low and the on-board LEDs released so nothing leaks.
 *
 *   USB does not survive standby. To re-flash: double-press the reset button
 *   (bootloader, LED pulses), then upload. Set LOW_POWER 0 for bench debugging
 *   over USB (plain delay(), Serial debug works).
 */

#include <Wire.h>
#include "RTC.h"

#define BURST_BEACON_MODE 0
#define LOW_POWER 1
#define MEASURE_INTERVAL_S 30

const uint8_t SOIL_VCC_PIN = 12;
const uint8_t SOIL_SIG_PIN = A0;
const uint8_t SHT_ADDR = 0x44;

float temperature = NAN;
float humidity = NAN;
uint16_t soil_raw = 0;

enum ShtType { SHT_NONE, SHT_4X, SHT_3X };
ShtType sht_type = SHT_NONE;

// Sensirion CRC-8 (poly 0x31, init 0xFF) - same for SHT3x and SHT4x
static uint8_t sht_crc(const uint8_t *d, int n) {
  uint8_t crc = 0xFF;
  for (int i = 0; i < n; i++) {
    crc ^= d[i];
    for (int b = 0; b < 8; b++) crc = (crc & 0x80) ? (crc << 1) ^ 0x31 : (crc << 1);
  }
  return crc;
}

// Sends a command, waits, reads 6 bytes and validates both CRCs.
static bool sht_read_raw(const uint8_t *cmd, uint8_t cmd_len, uint16_t wait_ms,
                         uint16_t &t_ticks, uint16_t &rh_ticks) {
  Wire.beginTransmission(SHT_ADDR);
  Wire.write(cmd, cmd_len);
  if (Wire.endTransmission() != 0) return false;   // NACK: no device / unknown command
  delay(wait_ms);
  if (Wire.requestFrom(SHT_ADDR, (uint8_t)6) != 6) return false;
  uint8_t rx[6];
  for (int i = 0; i < 6; i++) rx[i] = Wire.read();
  if (sht_crc(rx, 2) != rx[2] || sht_crc(rx + 3, 2) != rx[5]) return false;
  t_ticks = (rx[0] << 8) | rx[1];
  rh_ticks = (rx[3] << 8) | rx[4];
  return true;
}

static bool read_sht(void) {
  static const uint8_t SHT4X_CMD[] = {0xFD};        // high precision, ~8.3 ms
  static const uint8_t SHT3X_CMD[] = {0x24, 0x00};  // high repeatability, no clock stretch, ~15 ms
  uint16_t t, rh;

  if (sht_type != SHT_3X && sht_read_raw(SHT4X_CMD, 1, 10, t, rh)) {
    sht_type = SHT_4X;
    temperature = -45.0f + 175.0f * t / 65535.0f;
    humidity = constrain(-6.0f + 125.0f * rh / 65535.0f, 0.0f, 100.0f);
    return true;
  }
  if (sht_type != SHT_4X && sht_read_raw(SHT3X_CMD, 2, 16, t, rh)) {
    sht_type = SHT_3X;
    temperature = -45.0f + 175.0f * t / 65535.0f;
    humidity = 100.0f * rh / 65535.0f;
    return true;
  }
  sht_type = SHT_NONE;  // re-detect next time
  return false;
}

void Read_Sensors(void) {
  if (!read_sht()) {
    Serial.println("[DEBUG] SHT read failed (check SDA=A4, SCL=A5, VIN, GND)");
    temperature = NAN;
    humidity = NAN;
  }

  // Soil moisture: power up, settle, sample, power down immediately.
  digitalWrite(SOIL_VCC_PIN, HIGH);
  delay(10);
  soil_raw = analogRead(SOIL_SIG_PIN);
  digitalWrite(SOIL_VCC_PIN, LOW);
}

void Transmit_Data(void) {
  char buf[40];
  // %f is not reliable in every core's printf, so format with dtostrf-style integer math.
  int t10 = isnan(temperature) ? -9999 : (int)lroundf(temperature * 10);
  int h10 = isnan(humidity) ? 0 : (int)lroundf(humidity * 10);
  int len = snprintf(buf, sizeof(buf), "%s%d.%d,%d.%d,%u",
                     (t10 < 0 && t10 > -10) ? "-" : "", t10 / 10, abs(t10 % 10),
                     h10 / 10, h10 % 10, soil_raw);

#if BURST_BEACON_MODE
  Serial1.write((uint8_t)0xFF);  // wake the sleeping Xiao via its RX-pin interrupt
  Serial1.flush();
  delay(10);
#endif
  Serial1.write((uint8_t)0x02);
  Serial1.write((const uint8_t *)buf, len);
  Serial1.write((uint8_t)0x03);
  Serial1.flush();

#if !LOW_POWER
  Serial.print("[TX] ");
  Serial.print(buf);
  Serial.print("   (sensor: ");
  Serial.print(sht_type == SHT_4X ? "SHT4x" : sht_type == SHT_3X ? "SHT3x" : "none");
  Serial.println(")");
#endif
}

// ---------------------------------------------------------------- low power
volatile bool rtc_tick = false;
void rtc_period_cb() { rtc_tick = true; }

// Drive every pin we do not use to a defined level so no input buffer floats.
static void park_unused_pins(void) {
  const uint8_t unused[] = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 13, A1, A2, A3, A6, A7};
  for (uint8_t p : unused) {
    pinMode(p, OUTPUT);
    digitalWrite(p, LOW);
  }
  // On-board LEDs: high impedance = off whatever their polarity.
  pinMode(LED_BUILTIN, INPUT);
  pinMode(LEDR, INPUT);
  pinMode(LEDG, INPUT);
  pinMode(LEDB, INPUT);
}

// Software Standby until the next RTC periodic interrupt (every 2 s).
static void standby_until_rtc_tick(void) {
  R_SYSTEM->PRCR = 0xA502;            // unlock low-power registers
  R_SYSTEM->SBYCR_b.SSBY = 1;         // WFI -> Software Standby (not Sleep)
  R_SYSTEM->DPSBYCR_b.DPSBY = 0;      // ordinary standby, keeps RAM + pin states
  R_SYSTEM->SNZCR = 0;                // no snooze
  R_SYSTEM->PRCR = 0xA500;
  R_ICU->WUPEN |= R_ICU_WUPEN_RTCPRDWUPEN_Msk;  // RTC period IRQ may end standby

  rtc_tick = false;
  while (!rtc_tick) {
    __DSB();
    __WFI();
  }

  R_SYSTEM->PRCR = 0xA502;
  R_SYSTEM->SBYCR_b.SSBY = 0;         // plain WFI elsewhere (e.g. inside delay()) stays Sleep
  R_SYSTEM->PRCR = 0xA500;
}

static void wait_for_next_measurement(void) {
#if LOW_POWER
  for (int s = 0; s < MEASURE_INTERVAL_S; s += 2) standby_until_rtc_tick();
#else
  delay(MEASURE_INTERVAL_S * 1000UL);
#endif
}

void setup() {
  Serial.begin(115200);    // USB debug (only meaningful with LOW_POWER 0)
  Serial1.begin(115200);   // to Xiao BLE bridge; TX idles high during standby
  Wire.begin();
  Wire.setClock(100000);   // standard mode, as in the manual
  analogReadResolution(12);  // 0..4095, same range as the STM32 ADC

  pinMode(SOIL_VCC_PIN, OUTPUT);
  digitalWrite(SOIL_VCC_PIN, LOW);
  park_unused_pins();

#if LOW_POWER
  RTC.begin();
  RTCTime t0(1, Month::JANUARY, 2026, 0, 0, 0, DayOfWeek::THURSDAY, SaveLight::SAVING_TIME_INACTIVE);
  RTC.setTime(t0);
  RTC.setPeriodicCallback(rtc_period_cb, Period::ONCE_EVERY_2_SEC);
  delay(3000);  // window to start an upload right after reset, before the first standby
#else
  delay(1500);  // give the USB serial monitor time to attach
  Serial.println("[System] Nano R4 sensor node started");
#endif
}

void loop() {
  Read_Sensors();
  Transmit_Data();
  wait_for_next_measurement();
}
