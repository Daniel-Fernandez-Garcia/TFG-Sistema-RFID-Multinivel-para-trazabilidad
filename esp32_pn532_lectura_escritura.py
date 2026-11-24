"""
Prueba de LECTURA y ESCRITURA de tarjetas NFC/RFID HF (13,56 MHz) con un PN532
conectado a un ESP32, que hace de puente USB <-> UART (modo HSU) con el PC.

Hardware
--------
- Módulo PN532 NFC (Hailege, Amazon B0CJY2GQZ8) con los interruptores en modo HSU (0 0).
- ESP32-WROOM-32 (ELEGOO, Amazon B0D8T5XD3P) con el firmware esp32_puente_pn532.ino.
  El ESP32 reenvía los bytes sin tocarlos, así que el protocolo es el mismo que en
  Pruebas_PN532/pn532_lectura_escritura.py.
- Puerto: el del ESP32 (se detecta solo; si no, --port COMx) a 115200 baudios.
- Si no responde:  python esp32_pn532_lectura_escritura.py --diagnostico

Requisitos
----------
    pip install pyserial

Uso
---
    python esp32_pn532_lectura_escritura.py                 # detecta el puerto y pide el texto
    python esp32_pn532_lectura_escritura.py --texto "Hola TFG"
    python esp32_pn532_lectura_escritura.py --port COM5 --solo-leer

Tarjetas soportadas por este script
-----------------------------------
- MIFARE Classic 1K/4K (SAK 0x08 / 0x18): escribe en los bloques de datos de los
  sectores 1 y 2 (4, 5, 6, 8, 9, 10) con la clave A de fábrica FF FF FF FF FF FF.
  Nunca escribe en los bloques "trailer" (7, 11), que guardan las claves.
- MIFARE Ultralight / NTAG213/215/216 (SAK 0x00): escribe en las páginas de usuario 4..15.
  OJO: esto sobrescribe el mensaje NDEF que tuviera la etiqueta (un móvil dejará de
  verla como "URL/texto" hasta que se vuelva a formatear como NDEF).

El texto se guarda como UTF-8 terminado en 0x00 y relleno con ceros.
"""

import argparse
import sys
import time

try:
    import serial  # pyserial
    from serial.tools import list_ports
except ImportError:
    sys.exit("Falta pyserial. Instálalo con:  pip install pyserial")


# --------------------------------------------------------------------------
# Constantes del protocolo PN532 (manual de usuario UM0701-02 de NXP)
# --------------------------------------------------------------------------
PN532_HOST_TO_PN532 = 0xD4
PN532_PN532_TO_HOST = 0xD5
ACK_FRAME = bytes([0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00])

CMD_GET_FIRMWARE_VERSION = 0x02
CMD_SAM_CONFIGURATION = 0x14
CMD_RF_CONFIGURATION = 0x32
CMD_IN_LIST_PASSIVE_TARGET = 0x4A
CMD_IN_DATA_EXCHANGE = 0x40
CMD_IN_RELEASE = 0x52

# Comandos que el PN532 reenvía a la tarjeta dentro de InDataExchange
MIFARE_AUTH_A = 0x60
MIFARE_READ = 0x30          # Classic: lee 1 bloque (16 B) | Ultralight/NTAG: lee 4 páginas (16 B)
MIFARE_WRITE_CLASSIC = 0xA0  # escribe 1 bloque de 16 B
ULTRALIGHT_WRITE = 0xA2      # escribe 1 página de 4 B

CLAVE_FABRICA = bytes([0xFF] * 6)
BLOQUES_CLASSIC = [4, 5, 6, 8, 9, 10]       # 6 x 16 B = 96 B (sin trailers 7 y 11)
PAGINAS_ULTRALIGHT = list(range(4, 16))     # 12 x 4 B = 48 B

# Chips USB-serie habituales en placas ESP32 (VID, PID)
USB_SERIE_ESP32 = {
    (0x10C4, 0xEA60): "CP210x",
    (0x1A86, 0x7523): "CH340",
    (0x1A86, 0x55D4): "CH9102",
}


class PN532Error(Exception):
    pass


def detectar_puerto():
    """Devuelve el COM de la primera placa con chip USB-serie típico de ESP32, o None."""
    for p in list_ports.comports():
        chip = USB_SERIE_ESP32.get((p.vid, p.pid))
        if chip:
            print(f"Usando {p.device} ({chip}: {p.description})")
            return p.device
    return None


