"""
Escáner de ofertas de Easy.cl

- Recorre TODAS las páginas del paginado.
- Calcula el % de descuento (ignora precios por unidad como "$ 449 por m").
- Descarta productos de vendedores externos (solo "Vendido por Easy").
- Descarta productos que dicen "Este producto no está disponible en este momento".
- Verifica varios productos en paralelo, sin descargar imágenes.
- Guarda el resultado en un CSV en tu Escritorio.

Instalación (una sola vez):
    pip install playwright
    playwright install chromium

Uso:
    python escaner_easy.py
"""

import asyncio
import csv
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime

from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright

# ----------------------- CONFIGURACIÓN -----------------------
# Las páginas que quieres escanear: ofertas, categorías, lo que quieras.
# Agrega una dirección por línea, entre comillas y con una coma al final.
# Si un producto sale en dos categorías, se cuenta una sola vez.
URLS = [
    "https://www.easy.cl/cluster/6826",
    "https://www.easy.cl/decoracion-e-iluminacion/iluminacion",
    "https://www.easy.cl/search/organizacion",
    "https://www.easy.cl/decoracion-e-iluminacion/especial-navidad",
    "https://www.easy.cl/search/halloween",
    "https://www.easy.cl/search/mdesign",
    "https://www.easy.cl/jardin-y-aire-libre/jardin/maceteros-y-decoracion",
    "https://www.easy.cl/search/mascotas",
    #organización y limpieza
    "https://www.easy.cl/organizacion-y-limpieza/limpieza",
    "https://www.easy.cl/organizacion-y-limpieza/espejos-joyeros",
    "https://www.easy.cl/decoracion-e-iluminacion/decoracion-hogar",
    "https://www.easy.cl/organizacion-y-limpieza/cajas-organizadoras",
    "https://www.easy.cl/organizacion-y-limpieza/repisas-y-estantes",
    "https://www.easy.cl/organizacion-y-limpieza/organizacion-dormitorio-y-closet",
    "https://www.easy.cl/organizacion-y-limpieza/organizacion-escritorio",
    "https://www.easy.cl/cocina-y-comedor/organizacion-de-cocina",
    "https://www.easy.cl/organizacion-y-limpieza/organizacion-bano",
    "https://www.easy.cl/organizacion-y-limpieza/organizacion-loggia",
    "https://www.easy.cl/electricidad-y-seguridad/seguridad-infantil",
    "https://www.easy.cl/organizacion-y-limpieza/viaje-y-mudanza",
    
    "https://www.easy.cl/ver-todo/muebles",
    "https://www.easy.cl/search/menaje",
    "https://www.easy.cl/bano/griferia-y-repuestos-de-bano",
    "https://www.easy.cl/automovil/accesorios-interior",
    "https://www.easy.cl/automovil/limpieza-y-cuidado",
    "https://www.easy.cl/dormitorio/ropa-de-cama",
    "https://www.easy.cl/electrohogar-y-climatizacion/ventilacion",
    "https://www.easy.cl/electrohogar-y-climatizacion/refrigeracion",
    "https://www.easy.cl/electrohogar-y-climatizacion/electrodomesticos",
    "https://www.easy.cl/electrohogar-y-climatizacion/lavado-y-planchado",
    "https://www.easy.cl/electrohogar-y-climatizacion/cocina",
    "https://www.easy.cl/electrohogar-y-climatizacion/calefont-y-termos",
    "https://www.easy.cl/electrohogar-y-climatizacion/aspirado-y-limpieza",
    "https://www.easy.cl/electrohogar-y-climatizacion/smart-home",
    "https://www.easy.cl/electrohogar-y-climatizacion/tecnologia",
    "https://www.easy.cl/electrohogar-y-climatizacion/calefaccion",
]
MIN_DESCUENTO = 70       # solo guarda productos con al menos este % de descuento
MAX_PRECIO = 1500000      # solo guarda productos cuyo precio de oferta sea hasta este valor
SOLO_EASY = True         # True = descarta productos de vendedores externos
VERIFICAR_EN_PAGINA = True  # True = abre la página de cada oferta para confirmar
                            # que está disponible y quién la vende
CONCURRENCIA = 6        # cuántas páginas de producto revisa a la vez (2 a 6 es razonable)
PAUSA_SEG = 0.5         # pausa de cada revisor entre un producto y otro
SOLO_CONFIRMADOS = True # True = deja solo productos con botón "Añadir al carrito" visible
                        # (si no se puede confirmar que están disponibles, se descartan)
MAX_PAGINAS = 60        # tope de seguridad: máximo de páginas que recorre
SALIDA = os.environ.get("EASY_SALIDA") or os.path.expanduser("~/Desktop/ofertas_easy_70.csv")
# Aquí se guarda tu comuna de entrega (se crea con: python escaner_easy.py --configurar-ubicacion)
ESTADO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "easy_ubicacion.json")
# Cada dirección tiene su propio historial (un archivo por dirección) para comparar
# entre escaneos solo contra lo que esa misma dirección mostró la vez anterior.
CARPETA_HISTORIALES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "historiales")
CSV_POR_URL = True      # True = además del CSV con todo, guarda un CSV por cada dirección
CARPETA_POR_URL = os.environ.get("EASY_CARPETA_POR_URL") or os.path.expanduser("~/Desktop/ofertas_easy_por_url")

