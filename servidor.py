"""
Base de datos web de etiquetas RFID (TFG - RFID Ballistic) · versión 2 (revisión).

Servidor local (biblioteca estándar + pyserial; openpyxl para Excel) que:
  - sirve la página web (index.html) en http://localhost:8000
  - guarda productos, envíos, usuarios, equipos y movimientos en SQLite (rfid.db)
  - lee y escribe etiquetas con el PN532 conectado por USB (COM9)

Pestañas de la web y su parte en este archivo:
  Explotación
  - Inventario y envíos   -> productos HF, envíos, lectura/escritura, añadir a envío por lectura, reset
  - Verificación en campo -> usuarios, su equipo (casco, chaleco, 2 placas), vinculación, verificación, tamper
  Pruebas
  - Banco de pruebas      -> pruebas de lectura/escritura repetidas (hilo en segundo plano)
  - Exportar a Excel (las tres pestañas) -> libros generados con exportar_excel.py (openpyxl)

Cambios de la versión 2:
  - Tablas separadas: productos (solo HF), envios, usuarios (antes personas) y equipos
    (una fila por usuario con sus cuatro huecos).
  - Fechas en ISO 8601 (YYYY-MM-DDThh:mm) en la base, en la web, en las etiquetas y en los Excel.
  - Nº de serie de 7 dígitos hexadecimales (0000000-FFFFFFF): ocupa los mismos 7 caracteres
    en la etiqueta que el formato anterior tipo matrícula (AA00000-ZZ99999).
  - Formato de etiqueta RFID2. Las etiquetas RFID1 grabadas antes se siguen leyendo y se
    convierten al vuelo (serie de matrícula -> hexadecimal, fecha -> ISO).
  - Las bases de la versión 1 se migran solas al arrancar (antes se guarda una copia).

Uso:
    python servidor.py                      # PN532 en COM9
    python servidor.py --port COM5          # otro puerto
    python servidor.py --simulado           # sin lector, para probar la web
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sqlite3
import sys
import threading
import time
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pn532 import LectorPN532, LectorSimulado, PN532Error, SinTarjeta

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "rfid.db"
PREFIJO = "RFID2"            # cabecera del formato actual de los datos grabados en la tag
PREFIJO_V1 = "RFID1"         # formato anterior (serie AA00000, fecha dd/mm/yyyy-hh:mm), solo lectura
TIPOS_PRODUCTO = {"CA": "Casco", "CH": "Chaleco", "PB": "Placa balística"}
RE_SERIE = re.compile(r"^[0-9A-F]{7}$")          # serie actual: 7 dígitos hexadecimales
RE_SERIE_V1 = re.compile(r"^[A-Z]{2}\d{5}$")     # serie de la versión 1 (tipo matrícula)
RE_FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")   # ISO 8601 con minutos
SERIE_TOTAL = 16 ** 7                            # 268.435.456 números (28 bits)
# Huecos del equipo de un usuario: columna de la tabla equipos, tipo de producto y nombre
HUECOS = (("casco_id", "CA", "Casco"), ("chaleco_id", "CH", "Chaleco"),
          ("placa1_id", "PB", "Placa 1"), ("placa2_id", "PB", "Placa 2"))
COLS_HUECO = [h[0] for h in HUECOS]

# El servidor atiende varias peticiones a la vez (ThreadingHTTPServer), así que:
#   lock_db      -> serializa las escrituras en SQLite
#   lock_lector  -> impide dos operaciones simultáneas sobre el PN532 (responde 409)
lock_db = threading.Lock()
lock_lector = threading.Lock()
lector = None   # LectorPN532 o LectorSimulado; se crea en main()


# ===================================================================== utilidades
def ahora():
    """Fecha y hora actuales en ISO 8601 con minutos: 2026-11-09T14:19 (se graba en la tag)."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M")


def ahora_seg():
    """ISO 8601 con segundos, para ordenar por última actividad."""
    return datetime.now().isoformat(timespec="seconds")


def fecha_v1_a_iso(fecha):
    """'dd/mm/yyyy-hh:mm' (versión 1) -> 'yyyy-mm-ddThh:mm'. Si ya es ISO la deja igual."""
    if not fecha:
        return fecha
    try:
        return datetime.strptime(fecha, "%d/%m/%Y-%H:%M").strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return fecha


def serie_v1_a_hex(s):
    """AA00000 -> 0000000 ... ZZ99999 -> 4077A3F. Mismo orden que la matrícula: primero
    los números, luego la letra de la derecha y por último la primera."""
    indice = ((ord(s[0]) - 65) * 26 + (ord(s[1]) - 65)) * 100000 + int(s[2:])
    return f"{indice:07X}"


def siguiente_serie(ultima):
    """Siguiente nº de serie hexadecimal; tras FFFFFFF vuelve a 0000000."""
    return "0000000" if not ultima else f"{(int(ultima, 16) + 1) % SERIE_TOTAL:07X}"


# ===================================================================== base de datos
def db():
    # Una conexión nueva por operación; row_factory permite acceder a las columnas por nombre.
    # foreign_keys = ON activa los ON DELETE de las claves ajenas (equipos, envíos).
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


ESQUEMA = """
-- Envíos: contenedor de productos. Guarda la etiqueta asociada (UHF; en el prototipo, una HF).
CREATE TABLE IF NOT EXISTS envios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    numero      TEXT NOT NULL UNIQUE,       -- nº de envío
    origen      TEXT NOT NULL,
    destino     TEXT NOT NULL,
    fecha       TEXT NOT NULL,              -- creación, ISO 8601 (YYYY-MM-DDThh:mm)
    uid         TEXT,                       -- UID de la etiqueta del envío
    tarjeta     TEXT,                       -- tipo de esa etiqueta
    actualizado TEXT NOT NULL               -- última actividad, ISO con segundos
);
-- Productos HF (casco, chaleco, placa). envio_id = envío que los contiene.
CREATE TABLE IF NOT EXISTS productos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    serie         TEXT NOT NULL UNIQUE,     -- 7 dígitos hexadecimales
    tipo_producto TEXT NOT NULL,            -- CA / CH / PB
    fecha         TEXT NOT NULL,            -- creación, ISO 8601
    envio_id      INTEGER REFERENCES envios(id) ON DELETE SET NULL,
    uid           TEXT,
    tarjeta       TEXT,
    tamper        INTEGER NOT NULL DEFAULT 1,   -- bit virtual: 1 correcto, 0 posiblemente comprometido
    ultima_verificacion           TEXT,         -- solo se muestra en el Excel
    ultima_verificacion_resultado TEXT,
    actualizado   TEXT NOT NULL
);
-- Usuarios que llevan un equipo (antes «personas»)
CREATE TABLE IF NOT EXISTS usuarios (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre         TEXT NOT NULL,
    apellidos      TEXT NOT NULL,
    identificacion TEXT UNIQUE COLLATE NOCASE,  -- DNI, TIP…; puede faltar en usuarios migrados
    fecha          TEXT NOT NULL,               -- alta, ISO 8601
    ultima_verificacion           TEXT,
    ultima_verificacion_resultado TEXT
);
-- Equipos: una fila por usuario con los productos de sus cuatro huecos.
-- UNIQUE en cada hueco: un producto no puede estar en dos equipos.
CREATE TABLE IF NOT EXISTS equipos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario_id  INTEGER NOT NULL UNIQUE REFERENCES usuarios(id) ON DELETE CASCADE,
    casco_id    INTEGER UNIQUE REFERENCES productos(id) ON DELETE SET NULL,
    chaleco_id  INTEGER UNIQUE REFERENCES productos(id) ON DELETE SET NULL,
    placa1_id   INTEGER UNIQUE REFERENCES productos(id) ON DELETE SET NULL,
    placa2_id   INTEGER UNIQUE REFERENCES productos(id) ON DELETE SET NULL,
    fecha       TEXT NOT NULL,
    actualizado TEXT NOT NULL
);
-- Registro de todo lo que pasa. tipo: HF (producto), UHF (envío) o USR (usuario)
CREATE TABLE IF NOT EXISTS movimientos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha       TEXT NOT NULL,              -- ISO 8601
    evento      TEXT NOT NULL,
    producto_id INTEGER,
    envio_id    INTEGER,
    usuario_id  INTEGER,
    tipo        TEXT,
    ref         TEXT,                       -- nº de serie, nº de envío o nombre del usuario
    uid         TEXT,
    tarjeta     TEXT,
    detalle     TEXT
);
CREATE TABLE IF NOT EXISTS config (clave TEXT PRIMARY KEY, valor TEXT);
-- Registros del Banco de pruebas. El reset de Inventario NO toca esta tabla.
CREATE TABLE IF NOT EXISTS pruebas (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    numero     INTEGER NOT NULL,
    nombre     TEXT NOT NULL,
    tipo       TEXT NOT NULL,
    parametros TEXT NOT NULL,               -- JSON
    resultados TEXT NOT NULL,               -- JSON
    fecha      TEXT NOT NULL                -- ISO 8601
);
"""


