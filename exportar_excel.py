"""
Exportación a Excel de las tres pestañas de la web (lo usa servidor.py).

Los tres libros comparten el mismo formato (clase Hoja):
  - fila 1: título del libro · fila 2: periodo / fecha de generación / totales
  - cabecera azul 2E75B6 con texto blanco en negrita, Arial 9, bordes medianos oscuros
  - datos en Arial 9, centrados en su columna, con bordes finos grises y bandas alternas azul claro / blanco
  - filas de grupo (un envío, un usuario) en azul más claro y negrita, que reinician las bandas
  - paneles fijados bajo la cabecera, anchos de columna fijos, impresión apaisada a una página de ancho
En la base de datos (y en las etiquetas) las fechas están en ISO 8601 (2026-11-09T14:19). En el Excel
se escriben como fechas reales de Excel, para que se puedan ordenar y filtrar, y se muestran de forma
legible (09/11/2026 14:19); las fechas dentro de textos se formatean igual con texto_fecha().

Libros:
  libro_inventario(c, periodo, fecha_ref) -> Inventario (por envíos + sueltos), Envíos, Movimientos
  libro_verificacion(c)                   -> Usuarios y equipos, Usuarios, Verificaciones
  libro_banco(c)                          -> Pruebas (una fila por prueba), Errores
"""

import json
from datetime import datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

TIPOS_PRODUCTO = {"CA": "Casco", "CH": "Chaleco", "PB": "Placa balística"}
TIPOS_PRUEBA = {"lectura": "Lectura múltiple",
                "escritura_lectura": "Escritura y lectura múltiple",
                "datos_distintos": "Escritura y lectura múltiple con distintos datos"}
# columnas de la tabla equipos y nombre del hueco
HUECOS = [("casco_id", "Casco"), ("chaleco_id", "Chaleco"), ("placa1_id", "Placa 1"), ("placa2_id", "Placa 2")]

# ---------------------------------------------------------------- estilo común
AZUL = "2E75B6"          # cabecera
AZUL_OSC = "1F4E79"      # título y bordes medianos
GRUPO = "DDEBF7"         # filas de grupo (envío / usuario)
BANDA = "EEF4FB"         # banda alterna de datos
GRIS = "BFBFBF"          # bordes finos de datos
TEXTO_SEC = "595959"
ROJO, ROJO_FONDO = "C00000", "FDE9E7"
VERDE = "2E7D32"
FMT_FECHA = "dd/mm/yyyy hh:mm"

FINO = Side(style="thin", color=GRIS)
MEDIO = Side(style="medium", color=AZUL_OSC)


def fuente(**k):
    k.setdefault("size", 9)
    return Font(name="Arial", **k)


def a_fecha(txt):
    """Fecha ISO 8601 ('YYYY-MM-DDThh:mm' o con segundos) -> datetime; si no se puede, el texto tal cual."""
    if not txt:
        return None
    for f in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(txt, f)
        except ValueError:
            pass
    return txt


def texto_fecha(txt):
    """Fecha ISO 8601 -> '09/11/2026 14:19' para usarla dentro de un texto."""
    d = a_fecha(txt)
    return f"{d:%d/%m/%Y %H:%M}" if isinstance(d, datetime) else (txt or "")


def a_numero(txt):
    try:
        return float(str(txt).replace(",", "."))
    except (TypeError, ValueError):
        return txt or None


