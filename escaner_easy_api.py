#!/usr/bin/env python3
"""
escaner_easy_api.py - Escáner rápido de ofertas de Easy (sin navegador)

Lee directo los datos JSON que Easy entrega para cada categoría
(_next/data/.../cluster/<id>.json?page=N, 40 productos por página), con la comuna
de Huechuraba enviada por cookie. Filtra, compara con el historial y avisa por Telegram.

Uso:
    python escaner_easy_api.py --prueba          # prueba rápida de 1 URL (hacer esto primero)
    python escaner_easy_api.py --sin-telegram    # escaneo completo, solo imprime (no toca el historial)
    python escaner_easy_api.py                   # escaneo completo + Telegram + historial
    python escaner_easy_api.py --todo            # ignora el historial (avisa todo lo que cumpla)

Las URLs se leen de urls_easy.txt (una por línea; las líneas con # se ignoran).
Solo usa la librería estándar de Python.
"""
import argparse
import csv
import gzip
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ====================== CONFIGURACIÓN ======================
ARCHIVO_URLS = Path("urls_easy.txt")
HISTORIAL = Path("historial_easy.json")
ARCHIVO_CSV = Path("ofertas_easy_api.csv")   # se reescribe en cada corrida
CSV_SEPARADOR = ";"              # ";" abre bien en Excel en español; usa "," si prefieres
COMUNA_CODIGO = "13107"          # Huechuraba
REGION = "Metropolitana"
# Cookie X-Constructor-Sellers de tu navegador SIN 'mkt' (marketplace): Easy entrega solo productos
# vendidos por Easy (en las pruebas, 118.000 -> 508 en 'cuadros-y-marcos'). Se usa mientras
# SOLO_VENDIDO_POR_EASY = True; con --con-marketplace se incluyen también los de terceros.
VENDEDORES_SIN_MKT = ("50159080%2C503%2C510%2C512%2C518%2C522%2C5990001%2C5990003%2C599vp%2C659"
                      "%2C77137860%2C77924090%2C843%2C93129000%2C93830000%2C988")
MIN_DESCUENTO = 50               # % mínimo de descuento
PRECIO_MAX = 20000               # precio de oferta máximo (CLP)
SOLO_VENDIDO_POR_EASY = True     # descarta vendedores externos (marketplace)
USAR_PRECIO_TARJETA = False      # True = usar el precio con tarjeta (brandPrice) si es menor
MAX_PAGINAS = 200                # tope de páginas por URL (40 productos por página)
HILOS = 6                        # URLs que se escanean en paralelo
PAGINAS_PARALELO = 4             # páginas de una misma URL que se bajan a la vez
MAX_CONEXIONES = 8               # tope de conexiones simultáneas a Easy en total
PAUSA = 0.1                      # segundos de espera antes de pedir cada página
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT = os.environ.get("TELEGRAM_CHAT_ID")
# ===========================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-CL,es;q=0.9",
    "Accept-Encoding": "gzip",
}

BUILD = {"id": None}
BUILD_LOCK = threading.Lock()
LIMITE = threading.Semaphore(MAX_CONEXIONES)
STATS_LOCK = threading.Lock()
SELLERS = {"valor": None}  # se llena con --sin-marketplace
# Ruta JSON para categorías (/_next/data/...). EXPERIMENTAL y apagada por defecto: en las pruebas
# no fue más rápida que el HTML y en una categoría entregó datos distintos. Se activa con --json-rapido.
DATOS_RAPIDOS = {"activo": False, "ok": 0, "fallos": 0, "html": 0}
SIN_JSON = set()  # rutas que no entregaron datos por JSON: no se vuelve a intentar con ellas


class Bloqueado(Exception):
    pass


def _leer(resp):
    """Lee el cuerpo de una respuesta; si viene comprimido (gzip) lo descomprime."""
    datos = resp.read()
    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
        try:
            datos = gzip.decompress(datos)
        except Exception:
            pass
    return datos.decode("utf-8", "replace")


def http_get(url, con_comuna=True):
    h = dict(HEADERS)
    if con_comuna:
        cookie = f"X-Constructor-Commune-Code={COMUNA_CODIGO}; X-Constructor-Region={REGION}"
        if SELLERS["valor"]:
            cookie += f"; X-Constructor-Sellers={SELLERS['valor']}"
        h["Cookie"] = cookie
    ultimo = (None, "sin respuesta")
    for intento in range(4):
        try:
            req = urllib.request.Request(url, headers=h)
            with LIMITE:
                with urllib.request.urlopen(req, timeout=40) as r:
                    return r.status, _leer(r)
        except urllib.error.HTTPError as e:
            cuerpo = _leer(e)
            if e.code == 403:
                raise Bloqueado(f"Easy respondió 403 (bloqueado) en {url[:90]}")
            if e.code in (429, 500, 502, 503, 504):
                ultimo = (e.code, cuerpo)
                time.sleep(2 * (intento + 1))
                continue
            return e.code, cuerpo
        except Exception as e:
            ultimo = (None, str(e))
            time.sleep(1.5 * (intento + 1))
    return ultimo