# --- Avisos por Telegram (se activan con: python3 escaner_easy.py --configurar-telegram) ---
AVISAR_TELEGRAM = True      # False = no manda nada aunque Telegram esté configurado
MAX_AVISOS_POR_URL = 25     # máximo de ofertas por mensaje, de cada dirección (las de mayor descuento)
PRIMERA_VEZ_TOP = 5         # la primera vez que se escanea una dirección, solo avisa las N mejores
AVISAR_SIN_NOVEDADES = False  # True = también te escribe cuando no hubo nada nuevo
TELEGRAM_VISTA_PREVIA = False  # True = Telegram muestra la foto/vista previa de cada enlace
ARCHIVO_TELEGRAM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram.json")
BAJA_MIN_PCT = 5        # un producto "bajó de precio" si la rebaja es de al menos este %
REVERIFICAR_HORAS = 0   # 0 = verifica SIEMPRE el stock en cada escaneo (lo más exacto).
                        # Si pones 48, no vuelve a abrir lo ya revisado hace menos de 48 h.
MOSTRAR_TOP = 30        # cuántas ofertas imprimir en pantalla
# -------------------------------------------------------------

PRECIO = re.compile(r"\$\s?(\d{1,3}(?:\.\d{3})+|\d+)")
SIGUIENTE = re.compile(r"^\s*(siguiente|next|›|»|>)\s*$", re.I)
VENDIDO_POR = re.compile(r"vendido\s+(?:y\s+despachado\s+)?por\s*:?\s*(.*)$", re.I)
# Precios que NO son el precio del producto: "($ 449 por m)", "$9.490 /m2", "$3.000 x 6"
POR_UNIDAD = re.compile(r"\s*(por|/|x\b|c/u|c\.u)", re.I)
# Mensaje que aparece en la página del producto cuando no hay stock
NO_DISPONIBLE_PAGINA = re.compile(r"este producto no est[áa] disponible", re.I)
# Botones que solo aparecen cuando el producto se puede comprar
CON_BOTON_COMPRA = re.compile(r"a[ñn]adir al carrito|agregar al carrito|comprar ahora", re.I)
# Avisos que podrían aparecer directamente en la tarjeta del listado
NO_DISPONIBLE_TARJETA = re.compile(r"agotado|sin stock|no disponible|no est[áa] disponible", re.I)

# JavaScript que corre dentro de la página: junta todos los enlaces que
# contienen precios ($) y, además, los textos que aparecen tachados
# (el precio normal antes del descuento).
JS_EXTRAER = """
() => {
  const vistos = new Set();
  const out = [];
  for (const a of document.querySelectorAll('a[href]')) {
    const texto = (a.innerText || '').trim();
    if (!texto.includes('$')) continue;
    if (vistos.has(a.href)) continue;
    vistos.add(a.href);
    const tachados = [];
    for (const el of a.querySelectorAll('*')) {
      const linea = getComputedStyle(el).textDecorationLine || '';
      if (linea.includes('line-through')) {
        const t = (el.innerText || '').trim();
        if (t.includes('$')) tachados.push(t);
      }
    }
    out.push({href: a.href, texto, tachados});
  }
  return out;
}
"""


def opciones_contexto():
    """Opciones del navegador. Si hay una ubicación guardada, se usa para ver el stock de tu comuna."""
    if os.path.exists(ESTADO):
        return {"locale": "es-CL", "storage_state": ESTADO}
    return {"locale": "es-CL"}


def configurar_ubicacion():
    """Abre una ventana de Easy para que elijas tu comuna de entrega y guarda esa elección."""
    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=False)
        contexto = navegador.new_context(locale="es-CL")
        page = contexto.new_page()
        page.goto("https://www.easy.cl", wait_until="domcontentloaded", timeout=60000)
        print("\nSe abrió una ventana de Easy.")
        print("1) Arriba, donde dice 'Entrega en ...', elige tu comuna (por ejemplo Huechuraba).")
        print("2) No inicies sesión: solo elige la comuna.")
        print("3) Cuando termines, vuelve a esta ventana de la terminal y presiona Enter.")
        input("\nPresiona Enter para guardar ... ")
        try:
            texto = page.inner_text("body")
            m = re.search(r"Entrega en\s*\n?\s*([^\n]+)", texto, re.I)
            print(f"La ventana muestra: Entrega en {m.group(1).strip() if m else '(no se encontró)'}")
        except Exception:
            pass
        try:
            contexto.storage_state(path=ESTADO, indexed_db=True)
        except TypeError:
            contexto.storage_state(path=ESTADO)  # versión antigua de Playwright
        navegador.close()
    print(f"\nUbicación guardada en {ESTADO}")
    print("(Ese archivo contiene datos de sesión del navegador: no lo compartas.)")


# ---------------------- PASO 1: recorrer el listado ----------------------

def _por_navegacion(err):
    """True si el error es porque la página navegó o se recargó mientras la leíamos."""
    texto = str(err).lower()
    return "context was destroyed" in texto or "navigation" in texto