class Hoja:
    """Hoja con el formato común. columnas = [(título, ancho, formato_numérico | None), ...]."""

    def __init__(self, wb, nombre, titulo, subtitulo, columnas, primera=False, grupos_cabecera=None,
                 fijar_columnas=0):
        self.ws = wb.active if primera else wb.create_sheet()
        self.ws.title = nombre
        self.cols = columnas
        self.n = len(columnas)
        ws = self.ws
        ws.sheet_view.showGridLines = False
        ws.cell(1, 1, titulo).font = fuente(size=14, bold=True, color=AZUL_OSC)
        ws.cell(2, 1, subtitulo).font = fuente(italic=True, color=TEXTO_SEC)
        ws.row_dimensions[1].height = 22
        fila = 4
        # fila opcional de grupos de cabecera (p. ej. «Lecturas» sobre sus columnas)
        if grupos_cabecera:
            col = 1
            for texto, ancho in grupos_cabecera:
                if texto:
                    ws.merge_cells(start_row=fila, start_column=col, end_row=fila, end_column=col + ancho - 1)
                for k in range(col, col + ancho):
                    self._cabecera(ws.cell(fila, k, texto if k == col else None))
                col += ancho
            fila += 1
        for k, (t, ancho, _) in enumerate(columnas, start=1):
            self._cabecera(ws.cell(fila, k, t))
            ws.column_dimensions[get_column_letter(k)].width = ancho
        ws.row_dimensions[fila].height = 30
        self.fila_cab = fila
        self.fila = fila + 1
        self.banda = 0
        ws.freeze_panes = ws.cell(self.fila, 1 + fijar_columnas)
        ws.print_title_rows = f"{4}:{fila}"
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.sheet_properties.pageSetUpPr.fitToPage = True

    @staticmethod
    def _cabecera(celda):
        celda.font = fuente(bold=True, color="FFFFFF")
        celda.fill = PatternFill("solid", fgColor=AZUL)
        celda.border = Border(left=MEDIO, right=MEDIO, top=MEDIO, bottom=MEDIO)
        celda.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    def grupo(self, texto):
        """Fila de grupo a todo el ancho (un envío, un usuario)."""
        ws, f = self.ws, self.fila
        ws.merge_cells(start_row=f, start_column=1, end_row=f, end_column=self.n)
        for k in range(1, self.n + 1):
            c = ws.cell(f, k, texto if k == 1 else None)
            c.fill = PatternFill("solid", fgColor=GRUPO)
            c.font = fuente(bold=True, color=AZUL_OSC)
            c.border = Border(top=MEDIO, bottom=Side(style="thin", color=AZUL_OSC),
                              left=MEDIO if k == 1 else None, right=MEDIO if k == self.n else None)
            c.alignment = Alignment(vertical="center", indent=1)
        ws.row_dimensions[f].height = 18
        self.fila += 1
        self.banda = 0

    def datos(self, valores, resaltar=None):
        """Fila de datos con banda alterna. resaltar = {índice_columna: 'rojo' | 'verde'}."""
        ws, f = self.ws, self.fila
        fondo = PatternFill("solid", fgColor=BANDA) if self.banda % 2 else None
        for k, v in enumerate(valores, start=1):
            c = ws.cell(f, k, v)
            c.font = fuente()
            c.border = Border(left=FINO, right=FINO, top=FINO, bottom=FINO)
            # todo el contenido centrado en su columna (textos largos con ajuste de línea)
            c.alignment = Alignment(horizontal="center", vertical="center",
                                    wrap_text=isinstance(v, str) and len(v) > 40)
            if fondo:
                c.fill = fondo
            fmt = self.cols[k - 1][2]
            if isinstance(v, datetime):
                c.number_format = FMT_FECHA
            elif fmt and isinstance(v, (int, float)):
                c.number_format = fmt
        for k, color in (resaltar or {}).items():
            c = ws.cell(f, k + 1)
            if color == "rojo":
                c.font = fuente(bold=True, color=ROJO)
                c.fill = PatternFill("solid", fgColor=ROJO_FONDO)
            elif color == "verde":
                c.font = fuente(bold=True, color=VERDE)
        self.fila += 1
        self.banda += 1

    def nota(self, texto):
        """Fila en cursiva gris (p. ej. «Sin productos»)."""
        ws, f = self.ws, self.fila
        ws.merge_cells(start_row=f, start_column=1, end_row=f, end_column=self.n)
        c = ws.cell(f, 1, texto)
        c.font = fuente(italic=True, color=TEXTO_SEC)
        c.alignment = Alignment(indent=1)
        for k in range(1, self.n + 1):
            ws.cell(f, k).border = Border(left=FINO if k == 1 else None, right=FINO if k == self.n else None,
                                          bottom=FINO)
        self.fila += 1

    def cerrar(self, autofiltro=False):
        if self.fila == self.fila_cab + 1:
            self.nota("Sin datos")
        if autofiltro and self.fila > self.fila_cab + 1:
            self.ws.auto_filter.ref = (f"A{self.fila_cab}:{get_column_letter(self.n)}{self.fila - 1}")


def _sello():
    return datetime.now().strftime("%d/%m/%Y %H:%M")


