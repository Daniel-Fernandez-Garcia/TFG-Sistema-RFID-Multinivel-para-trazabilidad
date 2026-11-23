"""
Driver del PN532 por UART (HSU) usando solo pyserial.

Hardware: módulo PN532 NFC V2 con CH340 integrado (USB-C), COM9 a 115200 baudios.
Incluye las correcciones de la prueba de lectura/escritura:
  - Las líneas DTR/RTS del CH340 se abren desactivadas.
  - La señal de despertar y el primer comando (SAMConfiguration) se envían en una
    sola escritura, porque el PN532 vuelve a dormirse si hay una pausa entre ambos.

Tarjetas soportadas:
  - MIFARE Classic 1K/4K (SAK 0x08/0x18/0x88): bloques de datos de los sectores 1-3
    (4-6, 8-10, 12-14) con la clave A de fábrica FF..FF -> 144 bytes.
  - MIFARE Ultralight / NTAG21x (SAK 0x00): páginas de usuario desde la 4. La capacidad
    se lee del Capability Container (página 3): NTAG213 = 144 B, Ultralight = 48 B.

El texto se guarda en UTF-8 terminado en 0x00 y relleno con ceros.
"""

import random
import time

try:
    import serial  # pyserial
except ImportError:  # el modo simulado no lo necesita
    serial = None

# ---------------------------------------------------------------------------
# Constantes del protocolo (manual NXP UM0701-02)
# ---------------------------------------------------------------------------
# Byte de dirección (TFI) de cada trama: D4 = PC -> PN532, D5 = PN532 -> PC
HOST_TO_PN532 = 0xD4
PN532_TO_HOST = 0xD5
# Trama ACK: el PN532 la envía al recibir un comando bien formado; el PC la envía
# para abortar el comando en curso
ACK = bytes([0x00, 0x00, 0xFF, 0x00, 0xFF, 0x00])
# Señal de despertar en modo HSU: 0x55 0x55 y ceros (el chip arranca dormido)
WAKE = b"\x55\x55" + b"\x00" * 14

# Comandos del PN532 (la respuesta llega con el código del comando + 1)
CMD_FIRMWARE = 0x02
CMD_SAM = 0x14            # SAMConfiguration: primer comando tras despertar
CMD_RF_CONFIG = 0x32      # RFConfiguration: reintentos de búsqueda
CMD_LIST_TARGET = 0x4A    # InListPassiveTarget: busca una tarjeta y devuelve UID/SAK
CMD_DATA_EXCHANGE = 0x40  # InDataExchange: reenvía un comando a la tarjeta

# Comandos que se envían A LA TARJETA dentro de InDataExchange
MIFARE_AUTH_A = 0x60         # Classic: autenticar un sector con la clave A
MIFARE_READ = 0x30           # lee 16 B (1 bloque Classic o 4 páginas NTAG)
MIFARE_WRITE_CLASSIC = 0xA0  # Classic: escribe un bloque de 16 B
ULTRALIGHT_WRITE = 0xA2      # Ultralight/NTAG: escribe una página de 4 B

CLAVE_FABRICA = bytes([0xFF] * 6)
BLOQUES_CLASSIC = [4, 5, 6, 8, 9, 10, 12, 13, 14]  # nunca los trailers 7, 11, 15


# ---------------------------------------------------------------------------
# Excepciones
#   PN532Error         -> error de la tarjeta (se muestra al usuario sin reintentar)
#   ErrorComunicacion  -> el lector no contesta (LectorPN532 reabre el puerto y reintenta)
#   SinTarjeta         -> no hay tarjeta en el campo dentro del tiempo de espera
# ---------------------------------------------------------------------------
class PN532Error(Exception):
    pass


class ErrorComunicacion(PN532Error):
    """El PN532 no contesta o el puerto no se puede abrir (se reintenta reabriendo)."""


class SinTarjeta(PN532Error):
    pass


def tipo_tarjeta(sak):
    """Deduce la familia de la tarjeta a partir del SAK que devuelve InListPassiveTarget.
    Este texto decide cómo se lee/escribe y es el que se muestra junto al UID en la web."""
    if sak in (0x08, 0x18, 0x88, 0x09):
        return "MIFARE Classic"
    if sak == 0x00:
        return "Ultralight/NTAG"
    return "desconocida"