def evaluar(page, js, intentos=4):
    """Ejecuta JavaScript en la página. Si justo en ese momento la página navega o se recarga
    (error 'Execution context was destroyed'), espera a que termine de cargar y reintenta."""
    for _ in range(intentos - 1):
        try:
            return page.evaluate(js)
        except Exception as err:
            if not _por_navegacion(err):
                raise
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2000)
    return page.evaluate(js)  # último intento: si falla, deja salir el error real


def cargar_pagina(page):
    """Baja la página hasta el final para que carguen todos los productos de esa página."""
    altura_anterior = 0
    for _ in range(10):
        try:
            page.mouse.wheel(0, 4000)
            page.wait_for_timeout(1800)
        except Exception as err:
            if not _por_navegacion(err):
                raise
            page.wait_for_timeout(2000)
        altura = evaluar(page, "document.body.scrollHeight")
        if altura == altura_anterior:
            break
        altura_anterior = altura


def hacer_clic_siguiente(page, pagina_actual):
    """Intenta pasar a la página siguiente. Devuelve True si logró hacer clic."""
    siguiente_num = re.compile(rf"^\s*{pagina_actual + 1}\s*$")
    candidatos = [
        page.get_by_role("link", name=SIGUIENTE),
        page.get_by_role("button", name=SIGUIENTE),
        page.locator('[aria-label*="iguiente" i], [aria-label*="next" i], [rel="next"]'),
        page.get_by_role("link", name=siguiente_num),
        page.get_by_role("button", name=siguiente_num),
    ]
    for grupo in candidatos:
        try:
            for i in range(grupo.count()):
                el = grupo.nth(i)
                if not el.is_visible() or not el.is_enabled():
                    continue
                if el.get_attribute("aria-disabled") == "true":
                    continue
                el.scroll_into_view_if_needed()
                el.click()
                page.wait_for_timeout(3500)
                return True
        except Exception:
            continue
    return False