def _libro():
    wb = Workbook()
    wb.properties.creator = "Base de datos RFID"
    return wb


# ---------------------------------------------------------------- Inventario y envíos
def rango_periodo(periodo, fecha_ref):
    """Devuelve (inicio, fin, texto) del día, la semana (lunes a domingo) o el mes de fecha_ref."""
    d = datetime.combine(fecha_ref, datetime.min.time())
    if periodo == "dia":
        a, b = d, d + timedelta(days=1)
        txt = f"Día {a:%d/%m/%Y}"
    elif periodo == "semana":
        a = d - timedelta(days=d.weekday())
        b = a + timedelta(days=7)
        txt = f"Semana del {a:%d/%m/%Y} al {b - timedelta(days=1):%d/%m/%Y}"
    elif periodo == "mes":
        a = d.replace(day=1)
        b = (a + timedelta(days=32)).replace(day=1)
        meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
                 "septiembre", "octubre", "noviembre", "diciembre"]
        txt = f"Mes de {meses[a.month - 1]} de {a.year}"
    else:
        raise ValueError("Periodo no válido")
    return a, b, txt


def libro_inventario(c, periodo, fecha_ref):
    a, b, txt_periodo = rango_periodo(periodo, fecha_ref)
    # las fechas son texto ISO 8601: se comparan directamente como cadenas
    ia, ib = f"{a:%Y-%m-%d}", f"{b:%Y-%m-%d}"
    productos = [dict(r) for r in c.execute("SELECT * FROM productos")]
    todos_envios = [dict(r) for r in c.execute("SELECT * FROM envios")]
    usuarios = {r["id"]: f"{r['nombre']} {r['apellidos']}".strip() for r in c.execute("SELECT * FROM usuarios")}
    dueno = {}
    for r in c.execute("SELECT * FROM equipos"):
        for col, _ in HUECOS:
            if r[col]:
                dueno[r[col]] = usuarios.get(r["usuario_id"], "")
    movs = [dict(r) for r in c.execute(
        "SELECT * FROM movimientos WHERE fecha >= ? AND fecha < ? ORDER BY fecha DESC, id DESC", (ia, ib))]
    prod_mov = {m["producto_id"] for m in movs if m["producto_id"]}
    env_mov = {m["envio_id"] for m in movs if m["envio_id"]}

    # Un producto o envío entra si se creó en el periodo o tuvo algún movimiento en él
    def en_periodo(o, con_mov):
        return ia <= o["fecha"] < ib or o["id"] in con_mov

    recientes = lambda lista: sorted(lista, key=lambda o: o["fecha"], reverse=True)
    hijos = {e["id"]: recientes([p for p in productos if p["envio_id"] == e["id"]]) for e in todos_envios}
    envios = recientes([e for e in todos_envios if en_periodo(e, env_mov)
                        or any(en_periodo(p, prod_mov) for p in hijos[e["id"]])])
    sueltos = recientes([p for p in productos if p["envio_id"] is None and en_periodo(p, prod_mov)])
    n_hf = sum(len(hijos[e["id"]]) for e in envios) + len(sueltos)

    wb = _libro()
    sub = (f"{txt_periodo} · {len(envios)} envíos · {n_hf} productos HF · {len(movs)} movimientos · "
           f"generado el {_sello()}")
    cols = [("Nº de serie", 13, None), ("Tipo", 7, None), ("Producto", 16, None),
            ("Fecha de creación", 17, None), ("UID", 24, None), ("Tipo de etiqueta", 17, None),
            ("Tamper", 8, "0"), ("Usuario", 22, None), ("Última actividad", 17, None),
            ("Actividad en el periodo", 12, None)]
    h = Hoja(wb, "Inventario", "Inventario y envíos", sub, cols, primera=True)

    def fila_hf(p):
        h.datos([p["serie"], p["tipo_producto"], TIPOS_PRODUCTO.get(p["tipo_producto"], ""),
                 a_fecha(p["fecha"]), p["uid"] or "sin grabar", p["tarjeta"] or "",
                 p["tamper"], dueno.get(p["id"], ""), a_fecha(p["actualizado"]),
                 "Sí" if en_periodo(p, prod_mov) else "No"],
                resaltar={6: "rojo"} if p["tamper"] == 0 else None)

    if not envios:
        h.grupo("Envíos  ·  0")
        h.nota("Ningún envío creado o con movimientos en el periodo")
    for u in envios:
        n = len(hijos[u["id"]])
        h.grupo(f"Envío {u['numero']}  ·  {u['origen']} → {u['destino']}  ·  creado {texto_fecha(u['fecha'])}  ·  "
                f"{n} producto{'s' if n != 1 else ''}" + (f"  ·  UID {u['uid']}" if u["uid"] else ""))
        if not hijos[u["id"]]:
            h.nota("Sin productos")
        for p in hijos[u["id"]]:
            fila_hf(p)
    h.grupo(f"Productos sin asignar a ningún envío  ·  {len(sueltos)}")
    if not sueltos:
        h.nota("Ninguno en el periodo")
    for p in sueltos:
        fila_hf(p)
    h.cerrar()

    e = Hoja(wb, "Envíos", "Envíos", sub,
             [("Nº de envío", 16, None), ("Origen", 18, None), ("Destino", 18, None),
              ("Fecha de creación", 17, None), ("Productos", 10, "0"), ("UID", 24, None),
              ("Tipo de etiqueta", 17, None), ("Última actividad", 17, None)])
    for u in envios:
        e.datos([u["numero"], u["origen"], u["destino"], a_fecha(u["fecha"]), len(hijos[u["id"]]),
                 u["uid"] or "sin grabar", u["tarjeta"] or "", a_fecha(u["actualizado"])])
    e.cerrar(autofiltro=True)

    m = Hoja(wb, "Movimientos", "Movimientos del periodo", sub,
             [("Fecha", 17, None), ("Evento", 18, None), ("Tipo", 8, None), ("Referencia", 16, None),
              ("Detalle", 52, None), ("UID", 24, None), ("Tipo de etiqueta", 17, None)])
    for x in movs:
        m.datos([a_fecha(x["fecha"]), NOMBRE_EVENTO.get(x["evento"], x["evento"]),
                 {"USR": "Usuario"}.get(x["tipo"], x["tipo"] or ""), x["ref"] or "", x["detalle"] or "",
                 x["uid"] or "", x["tarjeta"] or ""],
                resaltar={1: "rojo"} if x["evento"] in ("DISCORDANCIA", "ELIMINADO", "RESET") else None)
    m.cerrar(autofiltro=True)
    return wb


