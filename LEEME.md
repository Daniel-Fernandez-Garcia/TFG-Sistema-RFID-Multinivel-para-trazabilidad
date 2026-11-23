# Base de datos web RFID (HF ↔ UHF) · versión 2

Web local para registrar etiquetas RFID con el PN532 de la prueba (USB, COM9, 115200 baudios).


Opciones de `servidor.py`:

| Opción | Uso |
| --- | --- |
| `--port COM5` | otro puerto serie |
| `--simulado` | lector virtual, sin hardware |
| `--debug` | muestra las tramas del PN532 en hexadecimal |
| `--http 8080` | otro puerto para la web |

## Base de datos (`rfid.db`)

| Tabla | Contenido |
| --- | --- |
| `productos` | productos HF: nº de serie, tipo (CA/CH/PB), fecha, envío al que pertenecen, UID, tamper |
| `envios` | envíos (etiqueta UHF): número, origen, destino, fecha, UID. Sus productos son las filas de `productos` con ese `envio_id` |
| `usuarios` | nombre, apellidos, identificación (única) y fecha de alta |
| `equipos` | una fila por usuario con sus huecos: `casco_id`, `chaleco_id`, `placa1_id`, `placa2_id` (→ `productos`) |
| `movimientos` | registro de todo lo que pasa, con referencias al producto, envío o usuario |
| `pruebas`, `config` | Banco de pruebas y ajustes (último nº de serie, carpeta de exportación) |

**Migración desde la versión 1**: si al arrancar se encuentra una base antigua (tabla `personas`), se guarda una
copia en `rfid_v1_copia.db` y se convierte sola: UHF → `envios`, personas → `usuarios` + `equipos`, series y fechas
al formato nuevo. Los usuarios migrados no tienen identificación: aparecen con el aviso «sin identificación»
y se completan con «Editar».

## Pestañas

La barra superior tiene dos grupos separados:

- **Explotación** (izquierda):
  - **Inventario y envíos**: productos HF y envíos. «Crear» da de alta un producto (HF) o un envío (UHF).
    «Añadir por lectura» mete en un envío todos los productos que se lean hasta pulsar Finalizar.
    «Reset de la base de datos» borra productos, envíos, usuarios, equipos y movimientos y vuelve a `0000000`
    (hay que escribir RESET). El Banco de pruebas no se borra.
  - **Verificación en campo**: usuarios con su equipo (1 casco, 1 chaleco, hasta 2 placas). «Vincular por lectura»
    llena los huecos; «Verificación» relee el equipo y marca cada producto con un tick verde o un aviso rojo.
- **Pruebas** (derecha): **Banco de pruebas**. Al abrirlo toda la interfaz pasa a un azul más claro para que se
  note que no se está trabajando con datos de explotación.

**Tamper**: bit virtual de los productos HF (1 = correcto, 0 = posiblemente comprometido). Solo existe en la base de datos.

## Exportar a Excel

Cada pestaña tiene «Exportar a Excel». La primera vez se pide una carpeta y dentro se crean `Inventario y envíos`,
`Verificación en campo` y `Banco de pruebas`. Nombre: pestaña + fecha ISO, p. ej. `Inventario_y_envíos_2026-11-09_14-41-12.xlsx`
(la hora va con guiones porque Windows no admite «:» en los nombres).

## Fechas y números de serie

- Fechas guardadas en ISO 8601 con fecha y hora (`2026-11-09T14:19`), tanto en la base de datos como en las
  etiquetas. En la web y en los Excel se muestran de forma legible: `09/11/2026 14:19` (en Excel son fechas reales,
  se pueden ordenar y filtrar).
- Nº de serie hexadecimal de 7 dígitos, `0000000` a `FFFFFFF`: ocupa lo mismo que el formato matrícula
  (7 caracteres = 56 bits en la etiqueta) y cubre todos sus valores (67 600 000 matrículas frente a 268 435 456
  series hexadecimales). Al llegar a `FFFFFFF` vuelve a `0000000`.
  Conversión de las series antiguas: (letra1 × 26 + letra2) × 100 000 + número, en hexadecimal
  (`AA00004 → 0000004`, `ZZ99999 → 4077E7F`).

## Datos grabados en la etiqueta

Texto UTF-8 terminado en 0x00:

- HF: `RFID2|HF|0000004|CA|2026-11-09T14:19` (36 B)
- UHF: `RFID2|UHF|<nº de envío>|<origen>|<destino>|<fecha>`

Las etiquetas grabadas con la versión 1 (`RFID1|HF|AA00004|CA|20/10/2026-12:37`) se siguen leyendo: se convierten
al formato nuevo al leerlas y coinciden con su registro migrado. Para actualizarlas basta con «Escribir».

Capacidad: NTAG213 y MIFARE Classic 1K → 144 B; Ultralight → 48 B. El PN532 solo trabaja en HF (13,56 MHz):
el envío es un contenedor virtual que se graba en una etiqueta HF que lo representa.

## Archivos

- `servidor.py`: servidor web, API y base de datos SQLite.
- `pn532.py`: driver del PN532 por UART.
- `index.html`: la interfaz.
- `exportar_excel.py`: genera los libros de Excel (necesita `openpyxl`; `iniciar.bat` la instala si falta).
