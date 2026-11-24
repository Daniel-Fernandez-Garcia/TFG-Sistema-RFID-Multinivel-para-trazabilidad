/*
 * Lector NFC autónomo: el ESP32 lee las tarjetas con el PN532 (modo HSU) y envía su
 * contenido por Bluetooth Classic (puerto serie SPP, nombre "ESP32-NFC").
 *
 * Mismo cableado y mismos interruptores que el puente (ver README):
 *   PN532 SDA/TXD -> GPIO16   PN532 SCL/RXD -> GPIO17   VCC -> 3V3   GND -> GND
 *   Interruptores del PN532 en HSU (0 0).
 *
 * Lee lo mismo que esp32_pn532_lectura_escritura.py:
 *   - MIFARE Classic 1K/4K: bloques 4, 5, 6, 8, 9, 10 con la clave A de fábrica FF..FF
 *   - MIFARE Ultralight / NTAG21x: páginas de usuario 4..15
 * y lo interpreta como texto UTF-8 terminado en 0x00.
 *
 * Cada tarjeta se envía UNA vez mientras sigue en el lector, como una línea JSON:
 *   {"evento":"lectura","uid":"04:A1:B2:C3:D4:E5:F6","atqa":"0x0044","sak":"0x00","tipo":"ultralight","texto":"Hola TFG"}
 *   {"evento":"error","uid":"...","tipo":"classic","error":"autenticacion"}
 *   {"evento":"retirada","uid":"..."}
 * Las líneas que empiezan por "#" son mensajes informativos. Todo se copia también por el USB.
 *
 * LED azul (GPIO2): fijo = PN532 listo, parpadeo lento = PN532 no responde,
 *                   destello = tarjeta leída.
 *
 * Placa en Arduino IDE: "ESP32 Dev Module". Si al compilar sale "Sketch too big",
 * elige Herramientas > Partition Scheme > "Huge APP (3MB No OTA/1MB SPIFFS)".
 */

#include <Arduino.h>
#include "BluetoothSerial.h"

#if !defined(CONFIG_BT_ENABLED) || !defined(CONFIG_BLUEDROID_ENABLED) || !defined(CONFIG_BT_SPP_ENABLED)
#error "Bluetooth Classic (SPP) no disponible: selecciona la placa 'ESP32 Dev Module'."
#endif

const char *NOMBRE_BT = "ESP32-NFC";
const int PIN_RX2 = 16;   // ESP32 recibe  <- TX  del PN532 (pin "SDA/TXD")
const int PIN_TX2 = 17;   // ESP32 envía   -> RX  del PN532 (pin "SCL/RXD")
const int PIN_LED = 2;
const uint32_t BAUDIOS = 115200;

// --------------------------------------------------------------------------
// Constantes del protocolo PN532 (manual de usuario UM0701-02 de NXP)
// --------------------------------------------------------------------------
const uint8_t PN532_HOST_TO_PN532 = 0xD4;
const uint8_t PN532_PN532_TO_HOST = 0xD5;

const uint8_t CMD_GET_FIRMWARE_VERSION = 0x02;
const uint8_t CMD_SAM_CONFIGURATION = 0x14;
const uint8_t CMD_RF_CONFIGURATION = 0x32;
const uint8_t CMD_IN_LIST_PASSIVE_TARGET = 0x4A;
const uint8_t CMD_IN_DATA_EXCHANGE = 0x40;
const uint8_t CMD_IN_RELEASE = 0x52;

const uint8_t MIFARE_AUTH_A = 0x60;
const uint8_t MIFARE_READ = 0x30;   // Classic: 1 bloque (16 B) | Ultralight/NTAG: 4 páginas (16 B)