def obtener_build(forzar=False):
    """buildId de Next.js: cambia cuando Easy actualiza el sitio."""
    with BUILD_LOCK:
        if BUILD["id"] and not forzar:
            return BUILD["id"]
        st, txt = http_get("https://www.easy.cl/", con_comuna=False)
        m = re.search(r'"buildId":"([^"]+)"', txt or "")
        if st != 200 or not m:
            raise RuntimeError(f"No pude leer el buildId de Easy (status {st})")
        BUILD["id"] = m.group(1)
        return BUILD["id"]


def _next_data_categoria(p, q, con_comuna=True):
    """Lee una categoría por /_next/data/<build>/<ruta>.json (JSON liviano, sin armar el HTML).
    Devuelve serverProductsResponse o None si esa ruta no entrega los datos."""
    ruta = p.path.rstrip("/")
    if not ruta or ruta in SIN_JSON:
        return None
    qs = urlencode(q)
    for intento in range(2):
        build = obtener_build(forzar=(intento == 1))
        url = f"https://www.easy.cl/_next/data/{build}{ruta}.json" + (f"?{qs}" if qs else "")
        st, txt = http_get(url, con_comuna)
        if st == 200:
            try:
                srv = json.loads(txt)["pageProps"]["serverProductsResponse"]
                return srv if isinstance(srv, dict) else None
            except Exception:
                return None
        # 404: si antes funcionó, probablemente Easy cambió de versión (buildId): reintenta con el nuevo
        if st != 404 or DATOS_RAPIDOS["ok"] == 0:
            return None
    return None


def traer_pagina(url, n, con_comuna=True):
    """Devuelve serverProductsResponse (productList, recordsFiltered...) de la página n."""
    p = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k != "page"]
    if n > 1:
        q.append(("page", str(n)))

    m = re.fullmatch(r"/cluster/(\d+)/?", p.path)
    if m:  # ruta rápida: JSON directo
        cid = m.group(1)
        for intento in range(2):
            build = obtener_build(forzar=(intento == 1))
            qs = urlencode([("clusterId", cid)] + q)
            st, txt = http_get(
                f"https://www.easy.cl/_next/data/{build}/cluster/{cid}.json?{qs}", con_comuna
            )
            if st == 200:
                try:
                    return json.loads(txt)["pageProps"]["serverProductsResponse"]
                except Exception:
                    return None
        return None

    # Categorías y búsquedas: primero el JSON liviano; si no responde, el HTML
    if DATOS_RAPIDOS["activo"]:
        srv = _next_data_categoria(p, q, con_comuna)
        with STATS_LOCK:
            if srv is not None:
                DATOS_RAPIDOS["ok"] += 1
            else:
                DATOS_RAPIDOS["fallos"] += 1
                SIN_JSON.add(p.path.rstrip("/"))
                if DATOS_RAPIDOS["ok"] == 0 and DATOS_RAPIDOS["fallos"] >= 3:
                    DATOS_RAPIDOS["activo"] = False  # no funciona: no seguir intentando
        if srv is not None:
            return srv
    with STATS_LOCK:
        DATOS_RAPIDOS["html"] += 1

    # Cualquier otra URL: los mismos datos vienen embebidos en el HTML (__NEXT_DATA__)
    full = urlunsplit((p.scheme or "https", p.netloc or "www.easy.cl", p.path, urlencode(q), ""))
    st, txt = http_get(full, con_comuna)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', txt or "", re.S)
    if st != 200 or not m:
        return None
    try:
        return json.loads(m.group(1))["props"]["pageProps"]["serverProductsResponse"]
    except Exception:
        return None