NOMBRE_EVENTO = {"CREADO": "Creado", "LECTURA": "Lectura", "REGISTRADO": "Registrado",
                 "ESCRITURA": "Escritura", "SOBRESCRITO": "Sobrescrito", "IGNORADO": "Lectura ignorada",
                 "ASIGNADO": "Asignado", "CONTENIDO": "Añadido al envío", "DESASIGNADO": "Retirado del envío",
                 "ELIMINADO": "Eliminado", "TAMPER": "Tamper cambiado", "RESET": "Reset",
                 "USUARIO_ALTA": "Alta de usuario", "USUARIO_BAJA": "Baja de usuario",
                 "USUARIO_EDITADO": "Usuario editado",
                 "VINCULADO": "Vinculado", "DESVINCULADO": "Desvinculado", "VERIFICADO": "Verificado",
                 "DISCORDANCIA": "Discordancia", "VERIF_FIN": "Verificación finalizada"}


# ---------------------------------------------------------------- Verificación en campo
def libro_verificacion(c):
    usuarios = [dict(r) for r in c.execute("""
        SELECT u.*, e.casco_id, e.chaleco_id, e.placa1_id, e.placa2_id
        FROM usuarios u LEFT JOIN equipos e ON e.usuario_id = u.id
        ORDER BY u.apellidos COLLATE NOCASE, u.nombre COLLATE NOCASE""")]
    productos = {r["id"]: dict(r) for r in c.execute("SELECT * FROM productos")}
    envio_de = {r["id"]: r["numero"] for r in c.execute("SELECT id, numero FROM envios")}
    verifs = [dict(r) for r in c.execute(
        "SELECT * FROM movimientos WHERE evento IN ('VERIFICADO','DISCORDANCIA','VERIF_FIN') "
        "ORDER BY fecha DESC, id DESC")]

    def equipo(u):
        """[(nombre del hueco, producto | None)] según la fila de equipos del usuario."""
        return [(nombre, productos.get(u[col])) for col, nombre in HUECOS]

    nombre = lambda u: f"{u['nombre']} {u['apellidos']}".strip()
    n_prod = sum(1 for u in usuarios for _, p in equipo(u) if p)
    wb = _libro()
    sub = f"{len(usuarios)} usuarios · {n_prod} productos vinculados · generado el {_sello()}"
    cols = [("Hueco", 10, None), ("Nº de serie", 13, None), ("Tipo", 7, None), ("Producto", 16, None),
            ("UID", 24, None), ("Tipo de etiqueta", 17, None), ("Tamper", 8, "0"), ("Envío", 14, None),
            ("Última verificación", 17, None), ("Resultado de la última verificación", 46, None)]
    h = Hoja(wb, "Usuarios y equipos", "Verificación en campo", sub, cols, primera=True)
    for u in usuarios:
        eq = equipo(u)
        n = sum(1 for _, p in eq if p)
        ult = f"  ·  última verificación {texto_fecha(u['ultima_verificacion'])}" if u.get("ultima_verificacion") else \
              "  ·  sin verificar"
        h.grupo(f"{nombre(u)}  ·  {u['identificacion'] or 'sin identificación'}  ·  alta {texto_fecha(u['fecha'])}  ·  "
                f"equipo {n} de 4" + (" (completo)" if n == 4 else "") + ult)
        for hueco, p in eq:
            if not p:
                h.datos([hueco, "—", "", "Vacío", "", "", None, "", None, ""])
                continue
            res = p.get("ultima_verificacion_resultado") or ""
            marca = {}
            if p["tamper"] == 0:
                marca[6] = "rojo"
            if res and "correcto" not in res.lower():
                marca[9] = "rojo"
            elif res:
                marca[9] = "verde"
            h.datos([hueco, p["serie"], p["tipo_producto"], TIPOS_PRODUCTO.get(p["tipo_producto"], ""),
                     p["uid"] or "sin grabar", p["tarjeta"] or "", p["tamper"], envio_de.get(p["envio_id"], ""),
                     a_fecha(p.get("ultima_verificacion")) or "Nunca", res], resaltar=marca)
    if not usuarios:
        h.nota("No hay usuarios registrados")
    h.cerrar()

    r = Hoja(wb, "Usuarios", "Usuarios", sub,
             [("Nombre", 16, None), ("Apellidos", 22, None), ("Identificación", 14, None),
              ("Fecha de alta", 17, None), ("Productos en el equipo", 12, "0"),
              ("Equipo completo", 11, None), ("Productos con tamper 0", 12, "0"),
              ("Última verificación", 17, None), ("Resultado de la última verificación", 46, None)])
    for u in usuarios:
        eq = [p for _, p in equipo(u) if p]
        t0 = sum(1 for p in eq if p["tamper"] == 0)
        marca = {}
        if not u["identificacion"]:
            marca[2] = "rojo"
        if t0:
            marca[6] = "rojo"
        r.datos([u["nombre"], u["apellidos"], u["identificacion"] or "sin identificación", a_fecha(u["fecha"]),
                 len(eq), "Sí" if len(eq) == 4 else "No", t0,
                 a_fecha(u.get("ultima_verificacion")) or "Nunca", u.get("ultima_verificacion_resultado") or ""],
                resaltar=marca)
    r.cerrar(autofiltro=True)

    v = Hoja(wb, "Verificaciones", "Historial de verificaciones", sub,
             [("Fecha", 17, None), ("Evento", 20, None), ("Usuario / producto", 18, None),
              ("Detalle", 60, None), ("UID", 24, None), ("Tipo de etiqueta", 17, None)])
    for x in verifs:
        v.datos([a_fecha(x["fecha"]), NOMBRE_EVENTO.get(x["evento"], x["evento"]), x["ref"] or "",
                 x["detalle"] or "", x["uid"] or "", x["tarjeta"] or ""],
                resaltar={1: "rojo"} if x["evento"] == "DISCORDANCIA" else
                ({1: "verde"} if x["evento"] == "VERIFICADO" else None))
    v.cerrar(autofiltro=True)
    return wb


