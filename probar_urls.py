#!/usr/bin/env python3
"""
probar_urls.py — Comprueba si unas direcciones web responden bien desde donde lo ejecutas.

Para cada dirección dice si está OK, BLOQUEADA, NO ENCONTRADA, ERROR o para REVISAR, con el
código HTTP, el tiempo, el título de la página y si redirige a otra dirección.
Funciona con cualquier sitio, no solo con Easy.

Dos modos:
  • Con navegador (por defecto): abre cada dirección en un Chromium real, como hace el
    escáner de ofertas. Es el más fiel: ve lo mismo que vería una persona.
    Necesita Playwright:  pip3 install playwright  y  python3 -m playwright install chromium
  • Rápido (--rapido): una petición simple, sin navegador y sin instalar nada. Es más veloz,
    pero no ejecuta JavaScript, y algunos sitios responden distinto a una petición simple.

Ejemplos:
  python3 probar_urls.py https://www.easy.cl/cluster/6826 https://www.google.com
  python3 probar_urls.py --archivo urls.txt
  python3 probar_urls.py --rapido https://www.easy.cl
  python3 probar_urls.py --captura --csv resultado.csv --archivo urls.txt
  python3 probar_urls.py            (sin direcciones: usa la lista URLS de este archivo)

Código de salida: 0 = todas OK, 1 = alguna no está OK, 2 = error de uso.
"""

import argparse
import csv
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

# ----------------------- LISTA POR DEFECTO -----------------------
# Se usa cuando ejecutas el script sin escribir direcciones. Una por línea, con comillas y coma.
URLS = [
    "https://www.easy.cl/cluster/6826",
    "https://www.easy.cl/decoracion-e-iluminacion/iluminacion/iluminacion-exterior",
    "https://www.example.com",
]
# -----------------------------------------------------------------

CARPETA_CAPTURAS = "capturas_urls"

# Textos típicos de páginas de bloqueo o de verificación anti-robots
PATRONES_BLOQUEO = re.compile(
    r"access denied|request could not be satisfied|request blocked|just a moment|"
    r"attention required|captcha|verify (that )?you are (a )?human|are you a robot|"
    r"pardon our interruption|unusual traffic|acceso denegado|has sido bloqueado",
    re.I,
)
PRECIO = re.compile(r"\$\s?\d{1,3}(?:\.\d{3})+|\$\s?\d+")


# ---------------------- utilidades ----------------------

def normalizar(url):
    """Agrega https:// si la dirección no tiene esquema."""
    url = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    return url


def leer_archivo(ruta):
    """Una dirección por línea. Se ignoran las líneas vacías y las que empiezan con #."""
    with open(ruta, encoding="utf-8-sig") as f:
        return [l.strip() for l in f if l.strip() and not l.strip().startswith("#")]


def nombre_corto(url):
    p = urlparse(url)
    return (p.netloc + p.path).rstrip("/")[:60] or url[:60]


def clasificar(estado, titulo, texto, error):
    """Decide el veredicto: OK, BLOQUEADA, NO ENCONTRADA, ERROR o REVISAR."""
    if error:
        return "ERROR"
    if estado in (401, 403, 429, 451):
        return "BLOQUEADA"
    texto = texto or ""
    if PATRONES_BLOQUEO.search(titulo or "") or (len(texto) < 1500 and PATRONES_BLOQUEO.search(texto[:400])):
        return "BLOQUEADA"
    if estado in (404, 410):
        return "NO ENCONTRADA"
    if estado is None:
        return "ERROR"
    if estado >= 400:
        return "REVISAR"
    if len(texto.strip()) < 50:
        return "REVISAR"  # respondió bien, pero la página está vacía
    return "OK"


# ---------------------- modo rápido (sin navegador) ----------------------

def _texto_de_html(html):
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html).strip()