def escanear_url(url, max_pag):
    """Baja la página 1 (para saber el total) y el resto en paralelo.
    Devuelve (productos, ok, paginas_con_error)."""
    primera = traer_pagina(url, 1)
    lista1 = (primera or {}).get("productList") or []
    if not lista1:
        # Respuesta válida pero sin productos (p. ej. una categoría que solo tiene vendedores
        # externos y se pidió sin marketplace): no es un error, solo está vacía.
        vacia = isinstance(primera, dict) and (primera.get("recordsFiltered") or 0) == 0
        return [], vacia, 0, 0, False
    total = primera.get("recordsFiltered") or len(lista1)
    necesarias = -(-total // len(lista1))  # páginas que haría falta leer (división hacia arriba)
    n_paginas = min(max_pag, necesarias)
    capado = necesarias > max_pag          # True solo si el tope de páginas dejó productos sin leer
    productos = list(lista1)
    malas = 0

    def una(n):
        for intento in range(3):  # hasta 3 intentos si la página viene vacía o con error
            time.sleep(PAUSA + intento * 1.5)
            lista = (traer_pagina(url, n) or {}).get("productList")
            if lista:
                return lista
        return None

    if n_paginas > 1:
        with ThreadPoolExecutor(max_workers=PAGINAS_PARALELO) as ex:
            for lista in ex.map(una, range(2, n_paginas + 1)):
                if lista:
                    productos.extend(lista)
                else:
                    malas += 1
    return productos, True, malas, total, capado


def evaluar(p):
    """Devuelve un dict con la oferta si el producto cumple las reglas; si no, None."""
    pr = p.get("prices") or {}
    normal, oferta, tarjeta = pr.get("normalPrice"), pr.get("offerPrice"), pr.get("brandPrice")
    precio = oferta
    if USAR_PRECIO_TARJETA and tarjeta and (not precio or tarjeta < precio):
        precio = tarjeta
    if not normal or not precio or precio >= normal:
        return None
    if (p.get("availableQuantity") or 0) <= 0:
        return None
    if SOLO_VENDIDO_POR_EASY and p.get("isMarketplace"):
        return None
    desc = round((1 - precio / normal) * 100)
    if desc < MIN_DESCUENTO or precio > PRECIO_MAX:
        return None
    return {
        "sku": str(p.get("sku")),
        "nombre": p.get("productName") or "",
        "marca": p.get("brand") or "",
        "normal": normal,
        "precio": precio,
        "tarjeta": tarjeta,
        "desc": desc,
        "stock": p.get("availableQuantity"),
        "url": "https://www.easy.cl/" + (p.get("linkText") or ""),
    }


def cargar_urls(archivo):
    if archivo.exists():
        lineas = [l.strip() for l in archivo.read_text(encoding="utf-8").splitlines()]
        urls = [l for l in lineas if l and not l.startswith("#")]
        if urls:
            return urls
    print(f"(No encontré {archivo} o está vacío; uso una URL de ejemplo)")
    return ["https://www.easy.cl/cluster/6826"]


def clp(n):
    return "$" + f"{int(n):,}".replace(",", ".")


def enviar_telegram(texto):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT):
        print("(Telegram sin configurar: define TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID)")
        return False
    data = urlencode({"chat_id": TELEGRAM_CHAT, "text": texto, "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", data=data),
            timeout=30,
        ).read()
        return True
    except Exception as e:
        print(f"No pude enviar a Telegram: {e}")
        return False


def guardar_csv(encontrados, avisar, ruta):
    """CSV con TODAS las ofertas que cumplen las reglas en esta corrida."""
    previos = {e["sku"]: antes for e, antes in avisar}
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M")
    columnas = ["sku", "marca", "nombre", "descuento_pct", "precio_oferta", "precio_normal",
                "precio_tarjeta", "stock", "nuevo_o_mas_barato", "precio_anterior",
                "url", "categoria_origen", "fecha"]
    with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=CSV_SEPARADOR)
        w.writerow(columnas)
        for e in sorted(encontrados.values(), key=lambda x: (-x["desc"], x["precio"])):
            es_aviso = e["sku"] in previos
            antes = previos.get(e["sku"])
            w.writerow([e["sku"], e["marca"], e["nombre"], e["desc"], e["precio"], e["normal"],
                        e["tarjeta"] if e["tarjeta"] else "", e["stock"],
                        "si" if es_aviso else "no", antes["precio"] if antes else "",
                        e["url"], e.get("origen", ""), fecha])
    print(f"CSV guardado: {Path(ruta).resolve()} ({len(encontrados)} filas)")