# ---------------------------------------------------------------- Banco de pruebas
def libro_banco(c):
    pruebas = [dict(r) for r in c.execute("SELECT * FROM pruebas ORDER BY numero")]
    wb = _libro()
    sub = f"{len(pruebas)} pruebas · generado el {_sello()}"
    cols = [("Prueba", 10, None), ("Fecha", 17, None), ("Tipo de prueba", 26, None), ("Repeticiones", 11, "0"),
            ("Ciclos realizados", 10, "0"), ("Cancelada", 9, None), ("UID", 24, None),
            ("Tipo de etiqueta", 16, None), ("Distancia (cm)", 10, "0.0"), ("Ángulo (°)", 9, "0"),
            ("Texto / serie inicial", 18, None), ("Notas", 34, None),
            # lecturas
            ("Total", 8, "0"), ("Sin error", 8, "0"), ("% sin error", 9, "0.0%"), ("Tiempo medio (ms)", 10, "0.0"),
            ("Mín (ms)", 9, "0.0"), ("Máx (ms)", 9, "0.0"), ("Velocidad (op/s)", 10, "0.00"),
            # escrituras
            ("Total", 8, "0"), ("Sin error", 8, "0"), ("% sin error", 9, "0.0%"), ("Tiempo medio (ms)", 10, "0.0"),
            ("Mín (ms)", 9, "0.0"), ("Máx (ms)", 9, "0.0"), ("Velocidad (op/s)", 10, "0.00"),
            # verificación y resto
            ("Leído = escrito", 9, "0"), ("% verificado", 10, "0.0%"), ("Duración (s)", 10, "0.00"),
            ("Contenido restaurado", 11, None), ("Nº de errores", 9, "0")]
    grupos = [("", 12), ("Lecturas", 7), ("Escrituras", 7), ("Verificación y resultado", 5)]
    h = Hoja(wb, "Pruebas", "Banco de pruebas", sub, cols, primera=True, grupos_cabecera=grupos,
             fijar_columnas=1)
    errores = []

    def bloque(S):
        if not S:
            return [None] * 7
        media = S.get("media_ms")
        return [S["total"], S["ok"], S["ok"] / S["total"] if S["total"] else None, media,
                S.get("min_ms"), S.get("max_ms"), round(1000 / media, 2) if media else None]

    for p in pruebas:
        A, R = json.loads(p["parametros"]), json.loads(p["resultados"])
        L, E = R.get("lecturas"), R.get("escrituras")
        ver = R.get("verificadas")
        ciclos = R.get("ciclos") or 0
        fila = ([p["nombre"], a_fecha(p["fecha"]), TIPOS_PRUEBA.get(p["tipo"], p["tipo"]), A.get("repeticiones"),
                 ciclos, "Sí" if R.get("cancelada") else "No", R.get("uid") or "", R.get("tarjeta") or "",
                 a_numero(A.get("distancia_cm")), a_numero(A.get("angulo_grados")),
                 A.get("texto") or A.get("serie_inicial") or "", A.get("notas") or ""]
                + bloque(L) + bloque(E)
                + [ver, (ver / ciclos) if ver is not None and ciclos else None, R.get("duracion_s"),
                   {True: "Sí", False: "No"}.get(R.get("restaurada"), "—"), R.get("n_errores", 0)])
        marca = {}
        if L and L["ok"] < L["total"]:
            marca[14] = "rojo"
        if E and E["ok"] < E["total"]:
            marca[21] = "rojo"
        if R.get("n_errores"):
            marca[30] = "rojo"
        h.datos(fila, resaltar=marca)
        for x in R.get("errores", []):
            errores.append([p["nombre"], x.get("ciclo"), x.get("op"), x.get("error")])
        if R.get("n_errores", 0) > len(R.get("errores", [])):
            errores.append([p["nombre"], None, "", f"… y {R['n_errores'] - len(R['errores'])} errores más "
                                                   "(solo se guardan los 50 primeros)"])
    h.cerrar(autofiltro=True)

    e = Hoja(wb, "Errores", "Errores de las pruebas", sub,
             [("Prueba", 10, None), ("Ciclo", 8, "0"), ("Operación", 14, None), ("Error", 70, None)])
    for x in errores:
        e.datos(x)
    if not errores:
        e.nota("Ninguna prueba ha tenido errores")
    e.cerrar(autofiltro=True)
    return wb