def probar_rapido(url, args):
    t0 = time.time()
    estado, titulo, texto, final, error = None, "", "", url, ""
    peticion = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; probar_urls/1.0)",
        "Accept-Language": "es-CL,es;q=0.9,en;q=0.8",
    })
    cuerpo = b""
    try:
        with urllib.request.urlopen(peticion, timeout=args.timeout) as r:
            estado, final, cuerpo = r.status, r.geturl(), r.read(300000)
    except urllib.error.HTTPError as err:
        estado, final = err.code, url
        try:
            cuerpo = err.read(300000)
        except Exception:
            cuerpo = b""
    except Exception as err:
        error = str(getattr(err, "reason", err))[:150]
        if "CERTIFICATE_VERIFY_FAILED" in str(err):
            error += (" | En Mac: abre la carpeta 'Python 3.x' en Aplicaciones y ejecuta "
                      "'Install Certificates.command', o usa el modo con navegador.")
    if cuerpo:
        html = cuerpo.decode("utf-8", errors="replace")
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
        titulo = re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        texto = _texto_de_html(html)
    return {
        "url": url, "veredicto": clasificar(estado, titulo, texto, error), "estado": estado,
        "segundos": time.time() - t0, "titulo": titulo, "url_final": final,
        "precios": "-", "detalle": error or texto[:160],
    }


# ---------------------- modo con navegador ----------------------

def _probar_una_con_navegador(page, url, args, indice):
    t0 = time.time()
    estado, titulo, texto, final, error = None, "", "", url, ""
    try:
        respuesta = page.goto(url, wait_until="domcontentloaded", timeout=args.timeout * 1000)
        estado = respuesta.status if respuesta else None
        try:
            page.wait_for_load_state("load", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(int(args.espera * 1000))
        for _ in range(3):  # si la página navega justo al leerla, espera y reintenta
            try:
                titulo, texto, final = page.title(), page.inner_text("body"), page.url
                break
            except Exception as err:
                if "context was destroyed" in str(err).lower() or "navigation" in str(err).lower():
                    page.wait_for_timeout(2000)
                    continue
                raise
        if args.captura:
            os.makedirs(CARPETA_CAPTURAS, exist_ok=True)
            seguro = re.sub(r"[^a-z0-9]+", "-", nombre_corto(url).lower()).strip("-")[:50]
            page.screenshot(path=os.path.join(CARPETA_CAPTURAS, f"{indice:02d}_{seguro}.png"))
    except Exception as err:
        error = str(err).splitlines()[0][:150] if str(err) else err.__class__.__name__
    texto_limpio = " ".join(texto.split())
    return {
        "url": url, "veredicto": clasificar(estado, titulo, texto_limpio, error), "estado": estado,
        "segundos": time.time() - t0, "titulo": titulo, "url_final": final,
        "precios": len(PRECIO.findall(texto)), "detalle": error or texto_limpio[:160],
    }


def probar_con_navegador(urls, args, mostrar):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Falta Playwright. Instálalo con:\n  pip3 install playwright\n"
              "  python3 -m playwright install chromium\n"
              "o usa --rapido, que no lo necesita.")
        sys.exit(2)
    resultados = []
    with sync_playwright() as p:
        navegador = p.chromium.launch(headless=True)
        try:
            opciones = {"locale": "es-CL"}
            if args.estado:
                opciones["storage_state"] = args.estado
            page = navegador.new_context(**opciones).new_page()
            for n, url in enumerate(urls, 1):
                r = _probar_una_con_navegador(page, url, args, n)
                resultados.append(r)
                mostrar(n, len(urls), r)
                if n < len(urls):
                    time.sleep(args.pausa)
        finally:
            navegador.close()
    return resultados


# ---------------------- salida ----------------------

def mostrar_resultado(n, total, r):
    estado = "-" if r["estado"] is None else r["estado"]
    print(f"{n:>2}/{total}  {r['veredicto']:<13} HTTP {estado!s:<4} {r['segundos']:>4.1f}s  "
          f"{nombre_corto(r['url'])}  | {r['titulo'][:50]}")
    if r["url_final"] and r["url_final"].rstrip("/") != r["url"].rstrip("/"):
        print(f"      redirige a: {r['url_final'][:110]}")
    if r["veredicto"] in ("BLOQUEADA", "ERROR", "REVISAR") and r["detalle"]:
        print(f"      detalle: {r['detalle'][:150]}")