const uint8_t CLAVE_FABRICA[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
const uint8_t BLOQUES_CLASSIC[] = {4, 5, 6, 8, 9, 10};   // 6 x 16 B = 96 B (sin trailers 7 y 11)
const uint8_t LECTURAS_ULTRALIGHT[] = {4, 8, 12};        // 3 x 4 páginas = páginas 4..15 = 48 B

const int FALLOS_PARA_RETIRADA = 3;   // búsquedas seguidas sin tarjeta para darla por retirada

struct Tarjeta {
  uint8_t uid[10];
  uint8_t uidLen;
  uint8_t sak;
  uint16_t atqa;
};

BluetoothSerial SerialBT;
HardwareSerial &PN = Serial2;

bool pn532Ok = false;
bool clienteAnterior = false;
bool hayTarjeta = false;
Tarjeta ultima;
int fallosSeguidos = 0;
unsigned long ultimoIntento = 0;

// --------------------------------------------------------------------------
// Capa de tramas
// --------------------------------------------------------------------------
// Avanza hasta encontrar 00 00 FF. Devuelve true si lo encuentra antes del timeout.
bool esperarInicioTrama(unsigned long timeout) {
  unsigned long fin = millis() + timeout;
  uint8_t v0 = 1, v1 = 1, v2 = 1;
  while ((long)(fin - millis()) > 0) {
    if (PN.available()) {
      v0 = v1;
      v1 = v2;
      v2 = PN.read();
      if (v0 == 0x00 && v1 == 0x00 && v2 == 0xFF) return true;
    }
  }
  return false;
}

bool leerExacto(uint8_t *buf, size_t n, unsigned long timeout) {
  unsigned long fin = millis() + timeout;
  size_t leidos = 0;
  while (leidos < n && (long)(fin - millis()) > 0) {
    if (PN.available()) buf[leidos++] = PN.read();
  }
  return leidos == n;
}

// Envía un comando y devuelve el número de bytes de respuesta (sin D5 y cmd+1), o -1 si falla.
// Con 'despertar' antepone el preámbulo 0x55 que saca al PN532 del modo LowVbat.
int comando(uint8_t cmd, const uint8_t *params, size_t np, uint8_t *resp, size_t maxResp,
            unsigned long timeout = 1000, bool despertar = false) {
  uint8_t trama[64];
  size_t n = 0;
  if (despertar) {
    trama[n++] = 0x55;
    trama[n++] = 0x55;
    for (int i = 0; i < 14; i++) trama[n++] = 0x00;
  }
  uint8_t lng = np + 2;
  uint8_t suma = PN532_HOST_TO_PN532 + cmd;
  trama[n++] = 0x00;
  trama[n++] = 0x00;
  trama[n++] = 0xFF;
  trama[n++] = lng;
  trama[n++] = (uint8_t)(~lng + 1);
  trama[n++] = PN532_HOST_TO_PN532;
  trama[n++] = cmd;
  for (size_t i = 0; i < np; i++) {
    trama[n++] = params[i];
    suma += params[i];
  }
  trama[n++] = (uint8_t)(~suma + 1);
  trama[n++] = 0x00;

  while (PN.available()) PN.read();
  PN.write(trama, n);  // preámbulo y comando en la misma escritura

  // 1) ACK
  uint8_t ack[3];
  if (!esperarInicioTrama(timeout)) return -1;
  if (!leerExacto(ack, 3, timeout) || ack[0] != 0x00 || ack[1] != 0xFF || ack[2] != 0x00) return -1;

  // 2) Respuesta
  uint8_t cab[2];
  if (!esperarInicioTrama(timeout)) return -1;
  if (!leerExacto(cab, 2, timeout) || (uint8_t)(cab[0] + cab[1]) != 0) return -1;
  uint8_t len = cab[0];
  uint8_t cuerpo[260];
  if (!leerExacto(cuerpo, len + 2, timeout)) return -1;  // datos + DCS + postámbulo
  uint8_t s = 0;
  for (int i = 0; i < len + 1; i++) s += cuerpo[i];
  if (s != 0) return -1;
  if (len < 2 || cuerpo[0] != PN532_PN532_TO_HOST || cuerpo[1] != cmd + 1) return -1;

  size_t nr = len - 2;
  if (nr > maxResp) nr = maxResp;
  memcpy(resp, cuerpo + 2, nr);
  return nr;
}

// --------------------------------------------------------------------------
// Comandos del PN532
// --------------------------------------------------------------------------
// Prueba a despertar al PN532 con esos pines y lo deja configurado si responde.
bool iniciarPN532(int rx, int tx) {
  PN.begin(BAUDIOS, SERIAL_8N1, rx, tx);
  delay(50);
  uint8_t r[32];
  const uint8_t sam[] = {0x01, 0x14, 0x01};   // modo normal, timeout 50 ms x 0x14, usar IRQ
  bool ok = false;
  for (int i = 0; i < 3 && !ok; i++) {
    ok = comando(CMD_SAM_CONFIGURATION, sam, sizeof(sam), r, sizeof(r), 300, true) >= 0;
  }
  if (!ok) {
    PN.end();
    return false;
  }
  if (comando(CMD_GET_FIRMWARE_VERSION, nullptr, 0, r, sizeof(r)) >= 4) {
    Serial.printf("# PN5%02X detectado. Firmware %d.%d (RX=GPIO%d, TX=GPIO%d)\n", r[0], r[1], r[2], rx, tx);
  }
  // MxRtyATR, MxRtyPSL, MxRtyPassiveActivation: pocos reintentos para no bloquear
  const uint8_t rf[] = {0x05, 0xFF, 0x01, 0x10};
  comando(CMD_RF_CONFIGURATION, rf, sizeof(rf), r, sizeof(r));
  return true;
}

bool buscarTarjeta(Tarjeta &t) {
  uint8_t r[64];
  // Libera cualquier tarjeta seleccionada antes, para que la nueva selección parta de cero
  const uint8_t rel[] = {0x00};
  comando(CMD_IN_RELEASE, rel, sizeof(rel), r, sizeof(r));

  const uint8_t lp[] = {0x01, 0x00};   // 1 tarjeta, ISO14443A a 106 kbps
  int n = comando(CMD_IN_LIST_PASSIVE_TARGET, lp, sizeof(lp), r, sizeof(r), 500);
  if (n < 6 || r[0] == 0) return false;
  t.atqa = (r[2] << 8) | r[3];
  t.sak = r[4];
  t.uidLen = r[5] > sizeof(t.uid) ? sizeof(t.uid) : r[5];
  if (n < 6 + t.uidLen) return false;
  memcpy(t.uid, r + 6, t.uidLen);
  return true;
}

// InDataExchange con la tarjeta 1. Devuelve los bytes de respuesta o -1 si la tarjeta da error.
int intercambio(const uint8_t *datos, size_t n, uint8_t *resp, size_t maxResp) {
  uint8_t p[32];
  uint8_t r[64];
  p[0] = 0x01;
  memcpy(p + 1, datos, n);
  int nr = comando(CMD_IN_DATA_EXCHANGE, p, n + 1, r, sizeof(r));
  if (nr < 1 || (r[0] & 0x3F) != 0) return -1;
  nr -= 1;
  if ((size_t)nr > maxResp) nr = maxResp;
  memcpy(resp, r + 1, nr);
  return nr;
}

// --------------------------------------------------------------------------
// Lectura del contenido
// --------------------------------------------------------------------------
const char *tipoTarjeta(uint8_t sak) {
  if (sak == 0x08 || sak == 0x18 || sak == 0x09 || sak == 0x88) return "classic";
  if (sak == 0x00) return "ultralight";
  return "desconocida";
}

// Rellena 'crudo' con los bloques/páginas de datos. Devuelve los bytes leídos o -1 (y 'error').
int leerContenido(const Tarjeta &t, const char *tipo, uint8_t *crudo, const char *&error) {
  uint8_t r[32];
  int total = 0;
  if (strcmp(tipo, "classic") == 0) {
    int sectorAuth = -1;
    for (uint8_t b : BLOQUES_CLASSIC) {
      int sector = b / 4;
      if (sector != sectorAuth) {
        uint8_t auth[12] = {MIFARE_AUTH_A, b};
        memcpy(auth + 2, CLAVE_FABRICA, 6);
        memcpy(auth + 8, t.uid, 4);
        if (intercambio(auth, sizeof(auth), r, sizeof(r)) < 0) {
          error = "autenticacion";   // la tarjeta no usa la clave de fábrica FF..FF
          return -1;
        }
        sectorAuth = sector;
      }
      const uint8_t leer[] = {MIFARE_READ, b};
      if (intercambio(leer, sizeof(leer), r, sizeof(r)) < 16) {
        error = "lectura";
        return -1;
      }
      memcpy(crudo + total, r, 16);
      total += 16;
    }
  } else {
    for (uint8_t p : LECTURAS_ULTRALIGHT) {
      const uint8_t leer[] = {MIFARE_READ, p};
      if (intercambio(leer, sizeof(leer), r, sizeof(r)) < 16) {
        error = "lectura";
        return -1;
      }
      memcpy(crudo + total, r, 16);
      total += 16;
    }
  }
  return total;
}

// --------------------------------------------------------------------------
// Salida (USB + Bluetooth)
// --------------------------------------------------------------------------
String uidTexto(const Tarjeta &t) {
  String s;
  char hex[4];
  for (int i = 0; i < t.uidLen; i++) {
    snprintf(hex, sizeof(hex), i ? ":%02X" : "%02X", t.uid[i]);
    s += hex;
  }
  return s;
}

// Texto hasta el primer 0x00, escapado para JSON (los bytes UTF-8 se dejan tal cual).
String textoJson(const uint8_t *crudo, int n) {
  String s;
  char esc[8];
  for (int i = 0; i < n && crudo[i] != 0x00; i++) {
    uint8_t c = crudo[i];
    if (c == '"' || c == '\\') {
      s += '\\';
      s += (char)c;
    } else if (c < 0x20) {
      snprintf(esc, sizeof(esc), "\\u%04X", c);
      s += esc;
    } else {
      s += (char)c;
    }
  }
  return s;
}

void publicar(const String &linea) {
  Serial.println(linea);
  if (SerialBT.hasClient()) SerialBT.println(linea);
}

// Comienzo común de las líneas JSON de una tarjeta (sin cerrar la llave)
String lineaBase(const char *evento, const Tarjeta &t, const char *tipo) {
  char campos[48];
  snprintf(campos, sizeof(campos), "\",\"atqa\":\"0x%04X\",\"sak\":\"0x%02X\",\"tipo\":\"", t.atqa, t.sak);
  return String("{\"evento\":\"") + evento + "\",\"uid\":\"" + uidTexto(t) + campos + tipo + "\"";
}

void procesarTarjeta(const Tarjeta &t) {
  const char *tipo = tipoTarjeta(t.sak);
  if (strcmp(tipo, "desconocida") == 0) {
    publicar(lineaBase("error", t, tipo) + ",\"error\":\"tipo no soportado\"}");
    return;
  }

  uint8_t crudo[96];
  const char *error = nullptr;
  int n = leerContenido(t, tipo, crudo, error);
  if (n < 0) {
    publicar(lineaBase("error", t, tipo) + ",\"error\":\"" + error + "\"}");
  } else {
    publicar(lineaBase("lectura", t, tipo) + ",\"texto\":\"" + textoJson(crudo, n) + "\"}");
  }

  // Destello del LED para indicar la lectura
  digitalWrite(PIN_LED, LOW);
  delay(80);
  digitalWrite(PIN_LED, HIGH);
}

// --------------------------------------------------------------------------
void detectarPN532() {
  if (iniciarPN532(PIN_RX2, PIN_TX2)) {
    pn532Ok = true;
  } else if (iniciarPN532(PIN_TX2, PIN_RX2)) {
    pn532Ok = true;
    Serial.println("# AVISO: TX y RX están intercambiados en el cableado. Funciona igual, pero conviene corregirlo.");
  } else {
    pn532Ok = false;
    Serial.println("# ERROR: el PN532 no responde. Revisa interruptores (HSU = 0 0), VCC/GND y TX/RX.");
  }
  digitalWrite(PIN_LED, pn532Ok ? HIGH : LOW);
}

void setup() {
  pinMode(PIN_LED, OUTPUT);
  digitalWrite(PIN_LED, LOW);
  Serial.begin(BAUDIOS);
  delay(300);  // deja arrancar al PN532 (se alimenta a la vez que el ESP32)
  Serial.println();
  Serial.println("# ESP32 lector NFC Bluetooth v1.0");

  if (SerialBT.begin(NOMBRE_BT)) {
    Serial.printf("# Bluetooth activo como \"%s\". Empareja el dispositivo y abre su puerto serie.\n", NOMBRE_BT);
  } else {
    Serial.println("# ERROR: no se pudo iniciar el Bluetooth.");
  }

  detectarPN532();
  ultimoIntento = millis();
}

void loop() {
  // Avisos de conexión / desconexión del cliente Bluetooth
  bool cliente = SerialBT.hasClient();
  if (cliente != clienteAnterior) {
    clienteAnterior = cliente;
    Serial.println(cliente ? "# Cliente Bluetooth conectado" : "# Cliente Bluetooth desconectado");
    if (cliente) {
      SerialBT.println(pn532Ok ? "# ESP32-NFC listo. Acerca una tarjeta."
                               : "# ESP32-NFC: el PN532 no responde, revisa el cableado.");
    }
  }

  // Sin PN532: parpadeo lento y reintento cada 3 s
  if (!pn532Ok) {
    digitalWrite(PIN_LED, (millis() / 500) % 2);
    if (millis() - ultimoIntento > 3000) {
      ultimoIntento = millis();
      detectarPN532();
    }
    return;
  }

  Tarjeta t;
  if (buscarTarjeta(t)) {
    fallosSeguidos = 0;
    bool misma = hayTarjeta && t.uidLen == ultima.uidLen && memcmp(t.uid, ultima.uid, t.uidLen) == 0;
    if (!misma) {
      procesarTarjeta(t);
      ultima = t;
      hayTarjeta = true;
    }
  } else if (hayTarjeta && ++fallosSeguidos >= FALLOS_PARA_RETIRADA) {
    hayTarjeta = false;
    publicar(String("{\"evento\":\"retirada\",\"uid\":\"") + uidTexto(ultima) + "\"}");
  }
  delay(100);
}