def crear_tablas():
    """Crea las tablas. Si la base es de la versión 1 (tabla personas, productos con UHF),
    antes guarda una copia (rfid_v1_copia.db) y la migra al esquema nuevo."""
    with db() as c:
        tablas = {r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "personas" in tablas:
        copia = BASE / "rfid_v1_copia.db"
        if not copia.exists():
            shutil.copy2(DB_PATH, copia)
        migrar_v1()
    with db() as c:
        c.executescript(ESQUEMA)


def migrar_v1():
    """Pasa una base de la versión 1 al esquema nuevo sin perder datos:
       - productos UHF -> envios; productos HF -> productos (serie hex, fecha ISO, envio_id)
       - personas -> usuarios (nombre / apellidos separados por el primer espacio) + equipos
       - movimientos y pruebas -> fecha ISO; series y PER -> nuevo formato"""
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = OFF")
    try:
        c.execute("BEGIN")
        for t in ("productos", "movimientos", "pruebas"):
            c.execute(f"ALTER TABLE {t} RENAME TO {t}_v1")
        for sentencia in re.sub(r"--[^\n]*", "", ESQUEMA).split(";"):
            if sentencia.strip():
                c.execute(sentencia)

        hexa = lambda s: serie_v1_a_hex(s) if s and RE_SERIE_V1.match(s) else s
        sustituir_series = lambda txt: re.sub(r"\b([A-Z]{2}\d{5})\b", lambda m: serie_v1_a_hex(m.group(1)), txt or "")

        viejos = [dict(r) for r in c.execute("SELECT * FROM productos_v1 ORDER BY id")]
        envio_nuevo = {}
        for p in viejos:
            if p["tipo"] == "UHF":
                cur = c.execute("INSERT INTO envios(numero,origen,destino,fecha,uid,tarjeta,actualizado) "
                                "VALUES(?,?,?,?,?,?,?)",
                                (p["envio"], p["origen"], p["destino"], fecha_v1_a_iso(p["fecha"]),
                                 p["uid"], p["tarjeta"], p["actualizado"]))
                envio_nuevo[p["id"]] = cur.lastrowid
        for p in viejos:
            if p["tipo"] == "HF":
                c.execute("INSERT INTO productos(id,serie,tipo_producto,fecha,envio_id,uid,tarjeta,tamper,"
                          "ultima_verificacion,ultima_verificacion_resultado,actualizado) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                          (p["id"], hexa(p["serie"]), p["tipo_producto"], fecha_v1_a_iso(p["fecha"]),
                           envio_nuevo.get(p["uhf_id"]), p["uid"], p["tarjeta"], p["tamper"],
                           fecha_v1_a_iso(p.get("ultima_verificacion")),
                           sustituir_series(p.get("ultima_verificacion_resultado")) or None, p["actualizado"]))

        personas = [dict(r) for r in c.execute("SELECT * FROM personas ORDER BY id")]
        nombre_a_usuario = {}
        for per in personas:
            partes = per["nombre"].split(" ", 1)
            nombre, apellidos = partes[0], (partes[1] if len(partes) > 1 else "")
            c.execute("INSERT INTO usuarios(id,nombre,apellidos,identificacion,fecha,ultima_verificacion,"
                      "ultima_verificacion_resultado) VALUES(?,?,?,?,?,?,?)",
                      (per["id"], nombre, apellidos, None, fecha_v1_a_iso(per["fecha"]),
                       fecha_v1_a_iso(per.get("ultima_verificacion")), per.get("ultima_verificacion_resultado")))
            nombre_a_usuario[per["nombre"]] = per["id"]
            # equipo: productos de la persona colocados en sus huecos
            suyos = sorted([p for p in viejos if p["tipo"] == "HF" and p.get("persona_id") == per["id"]],
                           key=lambda p: p["serie"])
            huecos, usados = {}, set()
            for col, tp, _ in HUECOS:
                prod = next((p for p in suyos if p["tipo_producto"] == tp and p["id"] not in usados), None)
                if prod:
                    usados.add(prod["id"])
                    huecos[col] = prod["id"]
            f = fecha_v1_a_iso(per["fecha"])
            c.execute("INSERT INTO equipos(usuario_id,casco_id,chaleco_id,placa1_id,placa2_id,fecha,actualizado) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (per["id"], huecos.get("casco_id"), huecos.get("chaleco_id"), huecos.get("placa1_id"),
                       huecos.get("placa2_id"), f, per["fecha_iso"]))

        tipo_viejo = {p["id"]: p["tipo"] for p in viejos}
        renombra = {"PERSONA_ALTA": "USUARIO_ALTA", "PERSONA_BAJA": "USUARIO_BAJA"}
        for m in c.execute("SELECT * FROM movimientos_v1 ORDER BY id").fetchall():
            m = dict(m)
            pid = m["producto_id"]
            es_envio = pid is not None and tipo_viejo.get(pid) == "UHF" or (pid is None and m["tipo"] == "UHF")
            c.execute("INSERT INTO movimientos(id,fecha,evento,producto_id,envio_id,usuario_id,tipo,ref,uid,tarjeta,detalle) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (m["id"], (m["fecha_iso"] or "")[:16] or fecha_v1_a_iso(m["fecha"]),
                       renombra.get(m["evento"], m["evento"]),
                       pid if pid is not None and not es_envio else None,
                       envio_nuevo.get(pid) if es_envio else None,
                       nombre_a_usuario.get(m["ref"]) if m["tipo"] == "PER" else None,
                       "USR" if m["tipo"] == "PER" else m["tipo"],
                       hexa(m["ref"]) if m["tipo"] == "HF" else m["ref"],
                       m["uid"], m["tarjeta"], sustituir_series(m["detalle"])))

        for p in c.execute("SELECT * FROM pruebas_v1 ORDER BY id").fetchall():
            p = dict(p)
            c.execute("INSERT INTO pruebas(id,numero,nombre,tipo,parametros,resultados,fecha) VALUES(?,?,?,?,?,?,?)",
                      (p["id"], p["numero"], p["nombre"], p["tipo"], p["parametros"], p["resultados"],
                       (p["fecha_iso"] or "")[:16] or fecha_v1_a_iso(p["fecha"])))

        r = c.execute("SELECT valor FROM config WHERE clave='ultima_serie'").fetchone()
        if r and RE_SERIE_V1.match(r["valor"] or ""):
            c.execute("UPDATE config SET valor=? WHERE clave='ultima_serie'", (serie_v1_a_hex(r["valor"]),))
        for t in ("productos_v1", "movimientos_v1", "pruebas_v1", "personas"):
            c.execute(f"DROP TABLE {t}")
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    finally:
        c.close()


# Tabla config: pares clave/valor ('ultima_serie', 'carpeta_exportacion').
def get_config(c, clave):
    r = c.execute("SELECT valor FROM config WHERE clave=?", (clave,)).fetchone()
    return r["valor"] if r else None


def set_config(c, clave, valor):
    c.execute("INSERT INTO config(clave,valor) VALUES(?,?) "
              "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor", (clave, valor))


def producto(c, pid):
    r = c.execute("SELECT * FROM productos WHERE id=?", (pid,)).fetchone()
    return dict(r) if r else None


def envio(c, eid):
    r = c.execute("SELECT * FROM envios WHERE id=?", (eid,)).fetchone()
    return dict(r) if r else None


def usuario(c, uid_):
    r = c.execute("SELECT * FROM usuarios WHERE id=?", (uid_,)).fetchone()
    return dict(r) if r else None


def nombre_completo(u):
    return f"{u['nombre']} {u['apellidos']}".strip()


# Añade una fila al registro de movimientos. Con prod / env / usr toma el tipo y la referencia
# del objeto y actualiza su marca 'actualizado' (ordena la lista de la web).
def registrar(c, evento, prod=None, env=None, usr=None, uid=None, detalle="", tipo=None, ref=None, tarjeta=None):
    marca = ahora_seg()
    if prod is not None:
        tipo, ref = "HF", prod["serie"]
        c.execute("UPDATE productos SET actualizado=? WHERE id=?", (marca, prod["id"]))
    elif env is not None:
        tipo, ref = "UHF", env["numero"]
        c.execute("UPDATE envios SET actualizado=? WHERE id=?", (marca, env["id"]))
    elif usr is not None:
        tipo, ref = "USR", nombre_completo(usr)
    c.execute("INSERT INTO movimientos(fecha,evento,producto_id,envio_id,usuario_id,tipo,ref,uid,tarjeta,detalle) "
              "VALUES(?,?,?,?,?,?,?,?,?,?)",
              (ahora(), evento, prod["id"] if prod else None, env["id"] if env else None,
               usr["id"] if usr else None, tipo, ref, uid, tarjeta, detalle))