def prueba(url):
    print(f"Probando: {url}\n")
    t = time.time()
    con = traer_pagina(url, 1, True)
    dt = time.time() - t
    sin = traer_pagina(url, 1, False)
    if not con:
        print("No pude leer la lista de productos. Si no hubo mensaje de bloqueo, la URL "
              "podría no ser una categoría con productos.")
        return
    lc, ls = con.get("productList") or [], (sin or {}).get("productList") or []
    print(f"Con comuna {COMUNA_CODIGO}: {len(lc)} productos en la página 1, "
          f"{con.get('recordsFiltered')} en total (tardó {dt:.1f} s)")
    print(f"Sin cookie de comuna: {len(ls)} productos, {(sin or {}).get('recordsFiltered')} en total")
    idx = {str(p.get("sku")): p for p in ls}
    print("\nPrimeros productos:")
    for p in lc[:8]:
        pr = p.get("prices") or {}
        s = idx.get(str(p.get("sku")), {})
        print(f"  {p.get('sku')} | {str(p.get('productName'))[:38]:38} | normal {pr.get('normalPrice')} "
              f"oferta {pr.get('offerPrice')} tarjeta {pr.get('brandPrice')} | stock {p.get('availableQuantity')} "
              f"(sin comuna: {s.get('availableQuantity')}) | marketplace {p.get('isMarketplace')} "
              f"| precio por zona {p.get('isPriceByZone')}")
    p2 = traer_pagina(url, 2, True)
    ids1 = {str(p.get("sku")) for p in lc}
    ids2 = {str(p.get("sku")) for p in (p2 or {}).get("productList") or []}
    print(f"\nPágina 2: {len(ids2)} productos, {len(ids2 - ids1)} distintos a la página 1")
    cumplen = [e for e in (evaluar(p) for p in lc) if e]
    print(f"De la página 1, cumplen tus reglas (≥{MIN_DESCUENTO}%, ≤{clp(PRECIO_MAX)}): {len(cumplen)}")
    print("\nSi 'Con comuna' y 'Sin cookie' salen idénticos, avísame: habría que enviar más datos de comuna.")