def raspar_url(page, url, vistos):
    """Recorre todas las páginas del paginado de UNA dirección y agrega las tarjetas a 'vistos'."""
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("load", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(4000)

    de_esta_url = set()  # para saber cuándo esta dirección ya no trae nada nuevo
    pagina = 1
    while pagina <= MAX_PAGINAS:
        cargar_pagina(page)
        items = evaluar(page, JS_EXTRAER)
        nuevos_url = 0
        nuevos = 0
        for it in items:
            if it["href"] not in de_esta_url:
                de_esta_url.add(it["href"])
                nuevos_url += 1
            if it["href"] not in vistos:
                vistos[it["href"]] = it
                nuevos += 1
        print(f"Página {pagina}: {len(items)} tarjetas, {nuevos} nuevas")

        if nuevos_url == 0:
            print("Esta página no trajo productos nuevos. Termino con esta dirección.")
            break
        if not hacer_clic_siguiente(page, pagina):
            print("No encontré botón de página siguiente. Termino con esta dirección.")
            break
        pagina += 1
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass


def raspar_listado(url):
    """Recorre todas las páginas de UNA dirección y devuelve {enlace: tarjeta}."""
    vistos = {}
    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        try:
            contexto = navegador.new_context(**opciones_contexto())
            page = contexto.new_page()
            try:
                raspar_url(page, url, vistos)
            except Exception as err:
                if not vistos:
                    raise
                print(
                    f"Aviso: el recorrido se interrumpió ({err}).\n"
                    f"Sigo con lo que alcancé a leer: {len(vistos)} tarjetas."
                )
        finally:
            navegador.close()
    return vistos


# ---------------------- PASO 2: interpretar las tarjetas ----------------------

# Montos que aparecen en etiquetas y no son el precio del producto: "Envío a $1",
# "Retiro gratis sobre $49.990", "6 cuotas de $3.000"
ANTES_NO_PRECIO = re.compile(r"(env[ií]o|despacho|retiro|cuotas?)\b[^$\n]*$", re.I)
PRECIO_MINIMO = 100  # nada en Easy cuesta menos que esto; un "$1" es una etiqueta


def extraer_precios(texto):
    """Devuelve los precios del texto, ignorando precios por unidad como '($ 449 por m)'
    y montos de etiquetas como 'Envío a $1'."""
    precios = []
    for m in PRECIO.finditer(texto):
        antes = texto[max(0, m.start() - 3):m.start()].rstrip()
        despues = texto[m.end():m.end() + 15]
        previo = texto[max(0, m.start() - 25):m.start()]
        if antes.endswith("(") or POR_UNIDAD.match(despues) or ANTES_NO_PRECIO.search(previo):
            continue
        valor = int(m.group(1).replace(".", ""))
        if valor < PRECIO_MINIMO:
            continue
        precios.append(valor)
    return precios


def detectar_vendedor(texto):
    """Busca 'Vendido por X' en el texto. Devuelve el vendedor o '' si no aparece."""
    lineas = [l.strip() for l in texto.split("\n") if l.strip()]
    for i, linea in enumerate(lineas):
        m = VENDIDO_POR.search(linea)
        if m:
            vendedor = m.group(1).strip()
            # a veces el nombre viene en la línea siguiente
            if not vendedor and i + 1 < len(lineas):
                vendedor = lineas[i + 1]
            return vendedor
    return ""


def es_easy(vendedor):
    """Si no se indica vendedor, se asume que es Easy."""
    return vendedor == "" or "easy" in vendedor.lower()


def parsear(item):
    """Convierte el texto de una tarjeta en un producto con precios y descuento."""
    precios = extraer_precios(item["texto"])
    tachados = [p for t in item.get("tachados", []) for p in extraer_precios(t)]

    if tachados:
        # Hay un precio tachado: ese es el precio normal.
        normal = max(tachados)
        menores = [p for p in precios if p < normal]
        if not menores:
            return None
        oferta = min(menores)
    else:
        # Sin precio tachado: se compara el más alto con el más bajo.
        if len(precios) < 2:
            return None
        normal, oferta = max(precios), min(precios)
        if normal == oferta:
            return None

    lineas = [
        l.strip()
        for l in item["texto"].split("\n")
        if l.strip()
        and "$" not in l
        and "%" not in l
        and not VENDIDO_POR.search(l)
        and len(l.strip()) > 3
    ]
    nombre = max(lineas, key=len) if lineas else "(sin nombre)"
    descuento = round((normal - oferta) / normal * 100)
    return {
        "nombre": nombre,
        "precio_normal": normal,
        "precio_oferta": oferta,
        "descuento_pct": descuento,
        "vendedor": detectar_vendedor(item["texto"]),
        "disponible": "no verificado",
        "enlace": item["href"],
    }


# ---------------------- PASO 3: verificar en la página de cada producto ----------------------

async def bloquear_recursos(route):
    """No descarga imágenes, videos ni tipografías (solo necesitamos el texto).
    Las hojas de estilo se dejan pasar a propósito, para que el texto oculto
    de la página siga oculto y no genere falsos avisos."""
    if route.request.resource_type in ("image", "media", "font"):
        await route.abort()
    else:
        await route.continue_()


async def verificar_uno(contexto, enlace, semaforo):
    """Devuelve (disponible, vendedor). disponible = None si no se pudo abrir la página."""
    async with semaforo:
        resultado = (None, "")
        pg = await contexto.new_page()
        try:
            await pg.goto(enlace, wait_until="domcontentloaded", timeout=45000)
            try:
                # Espera a que la página defina el stock: aparece el botón de compra
                # o el aviso de "no disponible" (puede llegar unos segundos después
                # del resto de la ficha).
                await pg.wait_for_function(
                    """() => {
                        const t = document.body.innerText.toLowerCase();
                        return t.includes('añadir al carrito')
                            || t.includes('agregar al carrito')
                            || t.includes('comprar ahora')
                            || t.includes('este producto no está disponible');
                    }""",
                    timeout=12000,
                )
            except Exception:
                pass
            # Margen extra: los productos recomendados también traen botones de compra
            # y pueden cargar antes que el aviso de stock del producto principal.
            await pg.wait_for_timeout(2500)
            texto = await pg.inner_text("body")
            if NO_DISPONIBLE_PAGINA.search(texto):
                disponible = False
            elif CON_BOTON_COMPRA.search(texto):
                disponible = True
            else:
                disponible = None  # no se pudo confirmar
            resultado = (disponible, detectar_vendedor(texto))
        except Exception:
            pass
        finally:
            await pg.close()
        await asyncio.sleep(PAUSA_SEG)
        return resultado


async def verificar_todas(ofertas):
    """Revisa todas las ofertas, CONCURRENCIA a la vez. Devuelve los resultados en el mismo orden."""
    semaforo = asyncio.Semaphore(CONCURRENCIA)
    total = len(ofertas)
    hechos = 0

    async with async_playwright() as p:
        navegador = await p.chromium.launch(headless=True)
        contexto = await navegador.new_context(**opciones_contexto())
        await contexto.route("**/*", bloquear_recursos)

        async def tarea(o):
            nonlocal hechos
            res = await verificar_uno(contexto, o["enlace"], semaforo)
            hechos += 1
            print(f"  ({hechos}/{total}) {o['nombre'][:50]}")
            return res

        resultados = await asyncio.gather(*(tarea(o) for o in ofertas))
        await navegador.close()
    return resultados


# ---------------------- PROGRAMA PRINCIPAL ----------------------

def diagnostico(url):
    """Abre un producto con la ubicación guardada, muestra qué ubicación y stock ve el sitio
    y guarda una captura de pantalla (diagnostico.png) junto al script."""
    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        contexto = navegador.new_context(**opciones_contexto())
        page = contexto.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        texto = page.inner_text("body")
        m = re.search(r"Entrega en\s*\n?\s*([^\n]+)", texto, re.I)
        print(f"\nUbicación guardada en disco: {'sí' if os.path.exists(ESTADO) else 'NO'}")
        print(f"El sitio muestra 'Entrega en': {m.group(1).strip() if m else '(no se encontró)'}")
        print(f"¿Dice 'no disponible'?: {'SÍ' if NO_DISPONIBLE_PAGINA.search(texto) else 'no'}")
        print(f"¿Tiene botón de compra?: {'SÍ' if CON_BOTON_COMPRA.search(texto) else 'no'}")
        ruta = os.path.join(os.path.dirname(ESTADO), "diagnostico.png")
        page.screenshot(path=ruta)
        print(f"Captura guardada en {ruta}")
        navegador.close()


def nombre_corto(url):
    """Nombre corto y seguro para archivos, sacado de la dirección.
    Ej.: https://www.easy.cl/cluster/6826 -> cluster-6826"""
    resto = re.sub(r"^https?://(www\.)?easy\.cl/?", "", url.strip().lower())
    resto = re.sub(r"[^a-z0-9]+", "-", resto).strip("-")
    return (resto or "inicio")[:70]


def ruta_historial(url):
    """Cada dirección tiene su propio historial."""
    return os.path.join(CARPETA_HISTORIALES, f"{nombre_corto(url)}.json")


def cargar_historial(ruta):
    """Lee las ofertas ya vistas en esa dirección: {enlace: {nombre, precio_oferta, ...}}."""
    try:
        with open(ruta, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def guardar_historial(historial, ruta):
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", encoding="utf-8") as f:
        json.dump(historial, f, ensure_ascii=False, indent=1)


def horas_desde(marca):
    """Horas transcurridas desde una fecha guardada como 'AAAA-MM-DD HH:MM' (infinito si no hay fecha)."""
    if not marca:
        return float("inf")
    try:
        return (datetime.now() - datetime.strptime(marca, "%Y-%m-%d %H:%M")).total_seconds() / 3600
    except ValueError:
        return float("inf")


def es_novedad(o, previo):
    """'nueva' si no la conocíamos (o antes no servía), 'bajó de $X' si se rebajó, '' si no hay cambios."""
    if previo is None or previo.get("estado", "ok") != "ok":
        return "nueva"
    if o["precio_oferta"] < previo["precio_oferta"] * (1 - BAJA_MIN_PCT / 100):
        return f"bajó de ${previo['precio_oferta']:,}".replace(",", ".")
    return ""


def procesar_url(tarjetas, historial, cache_corrida, categoria):
    """
    Procesa las tarjetas de UNA dirección: filtra, verifica en la página del producto y
    compara con el historial de ESA dirección. Devuelve sus ofertas con la columna 'novedad'.

    cache_corrida: lo verificado durante este mismo escaneo, para no abrir dos veces un
    producto que aparece en dos direcciones.
    """
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 1) Leer las tarjetas
    productos = []
    agotados_tarjeta = 0
    for href, it in tarjetas.items():
        if NO_DISPONIBLE_TARJETA.search(it["texto"]):
            agotados_tarjeta += 1
            continue
        prod = parsear(it)
        if prod:
            productos.append(prod)

    # 2) Filtros que se pueden aplicar con lo que muestra la tarjeta
    candidatos = [
        x for x in productos
        if x["descuento_pct"] >= MIN_DESCUENTO
        and x["precio_oferta"] <= MAX_PRECIO
        and (not SOLO_EASY or es_easy(x["vendedor"]))
    ]

    # 3) Separar lo que ya sabemos (solo si REVERIFICAR_HORAS > 0) de lo que hay que verificar
    resultado = []
    a_verificar = []
    conocidas = descartadas_antes = 0
    for o in candidatos:
        previo = historial.get(o["enlace"])
        confiable = (
            VERIFICAR_EN_PAGINA
            and previo is not None
            and horas_desde(previo.get("verificado")) < REVERIFICAR_HORAS
        )
        if confiable and previo.get("estado", "ok") != "ok":
            descartadas_antes += 1  # ya sabemos que está agotado o es de otro vendedor
            previo["ultima_vez"] = ahora
            continue
        if confiable and not es_novedad(o, previo):
            o["vendedor"] = previo.get("vendedor") or o["vendedor"]
            o["disponible"] = f"sí (revisado {previo['verificado']})"
            o["novedad"] = ""
            previo["precio_oferta"] = o["precio_oferta"]
            previo["ultima_vez"] = ahora
            resultado.append(o)
            conocidas += 1
            continue
        a_verificar.append(o)

    # 4) Verificar en la página del producto
    no_disp = externos = sin_confirmar = 0
    ya_en_corrida = 0
    if VERIFICAR_EN_PAGINA and a_verificar:
        pendientes = [o for o in a_verificar if o["enlace"] not in cache_corrida]
        ya_en_corrida = len(a_verificar) - len(pendientes)
        if pendientes:
            extra = f", {ya_en_corrida} ya verificados en otra dirección" if ya_en_corrida else ""
            print(
                f"Verificando {len(pendientes)} productos en su propia página "
                f"({CONCURRENCIA} a la vez{extra}) ..."
            )
            for o, res in zip(pendientes, asyncio.run(verificar_todas(pendientes))):
                cache_corrida[o["enlace"]] = res
    resultados = [cache_corrida.get(o["enlace"], (None, "")) for o in a_verificar]

    for o, (disponible, vendedor) in zip(a_verificar, resultados):
        previo = historial.get(o["enlace"])
        base = {
            "nombre": o["nombre"],
            "precio_oferta": o["precio_oferta"],
            "primera_vez": previo.get("primera_vez", ahora) if previo else ahora,
            "ultima_vez": ahora,
        }
        if disponible is False:
            no_disp += 1
            historial[o["enlace"]] = {**base, "estado": "no_disponible", "verificado": ahora}
        elif SOLO_EASY and not es_easy(o["vendedor"] or vendedor):
            externos += 1
            historial[o["enlace"]] = {**base, "estado": "externo", "verificado": ahora}
        elif disponible is None and SOLO_CONFIRMADOS and VERIFICAR_EN_PAGINA:
            sin_confirmar += 1  # no se guarda: se vuelve a intentar la próxima vez
        else:
            o = dict(o)  # copia: el mismo producto puede aparecer en otra dirección
            if vendedor and not o["vendedor"]:
                o["vendedor"] = vendedor
            o["disponible"] = "sí" if disponible else "no verificado"
            o["novedad"] = es_novedad(o, previo)
            historial[o["enlace"]] = {
                **base,
                "estado": "ok",
                "vendedor": o["vendedor"],
                "verificado": ahora if disponible else None,
            }
            resultado.append(o)

    print(f"Productos con descuento: {len(productos)}  |  candidatos tras filtros: {len(candidatos)}")
    if conocidas + descartadas_antes:
        print(f"Ya revisados antes (no se vuelven a abrir): {conocidas + descartadas_antes}")
    if a_verificar and VERIFICAR_EN_PAGINA:
        print(
            f"Verificados: {len(a_verificar)} -> no disponibles: {no_disp}, "
            f"otro vendedor: {externos}, sin confirmar: {sin_confirmar}"
        )
    print(f"Ofertas de esta dirección: {len(resultado)}")

    # Las columnas 'novedad' y 'categoria' van primero en el CSV
    return [
        {
            "novedad": o.get("novedad", ""),
            "categoria": categoria,
            **{k: v for k, v in o.items() if k not in ("novedad", "categoria")},
        }
        for o in resultado
    ]


def ordenar(ofertas):
    """Primero las novedades; dentro de cada grupo, mayor descuento primero."""
    return sorted(ofertas, key=lambda x: (x["novedad"] == "", -x["descuento_pct"]))


def unir(todas):
    """Junta las ofertas de todas las direcciones sin repetir productos."""
    vistos = set()
    unicas = []
    for o in ordenar(todas):
        if o["enlace"] not in vistos:
            vistos.add(o["enlace"])
            unicas.append(o)
    return unicas


def escribir_csv(ofertas, ruta):
    """Guarda las ofertas en un CSV (separado por ';' para Excel en español)."""
    if not ofertas:
        return
    os.makedirs(os.path.dirname(ruta), exist_ok=True)
    with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(ofertas[0].keys()), delimiter=";")
        w.writeheader()
        w.writerows(ofertas)