# ===================================================================== formato en la tag
# Texto UTF-8 terminado en 0x00. 'RFID2' identifica las tags de esta web y la versión del formato.
#   producto: RFID2|HF|<serie hex>|<tipo>|<fecha ISO>              (36 bytes)
#   envío:    RFID2|UHF|<nº envío>|<origen>|<destino>|<fecha ISO>
def tag_producto(p):
    return f"{PREFIJO}|HF|{p['serie']}|{p['tipo_producto']}|{p['fecha']}"


def tag_envio(e):
    return f"{PREFIJO}|UHF|{e['numero']}|{e['origen']}|{e['destino']}|{e['fecha']}"


# Operación inversa: dict con los campos, o None si la tag no tiene un formato conocido.
# Las tags RFID1 (versión 1) se convierten al formato actual al leerlas.
def desde_tag(texto):
    partes = texto.split("|")
    if len(partes) < 5 or partes[0] not in (PREFIJO, PREFIJO_V1):
        return None
    v1 = partes[0] == PREFIJO_V1
    fecha = fecha_v1_a_iso(partes[-1]) if v1 else partes[-1]
    if not RE_FECHA.match(fecha):
        return None
    if partes[1] == "HF" and len(partes) == 5:
        serie = partes[2]
        if v1 and RE_SERIE_V1.match(serie):
            serie = serie_v1_a_hex(serie)
        elif not RE_SERIE.match(serie):
            return None
        return {"tipo": "HF", "serie": serie, "tipo_producto": partes[3], "fecha": fecha, "formato": partes[0]}
    if partes[1] == "UHF" and len(partes) == 6:
        return {"tipo": "UHF", "numero": partes[2], "origen": partes[3], "destino": partes[4],
                "fecha": fecha, "formato": partes[0]}
    return None