def abrir_puerto(port, baudrate):
    """
    Abre el puerto con DTR y RTS desactivados ANTES de abrir: en las placas ESP32 esas
    líneas van a EN (reset) y a GPIO0 (modo programación). Si se activan, el ESP32 se
    reinicia o se queda en el cargador y deja de hacer de puente.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baudrate
    ser.timeout = 0.1
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def reiniciar_esp32(ser):
    """Pulso de reset por RTS (EN a nivel bajo) con GPIO0 alto: arranque normal."""
    ser.dtr = False
    ser.rts = True
    time.sleep(0.1)
    ser.rts = False


class PN532UART:
    """Driver mínimo del PN532 por UART (HSU) a través del puente ESP32, usando solo pyserial."""

    def __init__(self, port, baudrate=115200, debug=False):
        self.debug = debug
        try:
            self.ser = abrir_puerto(port, baudrate)
        except serial.SerialException as e:
            raise PN532Error(
                f"No se pudo abrir {port}: {e}\n"
                "¿Está otro programa (Arduino IDE, monitor serie, PuTTY) usando el puerto?"
            )
        self._esperar_arranque()
        self._wakeup()

    # ---------------- capa de tramas ----------------
    def _log(self, prefijo, datos):
        if self.debug:
            print(f"  {prefijo} {datos.hex(' ').upper()}")

    def _esperar_arranque(self):
        """
        Si al abrir el puerto el ESP32 se ha reiniciado (algunos drivers mueven DTR/RTS
        un instante), llegarán sus mensajes de arranque: se espera a que acabe y se descartan.
        """
        time.sleep(0.3)
        if self.ser.in_waiting:
            fin = time.time() + 3
            texto = b""
            while time.time() < fin and b"# Puente listo" not in texto:
                texto += self.ser.read(256)
            if self.debug:
                for linea in texto.decode("utf-8", errors="replace").splitlines():
                    if linea.startswith("#"):
                        print(f"  ESP32 {linea}")
        self.ser.reset_input_buffer()

    def _wakeup(self):
        """
        En HSU el PN532 arranca en modo LowVbat (dormido). Según el manual UM0701 hay que
        despertarlo con 0x55 + preámbulo largo y el PRIMER comando debe ser SAMConfiguration;
        si el primero es otro (p. ej. GetFirmwareVersion) el PN532 lo ignora y no envía ACK.
        """
        # El preámbulo de despertar y SAMConfiguration se envían en la MISMA escritura:
        # si hay una pausa entre ambos, el PN532 vuelve a dormirse y no contesta.
        ultimo_error = None
        for _ in range(3):
            try:
                self.comando(CMD_SAM_CONFIGURATION, bytes([0x01, 0x14, 0x01]), timeout=0.5,
                             prefijo=bytes([0x55, 0x55]) + bytes(14))
                return
            except PN532Error as e:
                ultimo_error = e
        raise PN532Error(
            f"{ultimo_error}\n"
            "Prueba el modo diagnóstico:  python esp32_pn532_lectura_escritura.py --diagnostico"
        )

    @staticmethod
    def construir_trama(cmd, params=b""):
        datos = bytes([PN532_HOST_TO_PN532, cmd]) + bytes(params)
        lng = len(datos)
        lcs = (~lng + 1) & 0xFF
        dcs = (~sum(datos) + 1) & 0xFF
        return bytes([0x00, 0x00, 0xFF, lng, lcs]) + datos + bytes([dcs, 0x00])

    def _leer_exacto(self, n, timeout):
        fin = time.time() + timeout
        buf = b""
        while len(buf) < n and time.time() < fin:
            buf += self.ser.read(n - len(buf))
        return buf

    def _esperar_inicio_trama(self, timeout):
        """Avanza hasta encontrar 00 00 FF y devuelve True si lo encuentra."""
        fin = time.time() + timeout
        ventana = b""
        while time.time() < fin:
            b = self.ser.read(1)
            if not b:
                continue
            ventana = (ventana + b)[-3:]
            if ventana == b"\x00\x00\xFF":
                return True
        return False

    def comando(self, cmd, params=b"", timeout=1.0, prefijo=b""):
        trama = prefijo + self.construir_trama(cmd, params)
        self._log(">>", trama)
        self.ser.reset_input_buffer()
        self.ser.write(trama)

        # 1) ACK
        if not self._esperar_inicio_trama(timeout):
            raise PN532Error("El PN532 no responde (sin ACK).")
        resto_ack = self._leer_exacto(3, timeout)
        if resto_ack != b"\x00\xFF\x00":
            raise PN532Error(f"ACK no válido: {resto_ack.hex()}")

        # 2) Respuesta
        if not self._esperar_inicio_trama(timeout):
            raise PN532Error("Tiempo de espera agotado esperando la respuesta del PN532.")
        cab = self._leer_exacto(2, timeout)
        if len(cab) < 2:
            raise PN532Error("Respuesta incompleta.")
        lng, lcs = cab[0], cab[1]
        if (lng + lcs) & 0xFF != 0:
            raise PN532Error("Checksum de longitud incorrecto.")
        cuerpo = self._leer_exacto(lng + 2, timeout)  # datos + DCS + postámbulo
        self._log("<<", b"\x00\x00\xFF" + cab + cuerpo)
        if len(cuerpo) < lng + 1:
            raise PN532Error("Respuesta incompleta.")
        datos, dcs = cuerpo[:lng], cuerpo[lng]
        if (sum(datos) + dcs) & 0xFF != 0:
            raise PN532Error("Checksum de datos incorrecto.")
        if datos[0] != PN532_PN532_TO_HOST or datos[1] != cmd + 1:
            raise PN532Error(f"Respuesta inesperada: {datos.hex()}")
        return datos[2:]

    # ---------------- comandos del PN532 ----------------
    def firmware(self):
        r = self.comando(CMD_GET_FIRMWARE_VERSION)
        return r[0], r[1], r[2], r[3]  # IC, Ver, Rev, Support

    def configurar(self):
        # Modo normal, timeout 50 ms x 0x14, usar IRQ
        self.comando(CMD_SAM_CONFIGURATION, bytes([0x01, 0x14, 0x01]))
        # MxRtyATR, MxRtyPSL, MxRtyPassiveActivation: pocos reintentos para no bloquear
        self.comando(CMD_RF_CONFIGURATION, bytes([0x05, 0xFF, 0x01, 0x10]))

    def buscar_tarjeta(self, timeout=1.0):
        """Devuelve (uid, sak, atqa) o None si no hay tarjeta ISO14443A en el campo."""
        try:
            # Libera cualquier tarjeta seleccionada antes, para que la nueva selección parta de cero
            self.comando(CMD_IN_RELEASE, bytes([0x00]))
        except PN532Error:
            pass
        try:
            r = self.comando(CMD_IN_LIST_PASSIVE_TARGET, bytes([0x01, 0x00]), timeout=timeout)
        except PN532Error:
            return None
        if not r or r[0] == 0:
            return None
        atqa = (r[2] << 8) | r[3]
        sak = r[4]
        uid_len = r[5]
        uid = bytes(r[6:6 + uid_len])
        return uid, sak, atqa

    def intercambio(self, datos):
        r = self.comando(CMD_IN_DATA_EXCHANGE, bytes([0x01]) + bytes(datos))
        estado = r[0]
        if estado & 0x3F != 0:
            raise PN532Error(f"La tarjeta devolvió error 0x{estado:02X}")
        return r[1:]

    # ---------------- MIFARE Classic ----------------
    def classic_autenticar(self, uid, bloque, clave=CLAVE_FABRICA):
        self.intercambio(bytes([MIFARE_AUTH_A, bloque]) + clave + uid[:4])

    def classic_leer(self, bloque):
        return self.intercambio(bytes([MIFARE_READ, bloque]))[:16]

    def classic_escribir(self, bloque, datos16):
        assert len(datos16) == 16
        if bloque == 0 or (bloque + 1) % 4 == 0:
            raise PN532Error(f"Bloque {bloque} protegido (fabricante o trailer). No se escribe.")
        self.intercambio(bytes([MIFARE_WRITE_CLASSIC, bloque]) + datos16)

    # ---------------- Ultralight / NTAG ----------------
    def ul_leer(self, pagina):
        return self.intercambio(bytes([MIFARE_READ, pagina]))[:16]  # 4 páginas

    def ul_escribir(self, pagina, datos4):
        assert len(datos4) == 4
        self.intercambio(bytes([ULTRALIGHT_WRITE, pagina]) + datos4)

    def cerrar(self):
        self.ser.close()


# --------------------------------------------------------------------------
# Lógica de la prueba
# --------------------------------------------------------------------------
def tipo_tarjeta(sak):
    if sak in (0x08, 0x18, 0x09, 0x88):
        return "classic"
    if sak == 0x00:
        return "ultralight"
    return "desconocida"


def leer_texto(pn, uid, tipo):
    if tipo == "classic":
        crudo = b""
        sector_auth = None
        for b in BLOQUES_CLASSIC:
            sector = b // 4
            if sector != sector_auth:
                pn.classic_autenticar(uid, b)
                sector_auth = sector
            crudo += pn.classic_leer(b)
    else:
        crudo = b""
        for p in PAGINAS_ULTRALIGHT[::4]:  # cada lectura devuelve 4 páginas
            crudo += pn.ul_leer(p)
        crudo = crudo[:len(PAGINAS_ULTRALIGHT) * 4]
    texto = crudo.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    return texto, crudo


def escribir_texto(pn, uid, tipo, texto):
    datos = texto.encode("utf-8") + b"\x00"
    if tipo == "classic":
        capacidad = len(BLOQUES_CLASSIC) * 16
    else:
        capacidad = len(PAGINAS_ULTRALIGHT) * 4
    if len(datos) > capacidad:
        raise PN532Error(f"Texto demasiado largo: {len(datos) - 1} B (máx. {capacidad - 1} B para esta tarjeta).")
    datos = datos.ljust(capacidad, b"\x00")

    if tipo == "classic":
        sector_auth = None
        for i, b in enumerate(BLOQUES_CLASSIC):
            sector = b // 4
            if sector != sector_auth:
                pn.classic_autenticar(uid, b)
                sector_auth = sector
            pn.classic_escribir(b, datos[i * 16:(i + 1) * 16])
    else:
        for i, p in enumerate(PAGINAS_ULTRALIGHT):
            pn.ul_escribir(p, datos[i * 4:(i + 1) * 4])


def esperar_tarjeta(pn, segundos=20):
    print(f"Acerca una tarjeta al lector (esperando hasta {segundos} s)...")
    fin = time.time() + segundos
    while time.time() < fin:
        t = pn.buscar_tarjeta(timeout=0.5)
        if t:
            return t
        time.sleep(0.1)
    return None


def diagnostico(port, baud):
    """
    Reinicia el ESP32 para ver su informe de arranque (firmware cargado, PN532 detectado,
    TX/RX cruzados...) y después manda al PN532 las tramas de prueba mostrando los bytes crudos.
    """
    wake = bytes([0x55, 0x55]) + bytes(14)
    sam = PN532UART.construir_trama(CMD_SAM_CONFIGURATION, bytes([0x01, 0x14, 0x01]))
    fw = PN532UART.construir_trama(CMD_GET_FIRMWARE_VERSION)
    print(f"Diagnóstico en {port} a {baud} baudios\n")
    try:
        s = abrir_puerto(port, baud)
    except serial.SerialException as e:
        sys.exit(f"No se pudo abrir {port}: {e}")

    # 1) Informe de arranque del ESP32
    print("[1] Reiniciando el ESP32...")
    s.reset_input_buffer()
    reiniciar_esp32(s)
    fin = time.time() + 4
    texto = b""
    while time.time() < fin and b"# Puente listo" not in texto:
        texto += s.read(256)
    lineas = [l for l in texto.decode("utf-8", errors="replace").splitlines() if l.startswith("#")]
    if lineas:
        for l in lineas:
            print(f"    {l}")
    else:
        print("    (sin mensajes del puente)")
        print("    -> El firmware esp32_puente_pn532.ino no está cargado, o el puerto/baudios no son los del ESP32.")

    # 2) Prueba directa del PN532 a través del puente
    print("\n[2] Prueba de tramas con el PN532")
    s.reset_input_buffer()
    s.write(wake + sam)
    time.sleep(0.3)
    r1 = s.read(64)
    s.write(fw)
    time.sleep(0.3)
    r2 = s.read(64)
    s.close()
    print(f"    SAM: {r1.hex(' ').upper() or '(nada)'}")
    print(f"    FW : {r2.hex(' ').upper() or '(nada)'}")
    print()
    if ACK_FRAME in r2 and b"\xD5\x03" in r2:
        print(f"El PN532 responde. Ejecuta:  python esp32_pn532_lectura_escritura.py --port {port}")
    else:
        print("El PN532 no ha respondido. Revisa:")
        print("  - Interruptores del PN532 en HSU (los dos en 0 / OFF) y reinicia tras cambiarlos.")
        print("  - VCC a 3V3 y GND a GND del ESP32.")
        print("  - TXD del PN532 -> GPIO16 (RX2) y RXD del PN532 -> GPIO17 (TX2).")
        print("Si sigue sin funcionar, copia esta salida y envíamela.")


def main():
    ap = argparse.ArgumentParser(description="Prueba de lectura/escritura con PN532 a través de un ESP32.")
    ap.add_argument("--port", help="Puerto del ESP32 (por defecto se detecta solo)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--texto", help="Texto a escribir (si no se indica, se pide por teclado)")
    ap.add_argument("--solo-leer", action="store_true", help="Solo leer, no escribir")
    ap.add_argument("--debug", action="store_true", help="Muestra las tramas en hexadecimal")
    ap.add_argument("--diagnostico", action="store_true", help="Reinicia el ESP32, muestra su informe y prueba el PN532")
    args = ap.parse_args()

    port = args.port or detectar_puerto()
    if not port:
        puertos = ", ".join(p.device for p in list_ports.comports()) or "ninguno"
        sys.exit(f"No se encontró el ESP32. Puertos disponibles: {puertos}. Indica uno con --port COMx")

    if args.diagnostico:
        diagnostico(port, args.baud)
        return

    try:
        pn = PN532UART(port, args.baud, debug=args.debug)
    except PN532Error as e:
        sys.exit(str(e))

    try:
        ic, ver, rev, sup = pn.firmware()
        print(f"PN5{ic:02X} encontrado. Firmware {ver}.{rev}  (soporte 0x{sup:02X})")
        pn.configurar()

        t = esperar_tarjeta(pn)
        if not t:
            sys.exit("No se detectó ninguna tarjeta.")
        uid, sak, atqa = t
        tipo = tipo_tarjeta(sak)
        print(f"Tarjeta detectada  UID={uid.hex(':').upper()}  ATQA=0x{atqa:04X}  SAK=0x{sak:02X}  -> {tipo}")
        if tipo == "desconocida":
            sys.exit("Tipo de tarjeta no soportado por esta prueba (¿DESFire / ISO14443-4?).")

        # 1) Lectura inicial
        antes, crudo = leer_texto(pn, uid, tipo)
        print(f"\n[1] Contenido ANTES : {antes!r}")
        if args.debug:
            print(f"    Crudo: {crudo.hex(' ').upper()}")
        if args.solo_leer:
            return

        # 2) Escritura
        texto = args.texto if args.texto is not None else input("\nTexto a escribir: ")
        print("[2] Escribiendo... no retires la tarjeta.")
        escribir_texto(pn, uid, tipo, texto)
        print("    Escritura enviada.")

        # 3) Relectura (se vuelve a seleccionar la tarjeta para leer desde cero)
        t2 = pn.buscar_tarjeta(timeout=1.0)
        if not t2 or t2[0] != uid:
            sys.exit("La tarjeta se movió o se retiró antes de verificar. Repite la prueba.")
        despues, _ = leer_texto(pn, uid, tipo)
        print(f"[3] Contenido DESPUÉS: {despues!r}")

        if despues == texto:
            print("\nOK: el contenido leído coincide con el texto escrito. Lectura y escritura verificadas.")
        else:
            print("\nFALLO: el contenido leído no coincide con el escrito.")
            sys.exit(1)

    except PN532Error as e:
        msg = str(e)
        if "error 0x14" in msg:
            msg += " (fallo de autenticación: la tarjeta no usa la clave de fábrica FF..FF)"
        sys.exit(f"Error: {msg}")
    except KeyboardInterrupt:
        print("\nCancelado.")
    finally:
        pn.cerrar()


if __name__ == "__main__":
    main()
