/*
 * Puente USB <-> UART para el lector NFC PN532 (Hailege, modo HSU) con un ESP32-WROOM-32.
 *
 * El ESP32 no interpreta el protocolo: reenvía byte a byte lo que llega por el USB
 * (Serial, 115200) al PN532 (Serial2, 115200) y viceversa. Así el script de Python
 * habla con el PN532 exactamente igual que con el módulo USB de Pruebas_PN532.
 *
 * Al arrancar comprueba que el PN532 responde y, si no, prueba con TX/RX intercambiados
 * (error de cableado muy habitual). El resultado se imprime por el USB en líneas que
 * empiezan por "#" y se refleja en el LED azul de la placa (GPIO2):
 *   - LED fijo encendido : PN532 detectado
 *   - LED parpadeo lento : PN532 NO detectado (revisa cableado e interruptores)
 *   - LED parpadea rápido: hay tráfico entre el PC y el PN532
 *
 * Placa en Arduino IDE: "ESP32 Dev Module" (paquete "esp32" de Espressif).
 */

#include <Arduino.h>

const int PIN_RX2 = 16;   // ESP32 recibe  <- TX  del PN532 (pin "SDA/TXD")
const int PIN_TX2 = 17;   // ESP32 envía   -> RX  del PN532 (pin "SCL/RXD")
const int PIN_LED = 2;
const uint32_t BAUDIOS = 115200;

// Preámbulo de despertar + SAMConfiguration(modo normal, timeout 0x14, IRQ)
const uint8_t WAKE_SAM[] = {
  0x55, 0x55, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
  0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
  0x00, 0x00, 0xFF, 0x05, 0xFB, 0xD4, 0x14, 0x01, 0x14, 0x01, 0x02, 0x00
};
// GetFirmwareVersion
const uint8_t GET_FW[] = {0x00, 0x00, 0xFF, 0x02, 0xFE, 0xD4, 0x02, 0x2A, 0x00};
const uint8_t ACK[] = {0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00};

bool pn532Ok = false;
int rxUsado = PIN_RX2, txUsado = PIN_TX2;
unsigned long ultimoTrafico = 0;

// Lee de Serial2 durante 'ms' milisegundos y lo guarda en buf. Devuelve los bytes leídos.
size_t leerDurante(uint8_t *buf, size_t max, unsigned long ms) {
  size_t n = 0;
  unsigned long t0 = millis();
  while (millis() - t0 < ms) {
    while (Serial2.available() && n < max) buf[n++] = Serial2.read();
  }
  return n;
}

bool contiene(const uint8_t *buf, size_t n, const uint8_t *patron, size_t m) {
  for (size_t i = 0; i + m <= n; i++) {
    if (memcmp(buf + i, patron, m) == 0) return true;
  }
  return false;
}

// Intenta despertar al PN532 con los pines dados. Si responde, imprime su firmware.
bool probarPN532(int rx, int tx) {
  Serial2.begin(BAUDIOS, SERIAL_8N1, rx, tx);
  delay(50);
  while (Serial2.available()) Serial2.read();

  uint8_t buf[64];
  Serial2.write(WAKE_SAM, sizeof(WAKE_SAM));
  size_t n = leerDurante(buf, sizeof(buf), 300);
  if (!contiene(buf, n, ACK, sizeof(ACK))) {
    Serial2.end();
    return false;
  }

  Serial2.write(GET_FW, sizeof(GET_FW));
  n = leerDurante(buf, sizeof(buf), 300);
  // Respuesta: ACK + 00 00 FF 06 FA D5 03 IC VER REV SUP DCS 00
  for (size_t i = 0; i + 6 <= n; i++) {
    if (buf[i] == 0xD5 && buf[i + 1] == 0x03) {
      Serial.printf("# PN5%02X detectado. Firmware %d.%d (RX=GPIO%d, TX=GPIO%d)\n",
                    buf[i + 2], buf[i + 3], buf[i + 4], rx, tx);
      return true;
    }
  }
  Serial.printf("# PN532 responde ACK pero no su firmware (RX=GPIO%d, TX=GPIO%d)\n", rx, tx);
  return true;
}

void setup() {
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
  Serial.begin(BAUDIOS);
  delay(300);  // deja arrancar al PN532 (se alimenta a la vez que el ESP32)
  Serial.println();
  Serial.println("# ESP32 puente PN532 v1.0");

  if (probarPN532(PIN_RX2, PIN_TX2)) {
    pn532Ok = true;
  } else if (probarPN532(PIN_TX2, PIN_RX2)) {
    // Funciona con los cables cruzados: se usa así y se avisa
    pn532Ok = true;
    rxUsado = PIN_TX2;
    txUsado = PIN_RX2;
    Serial.println("# AVISO: TX y RX están intercambiados en el cableado. Funciona igual, pero conviene corregirlo.");
  } else {
    Serial.println("# ERROR: el PN532 no responde. Revisa interruptores (HSU = 0 0), VCC/GND y TX/RX.");
    Serial2.begin(BAUDIOS, SERIAL_8N1, PIN_RX2, PIN_TX2);
  }

  while (Serial2.available()) Serial2.read();
  Serial.println("# Puente listo");
  Serial.flush();
  digitalWrite(PIN_LED, pn532Ok ? HIGH : LOW);
}

void loop() {
  uint8_t buf[256];

  // PC -> PN532. Se envía en bloque para que el preámbulo de despertar y el comando
  // lleguen seguidos (si hay una pausa, el PN532 vuelve a dormirse).
  size_t n = Serial.available();
  if (n) {
    n = Serial.readBytes(buf, min(n, sizeof(buf)));
    Serial2.write(buf, n);
    ultimoTrafico = millis();
  }

  // PN532 -> PC
  n = Serial2.available();
  if (n) {
    n = Serial2.readBytes(buf, min(n, sizeof(buf)));
    Serial.write(buf, n);
    ultimoTrafico = millis();
  }

  // LED de estado
  unsigned long ahora = millis();
  if (ahora - ultimoTrafico < 100) {
    digitalWrite(PIN_LED, (ahora / 25) % 2);
  } else if (pn532Ok) {
    digitalWrite(PIN_LED, HIGH);
  } else {
    digitalWrite(PIN_LED, (ahora / 500) % 2);
  }
}