# ===================================================================== lógica
# Error que se devuelve a la web como {"error": msg} con el código HTTP indicado.
class ErrorAPI(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


# Valida un campo de texto. Se prohíbe '|' porque es el separador en la tag.
def limpiar(v, campo, maxlen=40, obligatorio=True):
    v = (v or "").strip()
    if not v:
        if obligatorio:
            raise ErrorAPI(f"El campo «{campo}» es obligatorio")
        return ""
    if "|" in v:
        raise ErrorAPI(f"El campo «{campo}» no puede contener el carácter |")
    if len(v) > maxlen:
        raise ErrorAPI(f"El campo «{campo}» admite como máximo {maxlen} caracteres")
    return v


# POST /api/productos {tipo_producto, tamper}. El nº de serie lo calcula el servidor.
def crear_producto(d):
    with lock_db, db() as c:
        tp = (d.get("tipo_producto") or "").strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", tp):
            raise ErrorAPI("El tipo de producto debe ser de dos letras (CA, CH, PB)")
        tamper = 0 if str(d.get("tamper", 1)) == "0" else 1   # por defecto 1 (correcto)
        serie = siguiente_serie(get_config(c, "ultima_serie"))
        for _ in range(1000):   # salta series ocupadas (p. ej. tras dar la vuelta en FFFFFFF)
            if not c.execute("SELECT 1 FROM productos WHERE serie=?", (serie,)).fetchone():
                break
            serie = siguiente_serie(serie)
        f = ahora()
        cur = c.execute("INSERT INTO productos(serie,tipo_producto,fecha,tamper,actualizado) VALUES(?,?,?,?,?)",
                        (serie, tp, f, tamper, ahora_seg()))
        set_config(c, "ultima_serie", serie)
        p = producto(c, cur.lastrowid)
        registrar(c, "CREADO", prod=p, detalle=f"{tp} · {TIPOS_PRODUCTO.get(tp, '')}"
                                                 + (" · tamper 0" if tamper == 0 else ""))
        return p


# POST /api/envios {numero, origen, destino}
def crear_envio(d):
    numero = limpiar(d.get("numero"), "Número de envío", 24)
    origen = limpiar(d.get("origen"), "Origen", 40)
    destino = limpiar(d.get("destino"), "Destino", 40)
    with lock_db, db() as c:
        if c.execute("SELECT 1 FROM envios WHERE numero=?", (numero,)).fetchone():
            raise ErrorAPI(f"Ya existe el envío {numero}")
        cur = c.execute("INSERT INTO envios(numero,origen,destino,fecha,actualizado) VALUES(?,?,?,?,?)",
                        (numero, origen, destino, ahora(), ahora_seg()))
        e = envio(c, cur.lastrowid)
        registrar(c, "CREADO", env=e, detalle=f"{origen} → {destino}")
        return e


# POST /api/asignar {producto_id, envio_id}. envio_id = None lo retira de su envío. Genera dos
# movimientos (ASIGNADO en el producto y CONTENIDO en el envío) para encontrarlo buscando cualquiera.
def asignar(pid, eid):
    with lock_db, db() as c:
        return _asignar(c, pid, eid)


# Núcleo de asignar() sin bloqueo propio: lo reutiliza también "Añadir por lectura",
# que ya tiene abierta la conexión y tomado lock_db (un Lock no se puede tomar dos veces).
def _asignar(c, pid, eid):
    p = producto(c, pid)
    if not p:
        raise ErrorAPI("Producto no encontrado", 404)
    if eid is None:
        if p["envio_id"] is None:
            return p
        antiguo = envio(c, p["envio_id"])
        c.execute("UPDATE productos SET envio_id=NULL WHERE id=?", (pid,))
        registrar(c, "DESASIGNADO", prod=p, detalle=f"retirado del envío {antiguo['numero'] if antiguo else '?'}")
    else:
        e = envio(c, eid)
        if not e:
            raise ErrorAPI("Envío no encontrado", 404)
        if p["envio_id"] == eid:
            return p
        c.execute("UPDATE productos SET envio_id=? WHERE id=?", (eid, pid))
        registrar(c, "ASIGNADO", prod=p, detalle=f"→ envío {e['numero']}")
        registrar(c, "CONTENIDO", env=e, detalle=f"+ {p['serie']} ({p['tipo_producto']})")
    return producto(c, pid)


# DELETE /api/productos/{id} y /api/envios/{id}. Solo borran de la base; la etiqueta no se toca.
# Al borrar un producto, su hueco en el equipo queda vacío (ON DELETE SET NULL).
def eliminar_producto(pid):
    with lock_db, db() as c:
        p = producto(c, pid)
        if not p:
            raise ErrorAPI("Producto no encontrado", 404)
        registrar(c, "ELIMINADO", prod=p)
        c.execute("DELETE FROM productos WHERE id=?", (pid,))


def eliminar_envio(eid):
    with lock_db, db() as c:
        e = envio(c, eid)
        if not e:
            raise ErrorAPI("Envío no encontrado", 404)
        n = c.execute("SELECT COUNT(*) n FROM productos WHERE envio_id=?", (eid,)).fetchone()["n"]
        registrar(c, "ELIMINADO", env=e, detalle=f"{n} productos liberados" if n else "")
        c.execute("UPDATE productos SET envio_id=NULL WHERE envio_id=?", (eid,))
        c.execute("DELETE FROM envios WHERE id=?", (eid,))


# Ejecuta una operación del lector bajo lock_lector y traduce sus errores a ErrorAPI (503).
def usar_lector(fn):
    if not lock_lector.acquire(timeout=1):
        raise ErrorAPI("El lector está ocupado con otra operación", 409)
    try:
        return fn()
    except PN532Error as e:
        raise ErrorAPI(str(e), 503)
    finally:
        lock_lector.release()


# Búsqueda de coincidencia de una lectura: primero por el dato leído (serie o nº de envío),
# luego por UID en productos y en envíos. Devuelve (clase, registro) con clase
# "producto" o "envio", o (None, None).
def buscar(c, datos, uid):
    if datos and datos["tipo"] == "HF":
        r = c.execute("SELECT * FROM productos WHERE serie=?", (datos["serie"],)).fetchone()
        if r:
            return "producto", dict(r)
    if datos and datos["tipo"] == "UHF":
        r = c.execute("SELECT * FROM envios WHERE numero=?", (datos["numero"],)).fetchone()
        if r:
            return "envio", dict(r)
    r = c.execute("SELECT * FROM productos WHERE uid=?", (uid,)).fetchone()
    if r:
        return "producto", dict(r)
    r = c.execute("SELECT * FROM envios WHERE uid=?", (uid,)).fetchone()
    if r:
        return "envio", dict(r)
    return None, None


# Da de alta en la base lo leído de una tag con formato válido. Devuelve (clase, registro).
def registrar_desde_tag(c, datos, lectura):
    marca = ahora_seg()
    if datos["tipo"] == "HF":
        cur = c.execute("INSERT INTO productos(serie,tipo_producto,fecha,uid,tarjeta,actualizado) VALUES(?,?,?,?,?,?)",
                        (datos["serie"], datos["tipo_producto"], datos["fecha"], lectura["uid"],
                         lectura["tarjeta"], marca))
        # si la serie leída es mayor que la última asignada, se avanza el contador
        ultima = get_config(c, "ultima_serie")
        if not ultima or int(datos["serie"], 16) > int(ultima, 16):
            set_config(c, "ultima_serie", datos["serie"])
        obj = producto(c, cur.lastrowid)
        registrar(c, "REGISTRADO", prod=obj, uid=lectura["uid"], tarjeta=lectura["tarjeta"],
                  detalle="nuevo, desde lectura de tag")
        return "producto", obj
    cur = c.execute("INSERT INTO envios(numero,origen,destino,fecha,uid,tarjeta,actualizado) VALUES(?,?,?,?,?,?,?)",
                    (datos["numero"], datos["origen"], datos["destino"], datos["fecha"], lectura["uid"],
                     lectura["tarjeta"], marca))
    obj = envio(c, cur.lastrowid)
    registrar(c, "REGISTRADO", env=obj, uid=lectura["uid"], tarjeta=lectura["tarjeta"],
              detalle="nuevo, desde lectura de tag")
    return "envio", obj


def _registrar_en(c, evento, clase, obj, **k):
    registrar(c, evento, **({"prod": obj} if clase == "producto" else {"env": obj}), **k)


# POST /api/leer. Devuelve uno de tres resultados a la web:
#   "coincide"    -> ya existe; la web pregunta sobrescribir / ignorar (resolver_lectura)
#   "sin_formato" -> tag vacía o ajena; solo se anota la lectura
#   "nuevo"       -> tag válida que no estaba en la base; se registra aquí mismo
def leer_tag():
    lectura = usar_lector(lambda: lector.leer())
    datos = desde_tag(lectura["texto"])
    with lock_db, db() as c:
        clase, existente = buscar(c, datos, lectura["uid"])
        if existente:
            _registrar_en(c, "LECTURA", clase, existente, uid=lectura["uid"], tarjeta=lectura["tarjeta"],
                          detalle="coincide con un registro"
                          + (" · etiqueta en formato RFID1" if datos and datos["formato"] == PREFIJO_V1 else ""))
            return {"resultado": "coincide", "lectura": lectura, "leido": datos, "clase": clase,
                    "existente": existente}
        if datos is None:
            registrar(c, "LECTURA", uid=lectura["uid"], tarjeta=lectura["tarjeta"],
                      detalle=f"tag sin formato: {lectura['texto'][:40]!r}" if lectura["texto"] else "tag vacía")
            return {"resultado": "sin_formato", "lectura": lectura}
        clase, obj = registrar_desde_tag(c, datos, lectura)
        return {"resultado": "nuevo", "lectura": lectura, "leido": datos, "clase": clase, "registro": obj}


# POST /api/lectura/resolver {accion, clase, id, leido, uid, tarjeta}. Segunda mitad de una
# lectura que coincidió: sobrescribir el registro con lo leído o ignorar la lectura.
def resolver_lectura(d):
    accion, clase, rid = d.get("accion"), d.get("clase"), d.get("id")
    leido, uid, tarjeta = d.get("leido"), d.get("uid"), d.get("tarjeta")
    if clase not in ("producto", "envio"):
        raise ErrorAPI("Clase no válida")
    tabla = "productos" if clase == "producto" else "envios"
    leer = producto if clase == "producto" else envio
    with lock_db, db() as c:
        obj = leer(c, rid)
        if not obj:
            raise ErrorAPI("Registro no encontrado", 404)
        if accion == "ignorar":
            _registrar_en(c, "IGNORADO", clase, obj, uid=uid, tarjeta=tarjeta,
                          detalle="se mantiene el registro existente")
            return obj
        if accion != "sobrescribir":
            raise ErrorAPI("Acción no válida")
        if clase == "producto" and leido and leido.get("tipo") == "HF":
            c.execute("UPDATE productos SET serie=?, tipo_producto=?, fecha=?, uid=?, tarjeta=? WHERE id=?",
                      (leido["serie"], leido["tipo_producto"], leido["fecha"], uid, tarjeta, rid))
        elif clase == "envio" and leido and leido.get("tipo") == "UHF":
            c.execute("UPDATE envios SET numero=?, origen=?, destino=?, fecha=?, uid=?, tarjeta=? WHERE id=?",
                      (leido["numero"], leido["origen"], leido["destino"], leido["fecha"], uid, tarjeta, rid))
        else:
            # solo coincidía el UID o cambia el tipo: se actualiza el UID
            c.execute(f"UPDATE {tabla} SET uid=?, tarjeta=? WHERE id=?", (uid, tarjeta, rid))
        obj = leer(c, rid)
        _registrar_en(c, "SOBRESCRITO", clase, obj, uid=uid, tarjeta=tarjeta,
                      detalle="registro actualizado con los datos de la tag")
        return obj


# POST /api/escribir {clase, id}. Graba el producto o el envío y guarda el UID y el tipo de la tag.
def escribir_tag(clase, rid):
    if clase not in ("producto", "envio"):
        raise ErrorAPI("Clase no válida")
    leer = producto if clase == "producto" else envio
    with lock_db, db() as c:
        obj = leer(c, rid)
    if not obj:
        raise ErrorAPI("Registro no encontrado", 404)
    texto = tag_producto(obj) if clase == "producto" else tag_envio(obj)
    r = usar_lector(lambda: lector.escribir(texto))
    with lock_db, db() as c:
        c.execute(f"UPDATE {'productos' if clase == 'producto' else 'envios'} SET uid=?, tarjeta=? WHERE id=?",
                  (r["uid"], r["tarjeta"], rid))
        obj = leer(c, rid)
        _registrar_en(c, "ESCRITURA", clase, obj, uid=r["uid"], tarjeta=r["tarjeta"],
                      detalle=f"{len(texto.encode())} B en {r['tarjeta']}")
    return {"clase": clase, "registro": obj, "escritura": r}


# ===================================================================== sesiones de lectura
# Las sesiones de lecturas sucesivas (Añadir por lectura, Vincular equipo, Verificación)
# las dirige la web: llama una y otra vez a un endpoint que lee UNA etiqueta con un
# tiempo de espera corto. 'excluir' lleva los UIDs ya procesados en esa sesión, para
# que la misma etiqueta apoyada en el lector no se cuente dos veces.
def leer_sesion(d):
    try:
        timeout = min(10.0, max(0.3, float(d.get("timeout") or 1.5)))
    except (TypeError, ValueError):
        timeout = 1.5
    excluir = set(d.get("excluir") or [])

    def f():
        try:
            return lector.leer(timeout, excluir=excluir)
        except SinTarjeta:
            return None          # no ha llegado ninguna etiqueta nueva: la web vuelve a llamar
    return usar_lector(f)


# POST /api/envios/{id}/leer_anadir. Lee un producto y lo mete en el envío.
#   estado: anadido | movido (estaba en otro envío) | ya (ya estaba) | rechazado | sin_tarjeta
def leer_anadir(eid, d):
    lectura = leer_sesion(d)
    if lectura is None:
        return {"estado": "sin_tarjeta"}
    datos = desde_tag(lectura["texto"])
    with lock_db, db() as c:
        e = envio(c, eid)
        if not e:
            raise ErrorAPI("Envío no encontrado", 404)
        res = {"lectura": lectura, "leido": datos}
        clase, prod = buscar(c, datos, lectura["uid"])
        if prod is None and datos is None:
            return {**res, "estado": "rechazado", "mensaje": "La etiqueta no contiene datos de producto"}
        if clase == "envio" or (datos and datos["tipo"] == "UHF"):
            return {**res, "estado": "rechazado", "mensaje": "Es la etiqueta de un envío, no un producto"}
        nuevo = prod is None
        if nuevo:                 # tag HF válida que no estaba en la base: se registra antes
            _, prod = registrar_desde_tag(c, datos, lectura)
        if prod["envio_id"] == eid:
            return {**res, "estado": "ya", "producto": prod, "mensaje": "Ya estaba en este envío"}
        anterior = envio(c, prod["envio_id"]) if prod["envio_id"] else None
        prod = _asignar(c, prod["id"], eid)
        if anterior:
            msg = f"Movido desde el envío {anterior['numero']}"
        else:
            msg = "Registrado y añadido" if nuevo else "Añadido al envío"
        return {**res, "estado": "movido" if anterior else "anadido", "producto": prod, "mensaje": msg}


# ===================================================================== usuarios y equipos
def equipo_de(c, usuario_id):
    """Fila de equipos del usuario (la crea vacía si no existe)."""
    r = c.execute("SELECT * FROM equipos WHERE usuario_id=?", (usuario_id,)).fetchone()
    if r is None:
        c.execute("INSERT INTO equipos(usuario_id,fecha,actualizado) VALUES(?,?,?)",
                  (usuario_id, ahora(), ahora_seg()))
        r = c.execute("SELECT * FROM equipos WHERE usuario_id=?", (usuario_id,)).fetchone()
    return dict(r)


def productos_del_equipo(c, usuario_id):
    eq = equipo_de(c, usuario_id)
    return [producto(c, eq[col]) for col in COLS_HUECO if eq[col]]


def equipo_completo(c, usuario_id):
    eq = equipo_de(c, usuario_id)
    return all(eq[col] for col in COLS_HUECO)


def dueno_de(c, pid):
    """(usuario, columna del hueco) del equipo que contiene el producto, o (None, None)."""
    r = c.execute("SELECT * FROM equipos WHERE ? IN (casco_id, chaleco_id, placa1_id, placa2_id)",
                  (pid,)).fetchone()
    if not r:
        return None, None
    col = next(col for col in COLS_HUECO if r[col] == pid)
    return usuario(c, r["usuario_id"]), col


def _datos_usuario(d):
    nombre = limpiar(d.get("nombre"), "Nombre", 40)
    apellidos = limpiar(d.get("apellidos"), "Apellidos", 60)
    ident = limpiar(d.get("identificacion"), "Identificación", 20).upper()
    return nombre, apellidos, ident


# POST /api/usuarios {nombre, apellidos, identificacion}. Crea también su fila de equipo vacía.
def crear_usuario(d):
    nombre, apellidos, ident = _datos_usuario(d)
    with lock_db, db() as c:
        if c.execute("SELECT 1 FROM usuarios WHERE identificacion=?", (ident,)).fetchone():
            raise ErrorAPI(f"Ya existe un usuario con la identificación {ident}")
        cur = c.execute("INSERT INTO usuarios(nombre,apellidos,identificacion,fecha) VALUES(?,?,?,?)",
                        (nombre, apellidos, ident, ahora()))
        u = usuario(c, cur.lastrowid)
        equipo_de(c, u["id"])
        registrar(c, "USUARIO_ALTA", usr=u, detalle=f"identificación {ident}")
        return u


# PUT-like: POST /api/usuarios/{id} {nombre, apellidos, identificacion}. Edita los datos.
def editar_usuario(uid_, d):
    nombre, apellidos, ident = _datos_usuario(d)
    with lock_db, db() as c:
        u = usuario(c, uid_)
        if not u:
            raise ErrorAPI("Usuario no encontrado", 404)
        if c.execute("SELECT 1 FROM usuarios WHERE identificacion=? AND id<>?", (ident, uid_)).fetchone():
            raise ErrorAPI(f"Ya existe un usuario con la identificación {ident}")
        c.execute("UPDATE usuarios SET nombre=?, apellidos=?, identificacion=? WHERE id=?",
                  (nombre, apellidos, ident, uid_))
        u = usuario(c, uid_)
        registrar(c, "USUARIO_EDITADO", usr=u, detalle=f"identificación {ident}")
        return u


# DELETE /api/usuarios/{id}. Su equipo se borra (ON DELETE CASCADE); los productos no.
def eliminar_usuario(uid_):
    with lock_db, db() as c:
        u = usuario(c, uid_)
        if not u:
            raise ErrorAPI("Usuario no encontrado", 404)
        n = len(productos_del_equipo(c, uid_))
        registrar(c, "USUARIO_BAJA", usr=u, detalle=f"{n} productos desvinculados" if n else "perfil eliminado")
        c.execute("DELETE FROM usuarios WHERE id=?", (uid_,))


# POST /api/desvincular {producto_id}: vacía el hueco del equipo que ocupa el producto.
def desvincular(pid):
    with lock_db, db() as c:
        p = producto(c, pid)
        if not p:
            raise ErrorAPI("Producto no encontrado", 404)
        u, col = dueno_de(c, pid)
        if not u:
            return p
        c.execute(f"UPDATE equipos SET {col}=NULL, actualizado=? WHERE usuario_id=?", (ahora_seg(), u["id"]))
        registrar(c, "DESVINCULADO", prod=p, detalle=f"retirado del equipo de {nombre_completo(u)}")
        return producto(c, pid)


# POST /api/usuarios/{id}/leer_vincular. Lee un producto y lo coloca en el hueco libre de su
# tipo (casco, chaleco, placa 1 o placa 2).
#   estado: vinculado | ya | rechazado | sin_tarjeta ;  completo = el equipo está lleno
def leer_vincular(uid_, d):
    lectura = leer_sesion(d)
    if lectura is None:
        return {"estado": "sin_tarjeta"}
    datos = desde_tag(lectura["texto"])
    with lock_db, db() as c:
        u = usuario(c, uid_)
        if not u:
            raise ErrorAPI("Usuario no encontrado", 404)
        res = {"lectura": lectura, "leido": datos}

        def fin(estado, mensaje, prod=None):
            return {**res, "estado": estado, "mensaje": mensaje, "producto": prod,
                    "completo": equipo_completo(c, uid_)}

        clase, prod = buscar(c, datos, lectura["uid"])
        if prod is None and datos is None:
            return fin("rechazado", "La etiqueta no contiene datos de producto")
        if clase == "envio" or (datos and datos["tipo"] == "UHF"):
            return fin("rechazado", "Es la etiqueta de un envío, no un producto del equipo")
        if prod is None:
            _, prod = registrar_desde_tag(c, datos, lectura)
        tp = prod["tipo_producto"]
        if tp not in ("CA", "CH", "PB"):
            return fin("rechazado", f"El tipo {tp} no forma parte del equipo (CA, CH, PB)", prod)
        dueno, _ = dueno_de(c, prod["id"])
        if dueno and dueno["id"] == uid_:
            return fin("ya", "Ya estaba en el equipo de este usuario", prod)
        if dueno:
            return fin("rechazado", f"Está en el equipo de {nombre_completo(dueno)}; desvincúlalo primero", prod)
        eq = equipo_de(c, uid_)
        libre = next((col for col, t, _ in HUECOS if t == tp and not eq[col]), None)
        if not libre:
            ocupados = [producto(c, eq[col])["serie"] for col, t, _ in HUECOS if t == tp and eq[col]]
            return fin("rechazado", f"Hueco de {TIPOS_PRODUCTO[tp].lower()} ya ocupado por {', '.join(ocupados)}", prod)
        c.execute(f"UPDATE equipos SET {libre}=?, actualizado=? WHERE usuario_id=?", (prod["id"], ahora_seg(), uid_))
        registrar(c, "VINCULADO", prod=prod, uid=lectura["uid"], tarjeta=lectura["tarjeta"],
                  detalle=f"→ equipo de {nombre_completo(u)}")
        return fin("vinculado", f"{TIPOS_PRODUCTO[tp]} vinculado", producto(c, prod["id"]))


# POST /api/usuarios/{id}/leer_verificar. Lee una etiqueta y la contrasta con el equipo
# registrado del usuario. Devuelve estado "ok" o "discordancia" con la lista de
# discordancias encontradas: [{tipo, mensaje}], donde tipo es uno de
#   sin_datos | uhf | no_registrado | tipo (tipo grabado ≠ registrado) |
#   uid (etiqueta distinta de la registrada) | no_asociado | otra_persona
# 'tamper' acompaña al producto para que la web avise si vale 0.
def leer_verificar(uid_, d):
    lectura = leer_sesion(d)
    if lectura is None:
        return {"estado": "sin_tarjeta"}
    datos = desde_tag(lectura["texto"])
    with lock_db, db() as c:
        u = usuario(c, uid_)
        if not u:
            raise ErrorAPI("Usuario no encontrado", 404)
        clase, prod = buscar(c, datos, lectura["uid"])
        dueno = None
        disc = []
        if prod is None and datos is None:
            disc.append({"tipo": "sin_datos", "mensaje": "La etiqueta no contiene datos de producto"})
        elif clase == "envio" or (datos and datos["tipo"] == "UHF"):
            disc.append({"tipo": "uhf", "mensaje": "Es la etiqueta de un envío, no un producto del equipo"})
            prod = None
        elif prod is None:
            disc.append({"tipo": "no_registrado",
                         "mensaje": f"El producto {datos['serie']} no está registrado en la base de datos"})
        else:
            dueno, _ = dueno_de(c, prod["id"])
            if dueno and dueno["id"] == uid_:
                if datos is None:
                    disc.append({"tipo": "sin_datos", "mensaje": "La etiqueta registrada para este producto "
                                                                 "ya no contiene sus datos"})
                elif datos["tipo_producto"] != prod["tipo_producto"]:
                    disc.append({"tipo": "tipo", "mensaje":
                                 f"No es del tipo registrado: la etiqueta dice {datos['tipo_producto']} "
                                 f"({TIPOS_PRODUCTO.get(datos['tipo_producto'], '?')}) y en la base es "
                                 f"{prod['tipo_producto']} ({TIPOS_PRODUCTO.get(prod['tipo_producto'], '?')})"})
                if prod["uid"] and prod["uid"] != lectura["uid"]:
                    disc.append({"tipo": "uid", "mensaje":
                                 f"El UID de la etiqueta ({lectura['uid']}) no es el registrado ({prod['uid']}): "
                                 "posible etiqueta sustituida o clonada"})
            elif dueno:
                disc.append({"tipo": "otra_persona",
                             "mensaje": f"Está en el equipo de otro usuario: {nombre_completo(dueno)}"})
            else:
                disc.append({"tipo": "no_asociado",
                             "mensaje": "No está asociado a este usuario (no pertenece a ningún equipo)"})
        propio = prod is not None and dueno is not None and dueno["id"] == uid_
        estado = "ok" if propio and not disc else "discordancia"
        tamper = prod["tamper"] if prod else None
        detalle = nombre_completo(u) + ": " + ("correcto" if estado == "ok" else "; ".join(x["mensaje"] for x in disc))
        if tamper == 0:
            detalle += " · tamper 0"
        if prod:
            registrar(c, "VERIFICADO" if estado == "ok" else "DISCORDANCIA", prod=prod,
                      uid=lectura["uid"], tarjeta=lectura["tarjeta"], detalle=detalle)
            # fecha y resultado de la última verificación del producto (solo se ven en el Excel)
            c.execute("UPDATE productos SET ultima_verificacion=?, ultima_verificacion_resultado=? WHERE id=?",
                      (ahora(), detalle, prod["id"]))
        else:
            registrar(c, "DISCORDANCIA", uid=lectura["uid"], tarjeta=lectura["tarjeta"], detalle=detalle,
                      tipo="HF" if datos and datos["tipo"] == "HF" else None,
                      ref=datos.get("serie") if datos else None)
        return {"estado": estado, "lectura": lectura, "leido": datos, "producto": prod,
                "discordancias": disc, "tamper": tamper}


# POST /api/usuarios/{id}/verificacion_fin {verificados, total, incidencias, completa}
# Deja constancia del resultado global de la verificación.
def verificacion_fin(uid_, d):
    with lock_db, db() as c:
        u = usuario(c, uid_)
        if not u:
            raise ErrorAPI("Usuario no encontrado", 404)
        txt = (f"{int(d.get('verificados', 0))}/{int(d.get('total', 0))} productos correctos, "
               f"{int(d.get('incidencias', 0))} incidencias"
               + ("" if d.get("completa") else " · verificación interrumpida"))
        registrar(c, "VERIF_FIN", usr=u, detalle=txt)
        c.execute("UPDATE usuarios SET ultima_verificacion=?, ultima_verificacion_resultado=? WHERE id=?",
                  (ahora(), txt, uid_))
        return {"ok": True}


# POST /api/productos/{id}/tamper {tamper: 0|1}. Para pruebas: marcar un HF como comprometido.
def cambiar_tamper(pid, d):
    valor = 0 if str(d.get("tamper")) == "0" else 1
    with lock_db, db() as c:
        p = producto(c, pid)
        if not p:
            raise ErrorAPI("Producto no encontrado", 404)
        if p["tamper"] != valor:
            c.execute("UPDATE productos SET tamper=? WHERE id=?", (valor, pid))
            registrar(c, "TAMPER", prod=p, detalle=f"tamper {p['tamper']} → {valor}"
                                                   + (" (posiblemente comprometido)" if valor == 0 else ""))
        return producto(c, pid)


# POST /api/reset {confirmacion: "RESET"}. Vacía productos, envíos, usuarios, equipos y movimientos
# y reinicia el nº de serie a 0000000. La tabla 'pruebas' (Banco de pruebas) se conserva.
def reset(d):
    if d.get("confirmacion") != "RESET":
        raise ErrorAPI("Confirmación incorrecta")
    with lock_db, db() as c:
        for t in ("equipos", "usuarios", "productos", "envios", "movimientos"):
            c.execute(f"DELETE FROM {t}")
        c.execute("DELETE FROM config WHERE clave='ultima_serie'")
        c.execute("DELETE FROM sqlite_sequence WHERE name IN "
                  "('equipos','usuarios','productos','envios','movimientos')")
        registrar(c, "RESET", detalle="base de datos vaciada; nº de serie reiniciado a 0000000")
    return {"ok": True}


# Consultas que devuelven los listados a la web
def listar_productos():
    """Productos HF con el usuario y el hueco del equipo en que están (si lo están)."""
    with lock_db, db() as c:
        filas = [dict(r) for r in c.execute("""
            SELECT p.*, e.usuario_id,
                   CASE p.id WHEN e.casco_id THEN 'casco_id' WHEN e.chaleco_id THEN 'chaleco_id'
                             WHEN e.placa1_id THEN 'placa1_id' WHEN e.placa2_id THEN 'placa2_id' END AS hueco
            FROM productos p
            LEFT JOIN equipos e ON p.id IN (e.casco_id, e.chaleco_id, e.placa1_id, e.placa2_id)
            ORDER BY p.actualizado DESC""")]
    return filas


def listar_usuarios():
    """Usuarios con los cuatro huecos de su equipo."""
    with lock_db, db() as c:
        return [dict(r) for r in c.execute("""
            SELECT u.*, e.casco_id, e.chaleco_id, e.placa1_id, e.placa2_id
            FROM usuarios u LEFT JOIN equipos e ON e.usuario_id = u.id
            ORDER BY u.apellidos COLLATE NOCASE, u.nombre COLLATE NOCASE""")]


# ===================================================================== banco de pruebas
# Una prueba se ejecuta en un hilo aparte que retiene lock_lector durante toda la prueba
# (el resto de funciones del lector responden 409 mientras tanto). La web consulta el
# progreso con GET /api/banco/estado y puede cancelar con POST /api/banco/cancelar.
#
# Tipos:
#   lectura           -> N lecturas seguidas de la misma tag
#   escritura_lectura -> N ciclos: escribir el mismo texto + leer y comprobar
#   datos_distintos   -> N ciclos: escribir un producto distinto cada vez (nº de serie
#                        hexadecimal que avanza y tipo CA/CH/PB alternado) + leer y comprobar
# Medidas: tiempo de cada operación (detección de la tag incluida), media/mín/máx,
# operaciones sin error y, en las de escritura, cuántas lecturas devolvieron lo escrito.
TIPOS_PRUEBA = {"lectura": "Lectura múltiple",
                "escritura_lectura": "Escritura y lectura múltiple",
                "datos_distintos": "Escritura y lectura múltiple con distintos datos"}
banco = {"activa": False, "cancelar": False}
lock_banco = threading.Lock()


def _stats(tiempos, total):
    # tiempos = lista de ms de las operaciones sin error
    if not total:
        return None
    return {"total": total, "ok": len(tiempos),
            "media_ms": round(sum(tiempos) / len(tiempos), 1) if tiempos else None,
            "min_ms": round(min(tiempos), 1) if tiempos else None,
            "max_ms": round(max(tiempos), 1) if tiempos else None}


def banco_estado():
    with lock_banco:
        return {k: v for k, v in banco.items() if not k.startswith("_")}


def banco_iniciar(d):
    tipo = d.get("tipo")
    if tipo not in TIPOS_PRUEBA:
        raise ErrorAPI("Tipo de prueba no válido")
    try:
        n = int(d.get("repeticiones"))
    except (TypeError, ValueError):
        raise ErrorAPI("Número de repeticiones no válido")
    if not 1 <= n <= 1000:
        raise ErrorAPI("Las repeticiones deben estar entre 1 y 1000")
    texto = (d.get("texto") or "PRUEBA-BANCO").strip()
    if tipo == "escritura_lectura" and len(texto.encode("utf-8")) > 47:
        raise ErrorAPI("El texto de prueba admite como máximo 47 bytes (cabe en cualquier tag soportada)")
    serie = (d.get("serie_inicial") or "0000000").strip().upper()
    if tipo == "datos_distintos" and not RE_SERIE.match(serie):
        raise ErrorAPI("La serie inicial debe tener 7 dígitos hexadecimales (p. ej. 0000000)")
    cfg = {"tipo": tipo, "repeticiones": n, "restaurar": bool(d.get("restaurar", True)),
           "distancia_cm": (str(d.get("distancia_cm") or "").strip()[:12]) or None,
           "angulo_grados": (str(d.get("angulo_grados") or "").strip()[:12]) or None,
           "notas": (str(d.get("notas") or "").strip()[:200]) or None}
    if tipo == "escritura_lectura":
        cfg["texto"] = texto
    if tipo == "datos_distintos":
        cfg["serie_inicial"] = serie
    with lock_banco:
        if banco["activa"]:
            raise ErrorAPI("Ya hay una prueba en curso", 409)
        if not lock_lector.acquire(timeout=1):
            raise ErrorAPI("El lector está ocupado con otra operación", 409)
        banco.clear()
        banco.update({"activa": True, "cancelar": False, "fase": "Esperando etiqueta", "hecho": 0,
                      "total": n, "tipo": tipo, "error": None, "prueba_id": None, "parcial": None})
    threading.Thread(target=_banco_hilo, args=(cfg,), daemon=True).start()
    return banco_estado()


def _banco_hilo(cfg):
    tipo, n = cfg["tipo"], cfg["repeticiones"]
    escribe = tipo != "lectura"
    t_lect, t_esc, errores = [], [], []
    n_lect = n_esc = coincide = ciclos = 0
    inicio = time.perf_counter()
    original = None
    restaurada = None

    def fase(txt, **extra):
        with lock_banco:
            banco.update({"fase": txt, **extra})

    def parcial():
        return {"lecturas": _stats(t_lect, n_lect), "escrituras": _stats(t_esc, n_esc),
                "verificadas": coincide if escribe else None}

    try:
        # Paso 0: localizar la etiqueta y guardar su contenido original (no se mide)
        try:
            original = lector.leer(10.0)
        except PN532Error as e:
            fase("Error", error=f"No se detectó la etiqueta: {e}")
            return
        uid = original["uid"]
        serie = cfg.get("serie_inicial")
        for i in range(n):
            with lock_banco:
                if banco["cancelar"]:
                    break
            fase(f"Ciclo {i + 1} de {n}", hecho=i, parcial=parcial())
            esperado = None
            if escribe:
                if tipo == "datos_distintos":
                    tp = ("CA", "CH", "PB")[i % 3]
                    esperado = f"{PREFIJO}|HF|{serie}|{tp}|{ahora()}"
                    serie = siguiente_serie(serie)
                else:
                    esperado = cfg["texto"]
                n_esc += 1
                t0 = time.perf_counter()
                try:
                    r = lector.escribir_simple(esperado, 2.0, reintentar=False)
                    if r["uid"] != uid:
                        raise PN532Error(f"Otra etiqueta en el lector ({r['uid']})")
                    t_esc.append((time.perf_counter() - t0) * 1000)
                except PN532Error as e:
                    errores.append({"ciclo": i + 1, "op": "escritura", "error": str(e)})
            n_lect += 1
            t0 = time.perf_counter()
            try:
                r = lector.leer(2.0, reintentar=False)
                dt = (time.perf_counter() - t0) * 1000
                if r["uid"] != uid:
                    raise PN532Error(f"Otra etiqueta en el lector ({r['uid']})")
                t_lect.append(dt)
                if escribe:
                    if r["texto"] == esperado:
                        coincide += 1
                    else:
                        errores.append({"ciclo": i + 1, "op": "verificación",
                                        "error": "Lo leído no coincide con lo escrito"})
            except PN532Error as e:
                errores.append({"ciclo": i + 1, "op": "lectura", "error": str(e)})
            ciclos = i + 1
        # Restaurar el contenido original de la tag (no se mide)
        if escribe and cfg["restaurar"]:
            fase("Restaurando el contenido original")
            try:
                lector.escribir_simple(original["texto"], 3.0)
                restaurada = True
            except PN532Error:
                restaurada = False
        with lock_banco:
            cancelada = banco["cancelar"]
        res = {"uid": uid, "tarjeta": original["tarjeta"], "ciclos": ciclos,
               "lecturas": _stats(t_lect, n_lect), "escrituras": _stats(t_esc, n_esc),
               "verificadas": coincide if escribe else None,
               "duracion_s": round(time.perf_counter() - inicio, 2),
               "cancelada": cancelada, "restaurada": restaurada,
               "errores": errores[:50], "n_errores": len(errores)}
        with lock_db, db() as c:
            num = c.execute("SELECT COALESCE(MAX(numero),0)+1 n FROM pruebas").fetchone()["n"]
            cur = c.execute("INSERT INTO pruebas(numero,nombre,tipo,parametros,resultados,fecha) "
                            "VALUES(?,?,?,?,?,?)",
                            (num, f"Prueba {num}", tipo, json.dumps(cfg, ensure_ascii=False),
                             json.dumps(res, ensure_ascii=False), ahora()))
            pid = cur.lastrowid
        fase("Terminada", hecho=ciclos, parcial=parcial(), prueba_id=pid)
    except Exception as e:  # noqa
        fase("Error", error=f"Error interno: {e}")
    finally:
        with lock_banco:
            banco["activa"] = False
        lock_lector.release()


def listar_pruebas():
    with lock_db, db() as c:
        filas = [dict(r) for r in c.execute("SELECT * FROM pruebas ORDER BY numero DESC")]
    for f in filas:
        f["parametros"] = json.loads(f["parametros"])
        f["resultados"] = json.loads(f["resultados"])
        f["tipo_nombre"] = TIPOS_PRUEBA.get(f["tipo"], f["tipo"])
    return filas


# ===================================================================== exportación a Excel
# Los Excel se guardan en una carpeta elegida la primera vez (config 'carpeta_exportacion'),
# con una subcarpeta por pestaña. Nombre: <pestaña con _>_YYYY-MM-DD_hh-mm-ss.xlsx (fecha ISO;
# los «:» de la hora no se admiten en nombres de archivo de Windows, por eso van «-»)
# El reset no borra esta configuración.
PESTANAS_EXPORT = {"inv": "Inventario y envíos", "ver": "Verificación en campo", "banco": "Banco de pruebas"}
lock_dialogo = threading.Lock()


def carpeta_exportacion():
    with lock_db, db() as c:
        ruta = get_config(c, "carpeta_exportacion")
    return ruta if ruta and Path(ruta).is_dir() else None


def fijar_carpeta(ruta):
    p = Path(str(ruta).strip().strip('"')).expanduser()
    if not p.is_absolute():
        raise ErrorAPI("Escribe una ruta completa, por ejemplo D:\\Clase\\TFG\\Exportaciones")
    try:
        p.mkdir(parents=True, exist_ok=True)
        for nombre in PESTANAS_EXPORT.values():      # las tres subcarpetas, una por pestaña
            (p / nombre).mkdir(exist_ok=True)
    except OSError as e:
        raise ErrorAPI(f"No se puede usar esa carpeta: {e}")
    with lock_db, db() as c:
        set_config(c, "carpeta_exportacion", str(p))
    return {"carpeta": str(p)}


# Abre la ventana nativa de «Seleccionar carpeta» en el PC donde corre el servidor (el mismo
# que el navegador, porque solo escucha en 127.0.0.1). Si no hay tkinter, la web pide la ruta a mano.
def elegir_carpeta_dialogo():
    if not lock_dialogo.acquire(blocking=False):
        raise ErrorAPI("Ya hay una ventana de selección de carpeta abierta", 409)
    try:
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError:
            raise ErrorAPI("No se puede abrir la ventana de selección (falta tkinter)", 501)
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)    # que no quede detrás del navegador
            root.update()
            actual = carpeta_exportacion()
            ruta = filedialog.askdirectory(parent=root, mustexist=False,
                                           title="Carpeta donde guardar las exportaciones a Excel",
                                           initialdir=actual or str(BASE.parent))
            root.destroy()
        except tk.TclError as e:
            raise ErrorAPI(f"No se pudo abrir la ventana de selección: {e}", 501)
    finally:
        lock_dialogo.release()
    if not ruta:
        return {"cancelado": True}
    return fijar_carpeta(ruta)