def resumen(resultados):
    por_veredicto = {}
    for r in resultados:
        por_veredicto.setdefault(r["veredicto"], []).append(r)
    orden = ["OK", "BLOQUEADA", "NO ENCONTRADA", "ERROR", "REVISAR"]
    partes = [f"{len(por_veredicto[v])} {v}" for v in orden if v in por_veredicto]
    print(f"\nRESULTADO ({len(resultados)} direcciones): " + ", ".join(partes))
    for v in orden[1:]:
        for r in por_veredicto.get(v, []):
            print(f"  - {v}: {r['url']}")
    if os.environ.get("GITHUB_ACTIONS") == "true":  # resumen visible en 'Annotations' de GitHub
        detalle = " | ".join(f"{nombre_corto(r['url'])}: {r['veredicto']} HTTP {r['estado']}" for r in resultados)[:1500]
        print(f"::notice title=Direcciones: {', '.join(partes)}::{detalle}")


def guardar_csv(resultados, ruta):
    campos = ["veredicto", "estado", "segundos", "precios", "titulo", "url", "url_final", "detalle"]
    with open(ruta, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos, delimiter=";", extrasaction="ignore")
        w.writeheader()
        for r in resultados:
            fila = dict(r)
            fila["segundos"] = f"{r['segundos']:.1f}"
            w.writerow(fila)
    print(f"CSV guardado en {ruta}")


# ---------------------- programa principal ----------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Comprueba si unas direcciones web responden bien desde donde lo ejecutas.",
        epilog="Ejemplo: python3 probar_urls.py https://www.easy.cl --rapido")
    ap.add_argument("urls", nargs="*", help="direcciones a probar (con o sin https://)")
    ap.add_argument("--archivo", "-a", help="archivo de texto con una dirección por línea")
    ap.add_argument("--rapido", action="store_true", help="petición simple, sin navegador (no instala nada)")
    ap.add_argument("--captura", action="store_true", help="guarda una captura de cada página (solo con navegador)")
    ap.add_argument("--estado", help="archivo de ubicación/sesión de Playwright, ej.: easy_ubicacion.json")
    ap.add_argument("--espera", type=float, default=3.0, help="segundos de espera tras cargar (por defecto 3)")
    ap.add_argument("--pausa", type=float, default=1.0, help="segundos entre una dirección y otra (por defecto 1)")
    ap.add_argument("--timeout", type=int, default=45, help="segundos máximos por dirección (por defecto 45)")
    ap.add_argument("--csv", help="guarda el resultado en este archivo CSV")
    args = ap.parse_args(argv)

    urls = list(args.urls)
    if args.archivo:
        try:
            urls += leer_archivo(args.archivo)
        except OSError as err:
            print(f"No pude leer {args.archivo}: {err}")
            return 2
    if not urls:
        urls = list(URLS)
    urls = [normalizar(u) for u in urls]
    if args.estado and not os.path.exists(args.estado):
        print(f"No encuentro el archivo de ubicación: {args.estado}")
        return 2

    modo = "rápido, sin navegador" if args.rapido else "con navegador"
    print(f"Probando {len(urls)} direcciones ({modo}) ...\n")

    if args.rapido:
        resultados = []
        for n, url in enumerate(urls, 1):
            r = probar_rapido(url, args)
            resultados.append(r)
            mostrar_resultado(n, len(urls), r)
            if n < len(urls):
                time.sleep(args.pausa)
    else:
        resultados = probar_con_navegador(urls, args, mostrar_resultado)

    resumen(resultados)
    if args.csv:
        guardar_csv(resultados, args.csv)
    return 0 if all(r["veredicto"] == "OK" for r in resultados) else 1


if __name__ == "__main__":
    sys.exit(main())