class PN532:
    """Comunicación de bajo nivel: construye tramas, las envía por el puerto serie y
    traduce las respuestas. Una instancia = un puerto abierto."""

    def __init__(self, port="COM9", baud=115200, debug=False, ser=None):
        self.debug = debug
        self._buf = bytearray()
        if ser is not None:
            # puerto ya abierto (lo usan las pruebas con un PN532 emulado)
            self.ser = ser
        else:
            if serial is None:
                raise PN532Error("Falta pyserial:  pip install pyserial")
            s = serial.Serial()
            s.port, s.baudrate, s.timeout = port, baud, 0.02
            # DTR/RTS a False ANTES de abrir: en algunas placas estas líneas del CH340
            # van al reset del PN532 y lo dejan bloqueado
            s.dtr = False
            s.rts = False
            try:
                s.open()
            except Exception as e:
                raise ErrorComunicacion(f"No se puede abrir {port}: {e}")
            self.ser = s
        self._despertar()

    # ------------------------------------------------------------------ tramas
    def _log(self, pref, data):
        if self.debug:
            print(f"{pref} {bytes(data).hex(' ').upper()}")

    @staticmethod
    def _trama(cmd, data=b""):
        # Formato: 00 00 FF | LEN | LCS | D4 CMD DATOS... | DCS | 00
        #   LCS = complemento a 2 de LEN  ->  (LEN + LCS) & 0xFF == 0
        #   DCS = complemento a 2 de la suma del cuerpo
        body = bytes([HOST_TO_PN532, cmd]) + bytes(data)
        n = len(body)
        return (bytes([0x00, 0x00, 0xFF, n, (0x100 - n) & 0xFF]) + body
                + bytes([(0x100 - sum(body)) & 0xFF, 0x00]))

    def _enviar(self, cmd, data=b"", wake=False):
        t = self._trama(cmd, data)
        if wake:
            # despertar + comando en UNA sola escritura: si hay pausa, vuelve a dormirse
            t = WAKE + t
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self._buf.clear()
        self._log(">>", t)
        self.ser.write(t)
        try:
            self.ser.flush()
        except Exception:
            pass

    def _leer_trama(self, timeout):
        """Acumula bytes en self._buf hasta tener una trama completa.
        Devuelve ("ACK", None) o ("DATA", cuerpo). Descarta basura y tramas con LCS mal."""
        fin = time.monotonic() + timeout
        while True:
            i = self._buf.find(b"\x00\xff")
            if i >= 0 and len(self._buf) >= i + 4:
                ln, lcs = self._buf[i + 2], self._buf[i + 3]
                if ln == 0 and lcs == 0xFF:
                    del self._buf[:i + 4]
                    return "ACK", None
                if (ln + lcs) & 0xFF != 0:
                    del self._buf[:i + 2]
                    continue
                if len(self._buf) >= i + 4 + ln + 1:
                    body = bytes(self._buf[i + 4:i + 4 + ln])
                    dcs = self._buf[i + 4 + ln]
                    del self._buf[:i + 5 + ln]
                    if (sum(body) + dcs) & 0xFF != 0:
                        raise PN532Error("Checksum incorrecto en la respuesta")
                    self._log("<<", body)
                    return "DATA", body
            if time.monotonic() > fin:
                raise PN532Error("timeout")
            chunk = self.ser.read(64)
            if chunk:
                self._buf += chunk

    def llamar(self, cmd, data=b"", timeout=1.0, wake=False):
        """Envía un comando y espera ACK + respuesta. Devuelve solo los datos útiles
        (sin D5 ni el código de respuesta)."""
        self._enviar(cmd, data, wake)
        try:
            tipo, _ = self._leer_trama(0.3 if not wake else 0.5)
        except PN532Error:
            raise ErrorComunicacion("El PN532 no responde (sin ACK)")
        if tipo != "ACK":
            raise PN532Error("Se esperaba ACK")
        try:
            tipo, body = self._leer_trama(timeout)
        except PN532Error:
            self.ser.write(ACK)  # aborta el comando en curso
            raise ErrorComunicacion("timeout esperando respuesta del PN532")
        if len(body) == 1 and body[0] == 0x7F:
            raise PN532Error("El PN532 devolvió error de sintaxis")
        if body[0] != PN532_TO_HOST or body[1] != cmd + 1:
            raise PN532Error("Respuesta inesperada")
        return body[2:]

    # --------------------------------------------------------------- arranque
    def _despertar(self):
        # SAMConfiguration(modo normal, timeout, usar IRQ); hasta 3 intentos
        ultimo = None
        for _ in range(3):
            try:
                self.llamar(CMD_SAM, bytes([0x01, 0x14, 0x01]), wake=True)
                break
            except PN532Error as e:
                ultimo = e
                time.sleep(0.1)
        else:
            raise ErrorComunicacion(f"No se pudo despertar el PN532: {ultimo}")
        # MxRtyPassiveActivation = 2 -> InListPassiveTarget vuelve enseguida si no hay tarjeta
        self.llamar(CMD_RF_CONFIG, bytes([0x05, 0xFF, 0x01, 0x02]))

    def firmware(self):
        r = self.llamar(CMD_FIRMWARE)
        return f"PN5{r[0]:02X} v{r[1]}.{r[2]}"

    def cerrar(self):
        try:
            self.ser.close()
        except Exception:
            pass

    # --------------------------------------------------------------- tarjetas
    def buscar_tarjeta(self, timeout=10.0):
        """Sondea el campo hasta encontrar una tarjeta ISO14443A (106 kbps).
        Respuesta de InListPassiveTarget: Nº tarjetas, Tg, ATQA(2), SAK, longitud UID, UID..."""
        fin = time.monotonic() + timeout
        fallos = 0
        while time.monotonic() < fin:
            try:
                r = self.llamar(CMD_LIST_TARGET, bytes([0x01, 0x00]), timeout=0.6)
                fallos = 0
            except ErrorComunicacion:
                fallos += 1
                if fallos >= 3:
                    raise
                continue
            if r and r[0] >= 1:
                atqa = (r[2] << 8) | r[3]
                sak = r[4]
                uid = bytes(r[6:6 + r[5]])
                return uid, sak, atqa
            time.sleep(0.05)
        raise SinTarjeta("No se detectó ninguna tarjeta. Acércala al lector y vuelve a intentarlo.")

    def _intercambio(self, datos, timeout=1.0):
        # El primer byte de la respuesta es el estado: 0x00 = OK, otro valor = error de la tarjeta
        r = self.llamar(CMD_DATA_EXCHANGE, bytes([0x01]) + bytes(datos), timeout)
        if r[0] & 0x3F:
            msg = f"error 0x{r[0] & 0x3F:02X} de la tarjeta"
            if r[0] & 0x3F == 0x14:
                msg += " (autenticación: la tarjeta no usa la clave de fábrica)"
            raise PN532Error(msg)
        return r[1:]

    # Classic: cada sector (4 bloques) exige autenticarse antes de leer/escribir.
    # La autenticación usa la clave A y los 4 primeros bytes del UID.
    def _auth(self, bloque, uid):
        self._intercambio(bytes([MIFARE_AUTH_A, bloque]) + CLAVE_FABRICA + uid[:4])

    def _paginas_ntag(self):
        # Página 3 = Capability Container. Si empieza por E1 (formato NDEF), su byte 2
        # indica la memoria de datos en unidades de 8 B (NTAG213: 0x12 -> 144 B = 36 páginas)
        cc = self._intercambio(bytes([MIFARE_READ, 3]))[:4]
        if cc[0] == 0xE1 and cc[2]:
            return list(range(4, 4 + cc[2] * 8 // 4))
        return list(range(4, 16))  # Ultralight sin formatear: 48 B

    def capacidad(self, uid, sak):
        if tipo_tarjeta(sak) == "MIFARE Classic":
            return len(BLOQUES_CLASSIC) * 16
        return len(self._paginas_ntag()) * 4

    def leer_crudo(self, uid, sak):
        """Lee toda la zona de datos de usuario y la devuelve como bytes."""
        tipo = tipo_tarjeta(sak)
        datos = bytearray()
        if tipo == "MIFARE Classic":
            sector = None
            for b in BLOQUES_CLASSIC:
                if b // 4 != sector:
                    sector = b // 4
                    self._auth(b, uid)
                datos += self._intercambio(bytes([MIFARE_READ, b]))[:16]
        elif tipo == "Ultralight/NTAG":
            paginas = self._paginas_ntag()
            for p in range(paginas[0], paginas[-1] + 1, 4):
                bloque = self._intercambio(bytes([MIFARE_READ, p]))[:16]
                datos += bloque[: min(16, (paginas[-1] + 1 - p) * 4)]
        else:
            raise PN532Error("Tipo de tarjeta no soportado (¿DESFire / ISO14443-4?)")
        return bytes(datos)

    def escribir_crudo(self, uid, sak, datos):
        """Escribe 'datos' en la zona de usuario, rellenando con ceros hasta su capacidad
        (así no quedan restos de un contenido anterior más largo)."""
        tipo = tipo_tarjeta(sak)
        cap = self.capacidad(uid, sak)
        datos = bytes(datos).ljust(cap, b"\x00")
        if tipo == "MIFARE Classic":
            sector = None
            for k, b in enumerate(BLOQUES_CLASSIC):
                if b // 4 != sector:
                    sector = b // 4
                    self._auth(b, uid)
                self._intercambio(bytes([MIFARE_WRITE_CLASSIC, b]) + datos[k * 16:(k + 1) * 16])
        elif tipo == "Ultralight/NTAG":
            for k, p in enumerate(self._paginas_ntag()):
                self._intercambio(bytes([ULTRALIGHT_WRITE, p]) + datos[k * 4:(k + 1) * 4])
        else:
            raise PN532Error("Tipo de tarjeta no soportado (¿DESFire / ISO14443-4?)")


def a_texto(crudo):
    # Los datos son UTF-8 terminados en 0x00: se corta en el primer cero
    return crudo.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


class LectorPN532:
    """Envoltorio de alto nivel usado por el servidor web.
    Abre el puerto la primera vez que se usa y ofrece estas operaciones:
      estado()                      -> firmware / disponibilidad del lector
      leer(timeout, excluir)        -> lee la primera tarjeta cuyo UID no esté en 'excluir'
      escribir(texto)               -> escribe y verifica releyendo (pestaña Inventario)
      escribir_simple(texto)        -> escribe sin verificar (Banco de pruebas mide cada paso aparte)
    Cada una devuelve un dict con uid, tarjeta (tipo, p. ej. "Ultralight/NTAG") y texto.
    reintentar=False desactiva el reintento automático tras un fallo de comunicación,
    para que el Banco de pruebas cuente ese fallo como error en vez de ocultarlo."""

    def __init__(self, port, baud=115200, debug=False):
        self.port, self.baud, self.debug = port, baud, debug
        self.pn = None

    def _conectar(self):
        if self.pn is None:
            self.pn = PN532(self.port, self.baud, self.debug)
        return self.pn

    def _cerrar(self):
        if self.pn:
            self.pn.cerrar()
        self.pn = None

    def _con_reintento(self, fn, reintentar=True):
        try:
            return fn(self._conectar())
        except ErrorComunicacion:
            # cierra el puerto; si se permite, lo reabre y repite una vez
            # (p. ej. si se desenchufó el lector)
            self._cerrar()
            if not reintentar:
                raise
            return fn(self._conectar())

    def estado(self):
        try:
            return {"ok": True, "detalle": f"{self._con_reintento(lambda pn: pn.firmware())} en {self.port}"}
        except PN532Error as e:
            self._cerrar()
            return {"ok": False, "detalle": str(e)}

    def leer(self, timeout=10.0, excluir=None, reintentar=True):
        # 'excluir' = UIDs ya procesados en una sesión de lecturas sucesivas: si la
        # tarjeta del campo es una de ellas se ignora (sin leer su memoria) y se
        # sigue esperando a otra hasta agotar el tiempo.
        def f(pn):
            fin = time.monotonic() + timeout
            while True:
                uid, sak, atqa = pn.buscar_tarjeta(max(0.05, fin - time.monotonic()))
                uid_txt = uid.hex(":").upper()
                if excluir and uid_txt in excluir:
                    if time.monotonic() >= fin:
                        raise SinTarjeta("Ninguna etiqueta nueva en el lector")
                    time.sleep(0.15)
                    continue
                texto = a_texto(pn.leer_crudo(uid, sak))
                return {"uid": uid_txt, "tarjeta": tipo_tarjeta(sak), "texto": texto}
        return self._con_reintento(f, reintentar)

    def escribir(self, texto, timeout=10.0):
        # Escribe y después vuelve a seleccionar la tarjeta y relee para verificar
        def f(pn):
            uid, sak, atqa = pn.buscar_tarjeta(timeout)
            datos = texto.encode("utf-8") + b"\x00"
            cap = pn.capacidad(uid, sak)
            if len(datos) > cap:
                raise PN532Error(f"Los datos ocupan {len(datos)} B y la tarjeta solo admite {cap} B")
            pn.escribir_crudo(uid, sak, datos)
            uid2, sak2, _ = pn.buscar_tarjeta(1.5)
            if uid2 != uid or a_texto(pn.leer_crudo(uid2, sak2)) != texto:
                raise PN532Error("La verificación tras escribir no coincide; repite sin mover la tarjeta")
            return {"uid": uid.hex(":").upper(), "tarjeta": tipo_tarjeta(sak), "texto": texto}
        return self._con_reintento(f)

    def escribir_simple(self, texto, timeout=2.0, reintentar=True):
        # Detecta la tarjeta y escribe, sin la relectura de verificación de escribir()
        def f(pn):
            uid, sak, atqa = pn.buscar_tarjeta(timeout)
            datos = texto.encode("utf-8") + b"\x00"
            cap = pn.capacidad(uid, sak)
            if len(datos) > cap:
                raise PN532Error(f"Los datos ocupan {len(datos)} B y la tarjeta solo admite {cap} B")
            pn.escribir_crudo(uid, sak, datos)
            return {"uid": uid.hex(":").upper(), "tarjeta": tipo_tarjeta(sak), "texto": texto}
        return self._con_reintento(f, reintentar)


class LectorSimulado:
    """Lector virtual para probar la web sin hardware (python servidor.py --simulado).
    Cada escribir() crea una tarjeta virtual nueva. leer() devuelve la más reciente
    cuyo UID no esté excluido, así las sesiones de lecturas sucesivas van recorriendo
    las tarjetas grabadas. escribir_simple() sobrescribe la última (Banco de pruebas)."""

    def __init__(self):
        self.tarjetas = []
        self.n = 0

    def estado(self):
        return {"ok": True, "detalle": "Lector SIMULADO"}

    def leer(self, timeout=10.0, excluir=None, reintentar=True):
        # lectura rápida en sesiones y en el Banco de pruebas; 0,6 s en Leer para ver el aviso
        rapida = excluir is not None or not reintentar
        time.sleep(random.uniform(0.04, 0.09) if rapida else 0.6)
        candidatas = self.tarjetas[::-1] or [
            {"uid": "04:00:00:00:00:00:01", "tarjeta": "Ultralight/NTAG", "texto": ""}]
        for t in candidatas:
            if not excluir or t["uid"] not in excluir:
                return dict(t)
        time.sleep(timeout)
        raise SinTarjeta("Ninguna etiqueta nueva en el lector")

    def escribir(self, texto, timeout=10.0):
        time.sleep(0.6)
        if len(texto.encode()) + 1 > 144:
            raise PN532Error("Los datos superan los 144 B de una NTAG213")
        self.n += 1
        t = {"uid": f"04:A1:B2:C3:{self.n >> 8:02X}:{self.n & 0xFF:02X}:80",
             "tarjeta": "Ultralight/NTAG", "texto": texto}
        self.tarjetas.append(t)
        return dict(t)

    def escribir_simple(self, texto, timeout=2.0, reintentar=True):
        time.sleep(random.uniform(0.08, 0.16))
        if len(texto.encode()) + 1 > 144:
            raise PN532Error("Los datos superan los 144 B de una NTAG213")
        if not self.tarjetas:
            return self.escribir(texto)
        self.tarjetas[-1]["texto"] = texto
        return dict(self.tarjetas[-1])