# POST /api/exportar/{inv|ver|banco}. Si aún no hay carpeta devuelve necesita_carpeta y la web la pide.
def exportar(pestana, d):
    try:
        import exportar_excel as X
    except ImportError:
        raise ErrorAPI("Falta la librería openpyxl. Cierra el servidor y vuelve a abrir iniciar.bat "
                       "(la instala), o ejecuta: .venv\\Scripts\\python.exe -m pip install openpyxl", 501)
    carpeta = carpeta_exportacion()
    if not carpeta:
        return {"necesita_carpeta": True}
    with lock_db, db() as c:
        if pestana == "inv":
            periodo = d.get("periodo") or "dia"
            if periodo not in ("dia", "semana", "mes"):
                raise ErrorAPI("Periodo no válido (día, semana o mes)")
            try:
                ref = date.fromisoformat(d["fecha"]) if d.get("fecha") else date.today()
            except ValueError:
                raise ErrorAPI("Fecha no válida")
            wb = X.libro_inventario(c, periodo, ref)
        elif pestana == "ver":
            wb = X.libro_verificacion(c)
        else:
            wb = X.libro_banco(c)
    destino = Path(carpeta) / PESTANAS_EXPORT[pestana]
    nombre = PESTANAS_EXPORT[pestana].replace(" ", "_") + "_" + datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".xlsx"
    try:
        destino.mkdir(exist_ok=True)
        wb.save(destino / nombre)
    except OSError as e:
        raise ErrorAPI(f"No se pudo guardar el Excel: {e}")
    return {"archivo": str(destino / nombre), "nombre": nombre, "carpeta": str(destino)}