def probar(url):
    """Revisa un solo producto y dice si está disponible (útil para comprobar la ubicación)."""
    ofertas = [{"enlace": url, "nombre": url[-50:]}]
    [(disponible, vendedor)] = asyncio.run(verificar_todas(ofertas))
    estado = {True: "DISPONIBLE", False: "NO DISPONIBLE", None: "no se pudo confirmar"}[disponible]
    print(f"\nResultado: {estado}")
    print(f"Vendedor: {vendedor or '(no se encontró)'}")


# ---------------------- AVISOS POR TELEGRAM ----------------------

def _precio(n):
    return f"${n:,}".replace(",", ".")


def telegram_api(token, metodo, datos=None):
    """Llama a la API de Telegram y devuelve la respuesta ya convertida desde JSON."""
    url = f"https://api.telegram.org/bot{token}/{metodo}"
    cuerpo = urllib.parse.urlencode(datos or {}).encode("utf-8")
    with urllib.request.urlopen(urllib.request.Request(url, data=cuerpo), timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def cargar_telegram():
    """Devuelve (token, chat_id) si Telegram está configurado; si no, None.
    Primero mira las variables de entorno TELEGRAM_TOKEN y TELEGRAM_CHAT_ID (para la nube)
    y si no están, el archivo telegram.json (para tu Mac)."""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat:
        return token, chat
    try:
        with open(ARCHIVO_TELEGRAM, encoding="utf-8") as f:
            d = json.load(f)
        return d["token"], str(d["chat_id"])
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None


def partir_texto(texto, limite=4000):
    """Telegram acepta mensajes de hasta 4096 caracteres: se parte por líneas si es más largo."""
    trozos, actual = [], ""
    for linea in texto.split("\n"):
        if len(actual) + len(linea) + 1 > limite and actual:
            trozos.append(actual.rstrip("\n"))
            actual = ""
        actual += linea + "\n"
    if actual.strip():
        trozos.append(actual.rstrip("\n"))
    return trozos


def enviar_telegram(texto):
    """Envía el texto por Telegram. Devuelve True si se envió. Nunca detiene el escaneo."""
    cred = cargar_telegram()
    if not cred:
        return False
    token, chat_id = cred
    try:
        for trozo in partir_texto(texto):
            telegram_api(token, "sendMessage", {
                "chat_id": chat_id,
                "text": trozo,
                "disable_web_page_preview": "false" if TELEGRAM_VISTA_PREVIA else "true",
            })
        return True
    except Exception as err:
        print(f"No pude enviar el aviso por Telegram ({err}). El escaneo sigue igual.")
        return False


def formatear_aviso(nombre, ofertas, primera_vez):
    """Arma el mensaje con las novedades de una dirección. Devuelve None si no hay novedades."""
    novedades = [o for o in ofertas if o["novedad"]]
    if not novedades:
        return None
    nuevas = sum(1 for o in novedades if o["novedad"] == "nueva")
    rebajadas = len(novedades) - nuevas
    tope = PRIMERA_VEZ_TOP if primera_vez else MAX_AVISOS_POR_URL

    txt_nuevas = f"{nuevas} nueva" if nuevas == 1 else f"{nuevas} nuevas"
    txt_rebajadas = f"{rebajadas} bajó de precio" if rebajadas == 1 else f"{rebajadas} bajaron de precio"
    lineas = [
        f"🔔 Ofertas Easy · {nombre}",
        f"{txt_nuevas}, {txt_rebajadas}",
        "",
    ]
    if primera_vez:
        lineas += [f"(Primera vez en esta categoría: te muestro solo las {tope} mejores; "
                   "el resto quedó guardado como base.)", ""]
    for o in novedades[:tope]:
        if o["novedad"] == "nueva":
            lineas.append(f"🆕 -{o['descuento_pct']}%  {_precio(o['precio_oferta'])}  "
                          f"(antes {_precio(o['precio_normal'])})")
        else:
            lineas.append(f"📉 -{o['descuento_pct']}%  {_precio(o['precio_oferta'])}  "
                          f"({o['novedad']})")
        lineas.append(o["nombre"][:90])
        lineas.append(o["enlace"])
        lineas.append("")
    if len(novedades) > tope:
        lineas.append(f"... y {len(novedades) - tope} novedades más en el CSV")
    return "\n".join(lineas).rstrip()


def avisar(nombre, ofertas, primera_vez):
    """Envía por Telegram las novedades de una dirección (si Telegram está configurado)."""
    if not AVISAR_TELEGRAM or not cargar_telegram():
        return
    texto = formatear_aviso(nombre, ofertas, primera_vez)
    if texto:
        if enviar_telegram(texto):
            print(f"Aviso enviado por Telegram ({nombre}).")
    elif AVISAR_SIN_NOVEDADES and ofertas:
        enviar_telegram(f"Ofertas Easy · {nombre}: sin novedades.")


def configurar_telegram():
    """Guía paso a paso para conectar el script con tu bot de Telegram."""
    print("\nAntes de seguir, en Telegram:")
    print("  1) Busca @BotFather y escríbele /newbot")
    print("  2) Ponle un nombre y un usuario que termine en 'bot' (ej.: johana_ofertas_bot)")
    print("  3) BotFather te responderá con un 'token' (una clave larga). Cópialo.\n")
    token = input("Pega aquí el token y presiona Enter: ").strip()
    try:
        yo = telegram_api(token, "getMe")
        usuario = yo["result"]["username"]
    except Exception as err:
        print(f"\nEse token no funcionó ({err}). Revisa que lo copiaste completo y vuelve a intentar.")
        return
    print(f"\nBot encontrado: @{usuario}")
    input(
        f"Ahora abre @{usuario} en Telegram, presiona 'Iniciar' y envíale cualquier mensaje "
        "(por ejemplo: hola).\nCuando lo hayas hecho, vuelve aquí y presiona Enter ... "
    )
    try:
        r = telegram_api(token, "getUpdates")
        chats = [u["message"]["chat"]["id"] for u in r["result"] if "message" in u]
    except Exception as err:
        print(f"\nNo pude leer tus mensajes ({err}).")
        return
    if not chats:
        print("\nNo vi ningún mensaje. Escríbele algo al bot y vuelve a ejecutar --configurar-telegram.")
        return
    with open(ARCHIVO_TELEGRAM, "w", encoding="utf-8") as f:
        json.dump({"token": token, "chat_id": chats[-1]}, f)
    print(f"\nGuardado en {ARCHIVO_TELEGRAM}")
    print("(Ese archivo contiene la clave de tu bot: no lo compartas.)")
    if enviar_telegram("✅ Listo: el escáner de ofertas de Easy ya puede avisarte por aquí."):
        print("Te mandé un mensaje de prueba. Revisa tu Telegram.")


def borrar_historiales(objetivo=None):
    """Borra el historial de una dirección (si se indica) o el de todas."""
    if objetivo:
        rutas = [ruta_historial(objetivo)]
    elif os.path.isdir(CARPETA_HISTORIALES):
        rutas = [
            os.path.join(CARPETA_HISTORIALES, f)
            for f in os.listdir(CARPETA_HISTORIALES) if f.endswith(".json")
        ]
    else:
        rutas = []
    borrados = 0
    for r in rutas:
        if os.path.exists(r):
            os.remove(r)
            borrados += 1
    if borrados:
        print(f"Historial borrado ({borrados} archivo(s)). La próxima vez todo contará como nuevo.")
    else:
        print("No había historial guardado.")


def main():
    if "--configurar-ubicacion" in sys.argv:
        configurar_ubicacion()
        return
    if "--configurar-telegram" in sys.argv:
        configurar_telegram()
        return
    if "--borrar-historial" in sys.argv:
        i = sys.argv.index("--borrar-historial")
        objetivo = sys.argv[i + 1] if i + 1 < len(sys.argv) and not sys.argv[i + 1].startswith("--") else None
        borrar_historiales(objetivo)
        return
    if "--probar" in sys.argv:
        i = sys.argv.index("--probar")
        if i + 1 < len(sys.argv):
            probar(sys.argv[i + 1])
        else:
            print("Uso: python3 escaner_easy.py --probar https://www.easy.cl/...../p")
        return
    if "--diagnostico" in sys.argv:
        i = sys.argv.index("--diagnostico")
        if i + 1 < len(sys.argv):
            diagnostico(sys.argv[i + 1])
        else:
            print("Uso: python3 escaner_easy.py --diagnostico https://www.easy.cl/...../p")
        return

    if os.path.exists(ESTADO):
        print("Usando la ubicación guardada.")
    else:
        print(
            "Aviso: no hay ubicación guardada, así que el stock puede no coincidir con tu comuna.\n"
            "Para guardarla: python3 escaner_easy.py --configurar-ubicacion\n"
        )

    if AVISAR_TELEGRAM and not cargar_telegram():
        print("Telegram sin configurar (para recibir avisos: python3 escaner_easy.py --configurar-telegram)\n")

    # Para pruebas (o en la nube) se puede limitar a las primeras N direcciones: EASY_LIMITE_URLS=2
    limite = int(os.environ.get("EASY_LIMITE_URLS", "0") or 0)
    urls = URLS[:limite] if limite > 0 else URLS
    tarjetas_total = 0
    todas = []
    por_url = []        # (nombre, ofertas, primera_vez) de cada dirección
    cache_corrida = {}  # lo verificado en este escaneo, para no repetir entre direcciones

    # Cada dirección se procesa completa (recorrer, filtrar, verificar, comparar con SU
    # historial) antes de pasar a la siguiente. El avance se guarda después de cada una.
    for n, url in enumerate(urls, 1):
        nombre = nombre_corto(url)
        print(f"\n=== Dirección {n} de {len(urls)}: {url}")
        ruta = ruta_historial(url)
        historial = cargar_historial(ruta)
        primera_vez = not historial
        try:
            tarjetas = raspar_listado(url)
        except Exception as err:
            print(f"No pude recorrer esta dirección ({err}). Sigo con la siguiente.")
            continue

        tarjetas_total += len(tarjetas)
        ofertas = ordenar(procesar_url(tarjetas, historial, cache_corrida, nombre))
        guardar_historial(historial, ruta)
        if CSV_POR_URL:
            escribir_csv(ofertas, os.path.join(CARPETA_POR_URL, f"{nombre}.csv"))
        por_url.append((nombre, ofertas, primera_vez))
        todas.extend(ofertas)
        escribir_csv(unir(todas), SALIDA)
        avisar(nombre, ofertas, primera_vez)

    # --- Resumen por dirección: solo se muestran las novedades ---
    print("\n" + "=" * 60)
    for nombre, ofertas, primera_vez in por_url:
        nuevas = sum(1 for o in ofertas if o["novedad"] == "nueva")
        rebajadas = sum(1 for o in ofertas if o["novedad"].startswith("bajó"))
        extra = "  (primera vez con historial: todo cuenta como nuevo)" if primera_vez else ""
        print(f"\n[{nombre}] {len(ofertas)} ofertas: {nuevas} nuevas, {rebajadas} bajaron de precio{extra}")
        novedades = [o for o in ofertas if o["novedad"]]
        for o in novedades[:MOSTRAR_TOP]:
            print(
                f"  [{o['novedad']}]  -{o['descuento_pct']}%  ${o['precio_oferta']:,}  "
                f"(antes ${o['precio_normal']:,})  {o['nombre'][:60]}".replace(",", ".")
            )
            print(f"      {o['enlace']}")
        if len(novedades) > MOSTRAR_TOP:
            print(f"  ... y {len(novedades) - MOSTRAR_TOP} novedades más en el CSV")
        if ofertas and not novedades:
            print("  Sin novedades desde la última vez.")

    if todas:
        print(f"\nGuardado ({datetime.now():%Y-%m-%d %H:%M}):")
        print(f"  Todas juntas, sin repetidos: {SALIDA}")
        if CSV_POR_URL:
            print(f"  Un archivo por dirección:    {CARPETA_POR_URL}")
    else:
        print("\nNo se encontraron ofertas. Revisa las direcciones o ajusta los filtros.")

    # Si no se leyó NINGÚN producto, algo falló (por ejemplo, el sitio bloqueó la conexión):
    # se sale con error para que, en la nube, se note.
    if tarjetas_total == 0:
        print("\nERROR: no se leyó ningún producto de ninguna dirección.")
        sys.exit(1)

    # Aviso final opcional (se activa con EASY_RESUMEN_FINAL=1): así sabes que corrió aunque no haya novedades
    if os.environ.get("EASY_RESUMEN_FINAL") == "1":
        novedades_total = sum(1 for o in todas if o["novedad"])
        enviar_telegram(
            f"✅ Escaneo de Easy terminado: {len(por_url)} de {len(urls)} direcciones leídas, "
            f"{len(unir(todas))} ofertas, {novedades_total} novedades."
        )


if __name__ == "__main__":
    main()