def ejecutar(args):
    urls = cargar_urls(Path(args.urls))
    t0 = time.time()
    print("Modo: " + ("solo productos vendidos por Easy (sin marketplace)" if SELLERS["valor"]
                      else "incluye marketplace (productos de vendedores externos)"))
    obtener_build()
    encontrados, total_vistos, fallidas = {}, 0, []
    hechos = 0
    vacias = 0
    compacto = len(urls) > 100  # con muchas URLs, solo se muestran las que traen algo o fallan

    with ThreadPoolExecutor(max_workers=HILOS) as ex:
        futuros = {ex.submit(escanear_url, u, args.paginas): u for u in urls}
        for f in as_completed(futuros):
            u = futuros[f]
            hechos += 1
            try:
                prods, ok, malas, total, capado = f.result()
            except Bloqueado as e:
                ex.shutdown(wait=False, cancel_futures=True)
                print(f"\n✖ {e}\n  Esta conexión está bloqueada por Easy; no sigo.")
                sys.exit(1)
            except Exception as e:
                fallidas.append(u)
                print(f"✖ {u} -> error: {e}")
                continue
            if not prods:
                if ok:
                    vacias += 1  # categoría sin productos de Easy: normal, no es un error
                    continue
                fallidas.append(u)
                print(f"✖ {u} -> no se pudo leer la lista de productos")
                continue
            total_vistos += len(prods)
            n_ok = 0
            for p in prods:
                e = evaluar(p)
                if e and (e["sku"] not in encontrados or e["desc"] > encontrados[e["sku"]]["desc"]):
                    e["origen"] = u
                    encontrados[e["sku"]] = e
                    n_ok += 1
            tope = (f"  ⚠ TOPE: Easy reporta {total} productos y solo se leyeron las primeras "
                    f"{args.paginas} páginas (sube --paginas)") if capado else ""
            aviso = f"  ⚠ {malas} página(s) no se pudieron leer" if malas else ""
            if not compacto or n_ok or tope or aviso:
                print(f"✓ [{hechos}/{len(urls)}] {u} -> {len(prods)} productos, {n_ok} cumplen{tope}{aviso}")
            elif hechos % 50 == 0:
                print(f"… {hechos}/{len(urls)} URLs revisadas ({time.time() - t0:.0f} s)")

    historial = {}
    if HISTORIAL.exists() and not args.todo:
        try:
            historial = json.loads(HISTORIAL.read_text(encoding="utf-8"))
        except Exception:
            historial = {}

    avisar = []
    for sku, e in encontrados.items():
        antes = historial.get(sku)
        if antes is None or e["precio"] < antes.get("precio", 10**12):
            avisar.append((e, antes))
    avisar.sort(key=lambda x: -x[0]["desc"])
    guardar_csv(encontrados, avisar, args.csv)

    print(f"\nProductos revisados: {total_vistos} | cumplen: {len(encontrados)} | "
          f"nuevos o más baratos: {len(avisar)} | URLs con problemas: {len(fallidas)} | "
          f"tiempo: {time.time() - t0:.0f} s")
    if vacias:
        print(f"URLs sin productos de Easy (vacías, no es un error): {vacias}")
    if fallidas:
        Path("urls_con_problemas.txt").write_text("\n".join(fallidas) + "\n", encoding="utf-8")
        print(f"URLs con problemas guardadas en {Path('urls_con_problemas.txt').resolve()} "
              f"(se pueden reintentar con --urls urls_con_problemas.txt)")
    if DATOS_RAPIDOS["ok"] or DATOS_RAPIDOS["fallos"]:
        print(f"Páginas de categorías leídas por JSON: {DATOS_RAPIDOS['ok']} | por HTML: {DATOS_RAPIDOS['html']}"
              + ("" if DATOS_RAPIDOS["activo"] else " (el JSON no funcionó y se apagó solo)"))
    for e, antes in avisar[:40]:
        extra = f" (antes {clp(antes['precio'])})" if antes else ""
        print(f"  {e['desc']}% | {clp(e['precio'])} (normal {clp(e['normal'])}){extra} | "
              f"{e['marca']} {e['nombre'][:50]} | {e['url']}")

    if args.sin_telegram:
        print("\n(--sin-telegram: no se envió nada y no se modificó el historial)")
        return

    if avisar:
        bloques = []
        for e, antes in avisar:
            extra = f" (antes {clp(antes['precio'])})" if antes else ""
            bloques.append(f"🔥 {e['desc']}% · {clp(e['precio'])} (normal {clp(e['normal'])}){extra}\n"
                           f"{e['marca']} {e['nombre']}\n{e['url']}")
        mensaje, enviado = "", True
        for b in bloques:
            if len(mensaje) + len(b) > 3500:
                enviado = enviar_telegram(mensaje) and enviado
                mensaje = ""
            mensaje += b + "\n\n"
        if mensaje:
            enviado = enviar_telegram(mensaje) and enviado
        if not enviado:
            print("\n⚠ No se pudo enviar todo por Telegram. NO guardé el historial, "
                  "para no perder estos avisos en la próxima corrida.")
            return

    ahora = datetime.now().isoformat(timespec="seconds")
    for sku, e in encontrados.items():
        historial[sku] = {"precio": e["precio"], "nombre": e["nombre"], "visto": ahora}
    HISTORIAL.write_text(json.dumps(historial, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    global MIN_DESCUENTO, PRECIO_MAX, LIMITE
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-descuento", type=int, default=None, help="reemplaza MIN_DESCUENTO (%%) solo en esta corrida")
    ap.add_argument("--precio-max", type=int, default=None, help="reemplaza PRECIO_MAX solo en esta corrida")
    ap.add_argument("--prueba", action="store_true", help="prueba rápida con la primera URL")
    ap.add_argument("--sin-telegram", action="store_true", help="solo imprime; no envía ni guarda historial")
    ap.add_argument("--todo", action="store_true", help="ignora el historial")
    ap.add_argument("--paginas", type=int, default=MAX_PAGINAS, help="máximo de páginas por URL")
    ap.add_argument("--urls", default=str(ARCHIVO_URLS), help="archivo con las URLs")
    ap.add_argument("--csv", default=str(ARCHIVO_CSV), help="archivo CSV de salida")
    ap.add_argument("--conexiones", type=int, default=None,
                    help=f"conexiones simultáneas a Easy (por defecto {MAX_CONEXIONES}); súbelo con cuidado")
    ap.add_argument("--con-marketplace", action="store_true",
                    help="incluir también productos de vendedores externos (marketplace); mucho más lento")
    ap.add_argument("--json-rapido", action="store_true",
                    help="experimental: leer categorías por /_next/data en vez del HTML")
    args = ap.parse_args()
    if args.min_descuento is not None:
        MIN_DESCUENTO = args.min_descuento
    if args.precio_max is not None:
        PRECIO_MAX = args.precio_max
    if args.conexiones:
        LIMITE = threading.Semaphore(max(1, args.conexiones))
    if args.json_rapido:
        DATOS_RAPIDOS["activo"] = True
    if SOLO_VENDIDO_POR_EASY and not args.con_marketplace:
        SELLERS["valor"] = VENDEDORES_SIN_MKT
    try:
        if args.prueba:
            prueba(cargar_urls(Path(args.urls))[0])
        else:
            ejecutar(args)
    except Bloqueado as e:
        print(f"\n✖ {e}\n  Esta conexión está bloqueada por Easy.")
        sys.exit(1)


if __name__ == "__main__":
    main()