# POST /api/exportar/abrir {ruta}. Muestra el archivo (o la carpeta) en el explorador de archivos.
# Solo se permiten rutas dentro de la carpeta de exportación.
def abrir_en_explorador(ruta):
    base = carpeta_exportacion()
    p = Path(str(ruta or base or "")).resolve()
    if not base or not p.exists() or not p.is_relative_to(Path(base).resolve()):
        raise ErrorAPI("Ruta no permitida", 403)
    try:
        if sys.platform == "win32":
            if p.is_file():
                subprocess.Popen(f'explorer /select,"{p}"')
            else:
                os.startfile(p)
        else:
            subprocess.Popen(["xdg-open", str(p if p.is_dir() else p.parent)])
    except OSError as e:
        raise ErrorAPI(f"No se pudo abrir el explorador: {e}")
    return {"ok": True}


# GET /api/estado. Lo consulta la web cada 20 s para el indicador de la barra superior.
def estado():
    with lock_db, db() as c:
        sig = siguiente_serie(get_config(c, "ultima_serie"))
    if lock_lector.acquire(timeout=0.2):
        try:
            est = lector.estado()
        finally:
            lock_lector.release()
    else:
        est = {"ok": True, "detalle": "Lector ocupado"}
    return {"lector": est, "siguiente_serie": sig, "tipos_producto": TIPOS_PRODUCTO}


# ===================================================================== HTTP
# Enrutador: cada petición HTTP pasa por _despachar, que mira método + ruta y llama a la
# función correspondiente. Todas las respuestas de /api son JSON.
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _despachar(self, metodo):
        ruta = self.path.split("?")[0]
        try:
            if metodo == "GET" and ruta in ("/", "/index.html"):
                data = (BASE / "index.html").read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if metodo == "GET" and ruta == "/api/estado":
                return self._json(estado())
            # --- Inventario y envíos
            if metodo == "GET" and ruta == "/api/productos":
                return self._json(listar_productos())
            if metodo == "GET" and ruta == "/api/envios":
                with lock_db, db() as c:
                    return self._json([dict(r) for r in c.execute("SELECT * FROM envios ORDER BY actualizado DESC")])
            if metodo == "GET" and ruta == "/api/movimientos":
                with lock_db, db() as c:
                    filas = [dict(r) for r in c.execute("SELECT * FROM movimientos ORDER BY id DESC LIMIT 500")]
                return self._json(filas)
            if metodo == "POST" and ruta == "/api/productos":
                return self._json(crear_producto(self._body()), 201)
            if metodo == "POST" and ruta == "/api/envios":
                return self._json(crear_envio(self._body()), 201)
            if metodo == "POST" and ruta == "/api/asignar":
                d = self._body()
                return self._json(asignar(int(d["producto_id"]), d.get("envio_id")))
            if metodo == "POST" and ruta == "/api/leer":
                return self._json(leer_tag())
            if metodo == "POST" and ruta == "/api/lectura/resolver":
                return self._json(resolver_lectura(self._body()))
            if metodo == "POST" and ruta == "/api/escribir":
                d = self._body()
                return self._json(escribir_tag(d.get("clase"), int(d["id"])))
            m = re.fullmatch(r"/api/productos/(\d+)", ruta)
            if metodo == "DELETE" and m:
                eliminar_producto(int(m.group(1)))
                return self._json({"ok": True})
            m = re.fullmatch(r"/api/envios/(\d+)", ruta)
            if metodo == "DELETE" and m:
                eliminar_envio(int(m.group(1)))
                return self._json({"ok": True})
            m = re.fullmatch(r"/api/productos/(\d+)/tamper", ruta)
            if metodo == "POST" and m:
                return self._json(cambiar_tamper(int(m.group(1)), self._body()))
            m = re.fullmatch(r"/api/envios/(\d+)/leer_anadir", ruta)
            if metodo == "POST" and m:
                return self._json(leer_anadir(int(m.group(1)), self._body()))
            if metodo == "POST" and ruta == "/api/reset":
                return self._json(reset(self._body()))
            # --- Verificación en campo: usuarios y equipos
            if metodo == "GET" and ruta == "/api/usuarios":
                return self._json(listar_usuarios())
            if metodo == "POST" and ruta == "/api/usuarios":
                return self._json(crear_usuario(self._body()), 201)
            if metodo == "POST" and ruta == "/api/desvincular":
                return self._json(desvincular(int(self._body()["producto_id"])))
            m = re.fullmatch(r"/api/usuarios/(\d+)(?:/(leer_vincular|leer_verificar|verificacion_fin))?", ruta)
            if m and metodo == "DELETE" and not m.group(2):
                eliminar_usuario(int(m.group(1)))
                return self._json({"ok": True})
            if m and metodo == "POST" and not m.group(2):
                return self._json(editar_usuario(int(m.group(1)), self._body()))
            if m and metodo == "POST" and m.group(2):
                fn = {"leer_vincular": leer_vincular, "leer_verificar": leer_verificar,
                      "verificacion_fin": verificacion_fin}[m.group(2)]
                return self._json(fn(int(m.group(1)), self._body()))
            # --- Exportación a Excel
            if metodo == "GET" and ruta == "/api/exportar/carpeta":
                return self._json({"carpeta": carpeta_exportacion()})
            if metodo == "POST" and ruta == "/api/exportar/carpeta":
                d = self._body()
                return self._json(fijar_carpeta(d["ruta"]) if d.get("ruta") else elegir_carpeta_dialogo())
            if metodo == "POST" and ruta == "/api/exportar/abrir":
                return self._json(abrir_en_explorador(self._body().get("ruta")))
            m = re.fullmatch(r"/api/exportar/(inv|ver|banco)", ruta)
            if metodo == "POST" and m:
                return self._json(exportar(m.group(1), self._body()))
            # --- Banco de pruebas
            if metodo == "GET" and ruta == "/api/pruebas":
                return self._json(listar_pruebas())
            if metodo == "GET" and ruta == "/api/banco/estado":
                return self._json(banco_estado())
            if metodo == "POST" and ruta == "/api/banco/iniciar":
                return self._json(banco_iniciar(self._body()))
            if metodo == "POST" and ruta == "/api/banco/cancelar":
                with lock_banco:
                    banco["cancelar"] = True
                return self._json(banco_estado())
            self._json({"error": "Ruta no encontrada"}, 404)
        except ErrorAPI as e:
            self._json({"error": str(e)}, e.code)
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            self._json({"error": f"Petición no válida: {e}"}, 400)
        except Exception as e:  # noqa
            self._json({"error": f"Error interno: {e}"}, 500)

    def do_GET(self):
        self._despachar("GET")

    def do_POST(self):
        self._despachar("POST")

    def do_DELETE(self):
        self._despachar("DELETE")


def main():
    global lector
    ap = argparse.ArgumentParser(description="Base de datos web de etiquetas RFID")
    ap.add_argument("--port", default="COM9", help="Puerto serie del PN532 (por defecto COM9)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--http", type=int, default=8000, help="Puerto de la web (por defecto 8000)")
    ap.add_argument("--simulado", action="store_true", help="Usa un lector virtual, sin hardware")
    ap.add_argument("--debug", action="store_true", help="Muestra las tramas del PN532")
    ap.add_argument("--no-navegador", action="store_true", help="No abrir el navegador al arrancar")
    args = ap.parse_args()

    crear_tablas()
    lector = LectorSimulado() if args.simulado else LectorPN532(args.port, args.baud, args.debug)
    est = lector.estado()
    print(f"Lector: {'OK' if est['ok'] else 'NO DISPONIBLE'} - {est['detalle']}")
    if not est["ok"]:
        print("  La web arranca igualmente; Leer/Escribir fallarán hasta conectar el lector.")

    srv = ThreadingHTTPServer(("127.0.0.1", args.http), Handler)
    url = f"http://localhost:{args.http}"
    print(f"Base de datos: {DB_PATH}\nWeb: {url}   (Ctrl+C para salir)")
    if not args.no_navegador:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor detenido.")
        sys.exit(0)


if __name__ == "__main__":
    main()
