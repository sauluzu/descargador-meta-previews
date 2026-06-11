import streamlit as st
import os
import sys
import asyncio
import tempfile
import subprocess
 
# En Windows, Playwright necesita el ProactorEventLoop para poder lanzar
# subprocesos (el navegador); sin esto puede tronar con NotImplementedError.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
 
# Chromium se instala UNA sola vez por máquina/contenedor. Streamlit
# re-ejecuta este script completo en CADA interacción; sin este guard,
# la instalación corría en cada clic. Ruta y comando multiplataforma
# (Windows local, Linux, Streamlit Cloud).
_MARCADOR_CHROMIUM = os.path.join(tempfile.gettempdir(), "chromium_instalado.flag")
if not os.path.exists(_MARCADOR_CHROMIUM):
    _instalacion = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
    if _instalacion.returncode == 0:
        open(_MARCADOR_CHROMIUM, "w").close()
 
import re
import html as html_lib
import time
import zipfile
import shutil
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.ad import Ad
from facebook_business.adobjects.user import User
 
# --- 1. TUS LLAVES DE META ---
MI_APP_ID = st.secrets["META_APP_ID"]
MI_APP_SECRET = st.secrets["META_APP_SECRET"]
MI_ACCESS_TOKEN = st.secrets["META_ACCESS_TOKEN"]
 
# --- 2. CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(page_title="Meta Previews Automático", page_icon="📸", layout="centered")
st.title("📸 Descargador de Previews - Meta Ads")
st.markdown("Genera capturas de pantalla automáticas de tus anuncios con rendimiento activo.")
st.divider()
 
# --- 3. CONEXIÓN A META ---
try:
    FacebookAdsApi.init(MI_APP_ID, MI_APP_SECRET, MI_ACCESS_TOKEN)
except Exception as e:
    st.error(f"Error al conectar con Meta: {e}")
 
 
@st.cache_data(ttl=3600)
def obtener_mis_cuentas():
    """
    Junta TODAS las cuentas visibles para el token:
      🔑  asignadas directamente al usuario de sistema  -> /me/adaccounts
      🏢  propias de cada Business Manager              -> /{bm}/owned_ad_accounts
      🤝  de clientes/socios de cada Business Manager   -> /{bm}/client_ad_accounts
 
    Las dos últimas requieren el permiso 'business_management' en el token.
    OJO: que una cuenta aparezca como 🏢/🤝 no garantiza que el usuario de
    sistema pueda CONSULTARLA; para eso el activo debe estar asignado al
    usuario de sistema en Business Settings. Las 🔑 sí están garantizadas.
    """
    cuentas = {}  # act_id -> etiqueta visible
 
    def registrar(cursor, etiqueta):
        try:
            for c in cursor:  # el Cursor del SDK pagina solo al iterar
                id_cuenta = c.get('account_id')
                if not id_cuenta or f"act_{id_cuenta}" in cuentas:
                    continue
                nombre = c.get('name', 'Cuenta sin nombre')
                cuentas[f"act_{id_cuenta}"] = f"{etiqueta} {nombre} (act_{id_cuenta})"
        except Exception:
            pass  # sin permiso para este borde: seguimos con lo que sí haya
 
    try:
        yo = User(fbid='me')
        # Primero las asignadas: en el dedupe, su etiqueta 🔑 gana.
        registrar(yo.get_ad_accounts(fields=['name', 'account_id'], params={'limit': 200}), "🔑")
        try:
            for negocio in yo.get_businesses(fields=['name'], params={'limit': 100}):
                nom = (negocio.get('name') or 'BM')[:18]
                registrar(
                    negocio.get_owned_ad_accounts(fields=['name', 'account_id'], params={'limit': 200}),
                    f"🏢 {nom} ·"
                )
                registrar(
                    negocio.get_client_ad_accounts(fields=['name', 'account_id'], params={'limit': 200}),
                    f"🤝 {nom} ·"
                )
        except Exception:
            pass  # típicamente: el token no tiene 'business_management'
    except Exception:
        return {}
 
    return {etiqueta: act_id for act_id, etiqueta in
            sorted(cuentas.items(), key=lambda kv: kv[1].lower())}
 
 
cuentas_disponibles = obtener_mis_cuentas()
 
# --- 4. MOTOR DE CAPTURA ---
 
URL_ENVOLTORIO = "https://envoltorio-previews.local/render"
 
JS_HIDRATACION = """() => {
    const imgs = Array.from(document.images);
    const imgsListas = imgs.length === 0
        || imgs.every(i => i.complete && i.naturalWidth > 0);
    return imgsListas && document.fonts.status === 'loaded';
}"""
 
JS_SCROLL = """async () => {
    window.scrollTo(0, document.body.scrollHeight);
    await new Promise(r => setTimeout(r, 600));
    window.scrollTo(0, 0);
}"""
 
JS_DIMENSIONES = """() => ({
    w: Math.max(document.documentElement.scrollWidth,  document.body ? document.body.scrollWidth  : 0),
    h: Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0)
})"""
 
JS_SENAL_CONTENIDO = """() => ({
    texto: ((document.body && document.body.innerText) || '').trim().length,
    imagenes: document.images.length,
    url: location.href
})"""
 
 
def lanzar_navegador(p):
    """
    channel='chromium' = Chromium completo en modo headless NUEVO (motor
    idéntico al Chrome real). El headless por defecto de Playwright es el
    'headless shell' (modo antiguo recortado) y el bootstrap de Facebook
    puede abortar ahí, dejando el esqueleto sin hidratar.
    Requiere playwright >= 1.49; si no está disponible, cae al modo clásico.
    """
    argumentos = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
    ]
    try:
        return p.chromium.launch(channel="chromium", headless=True, args=argumentos)
    except Exception:
        return p.chromium.launch(headless=True, args=argumentos)
 
 
def extraer_src_del_iframe(cuerpo_iframe: str):
    """
    El 'body' de /previews es un <iframe> con el src HTML-escapado
    ('&' llega como '&amp;'). La autenticación viaja en el token firmado
    'd=' de esa URL: no se necesitan cookies ni cabeceras extra, y nunca
    hay que inyectar el access token al navegador.
    """
    coincidencia = re.search(r'src="([^"]+)"', cuerpo_iframe)
    if not coincidencia:
        return None
    return html_lib.unescape(coincidencia.group(1))
 
 
def capturar_embebido(pagina, cuerpo_iframe: str, ruta_archivo: str, es_instagram: bool):
    """
    Modo principal: el iframe de la API se sirve dentro de una página
    envoltorio con origen https REAL (interceptada con page.route), tal
    como Meta espera consumirlo (Sec-Fetch-Dest: iframe, sitio cruzado).
    Playwright atraviesa el frame para esperar la hidratación de verdad
    y redimensiona el iframe al alto interno antes de fotografiarlo:
    sin recortes y sin márgenes blancos.
    """
    envoltorio = f"""<!doctype html>
    <html>
    <head>
        <meta charset="utf-8" />
        <style>
            body {{ margin: 0; padding: 16px; background: #ffffff;
                    display: flex; justify-content: center; }}
            iframe {{ border: 0; display: block; }}
        </style>
    </head>
    <body>{cuerpo_iframe}</body>
    </html>"""
 
    def _responder(route):
        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=envoltorio)
 
    pagina.route(URL_ENVOLTORIO, _responder)
    try:
        pagina.set_viewport_size({"width": 620 if es_instagram else 1100, "height": 1600})
        pagina.goto(URL_ENVOLTORIO, wait_until="domcontentloaded", timeout=30000)
 
        manija = pagina.wait_for_selector("iframe", timeout=15000)
        marco = manija.content_frame()
        if marco is None:
            raise RuntimeError("El iframe no creó un frame navegable")
 
        # Esperas tolerantes: FB deja beacons abiertos, un timeout NO es error.
        for estado, t in (("domcontentloaded", 20000), ("networkidle", 12000)):
            try:
                marco.wait_for_load_state(estado, timeout=t)
            except PlaywrightTimeout:
                pass
 
        # Hidratación real dentro del frame: imágenes decodificadas + fuentes.
        try:
            marco.wait_for_function(JS_HIDRATACION, timeout=15000)
        except PlaywrightTimeout:
            pass
 
        # Empujón de lazy-load dentro del frame.
        try:
            marco.evaluate(JS_SCROLL)
        except Exception:
            pass
        pagina.wait_for_timeout(800)
 
        # Redimensionar el iframe (desde el padre) al contenido interno real.
        try:
            d = marco.evaluate(JS_DIMENSIONES)
            pagina.evaluate(
                """(d) => {
                    const f = document.querySelector('iframe');
                    if (!f) return;
                    f.setAttribute('scrolling', 'no');
                    f.style.width  = Math.min(Math.max(d.w, 320), 1280) + 'px';
                    f.style.height = Math.min(Math.max(d.h, 200), 4500) + 'px';
                }""",
                d,
            )
            pagina.wait_for_timeout(400)
        except Exception:
            pass
 
        # Foto SOLO del iframe ya ajustado: el padding del envoltorio queda fuera.
        pagina.locator("iframe").first.screenshot(path=ruta_archivo)
 
        try:
            senal = marco.evaluate(JS_SENAL_CONTENIDO)
        except Exception:
            senal = {"texto": -1, "imagenes": -1, "url": marco.url}
        senal["modo"] = "embebido"
        return senal
    finally:
        try:
            pagina.unroute(URL_ENVOLTORIO)
        except Exception:
            pass
 
 
def capturar_directo(pagina, url_preview: str, ruta_archivo: str, es_instagram: bool):
    """Plan B automático: navegar la URL firmada como documento principal."""
    pagina.set_viewport_size({"width": 540 if es_instagram else 1000, "height": 1400})
    pagina.goto(url_preview, wait_until="domcontentloaded", timeout=45000)
    try:
        pagina.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeout:
        pass
    try:
        pagina.wait_for_function(JS_HIDRATACION, timeout=15000)
    except PlaywrightTimeout:
        pass
    try:
        pagina.evaluate(JS_SCROLL)
    except Exception:
        pass
    pagina.wait_for_timeout(800)
 
    d = pagina.evaluate(JS_DIMENSIONES)
    pagina.set_viewport_size({
        "width": min(max(int(d["w"]), 320), 1280),
        "height": min(max(int(d["h"]), 400), 4000),
    })
    pagina.wait_for_timeout(400)
    pagina.screenshot(path=ruta_archivo, full_page=True)
 
    senal = pagina.evaluate(JS_SENAL_CONTENIDO)
    senal["modo"] = "directo"
    return senal
 
 
# --- 5. INTERFAZ VISUAL ---
st.header("1. Configuración de Búsqueda")
 
if cuentas_disponibles:
    cuenta_seleccionada = st.selectbox("Selecciona la Cuenta Publicitaria", options=list(cuentas_disponibles.keys()))
    cuenta_id = cuentas_disponibles[cuenta_seleccionada]
    st.caption(
        "🔑 asignada al token · 🏢 propia del Business · 🤝 compartida por socio/cliente. "
        "Si una 🏢/🤝 falla con error de permisos, asigna esa cuenta al usuario de "
        "sistema en Business Settings."
    )
else:
    st.warning(
        "No pude cargar la lista de cuentas. Verifica que el token tenga los permisos "
        "'ads_read' y 'business_management', o ingresa el ID manualmente."
    )
    cuenta_id = st.text_input("ID de la Cuenta Publicitaria (ej. act_1234567890)")
 
col1, col2 = st.columns(2)
with col1:
    palabra_campana = st.text_input("Palabra clave en la Campaña (Opcional)")
with col2:
    palabra_conjunto = st.selectbox("Filtro del Conjunto de Anuncios", ["Facebook", "Instagram"])
 
st.divider()
st.header("2. Rango de Tiempo")
col_f1, col_f2 = st.columns(2)
with col_f1:
    fecha_inicio = st.date_input("Fecha de Inicio")
with col_f2:
    fecha_fin = st.date_input("Fecha de Fin")
 
st.divider()
st.header("3. Generar Descarga")
modo_debug = st.checkbox("🔬 Modo diagnóstico (procesa solo 1 anuncio y muestra qué pasó por dentro)")
 
# --- 6. EL MOTOR ---
if st.button("🚀 Iniciar Extracción", use_container_width=True):
    if not cuenta_id:
        st.error("⚠️ Por favor, selecciona un ID de cuenta publicitaria válido.")
    elif fecha_inicio > fecha_fin:
        st.error("⚠️ La fecha de inicio no puede ser posterior a la fecha de fin.")
    else:
        with st.spinner("Ensamblando el motor avanzado y tomando fotos..."):
            try:
                cuenta = AdAccount(cuenta_id)
                parametros_busqueda = {
                    'level': 'ad',
                    'time_range': {
                        'since': fecha_inicio.strftime('%Y-%m-%d'),
                        'until': fecha_fin.strftime('%Y-%m-%d')
                    }
                }
 
                metricas = cuenta.get_insights(
                    params=parametros_busqueda,
                    fields=['ad_id', 'ad_name', 'adset_name', 'campaign_name', 'impressions']
                )
 
                anuncios_filtrados = []
                if metricas:
                    for item in metricas:
                        nombre_camp = item.get('campaign_name', '')
                        nombre_conj = item.get('adset_name', '')
                        if palabra_campana and palabra_campana.lower() not in nombre_camp.lower():
                            continue
                        if palabra_conjunto not in nombre_conj:
                            continue
                        anuncios_filtrados.append(item)
 
                if len(anuncios_filtrados) == 0:
                    st.warning("No se encontraron anuncios que cumplan con todos los filtros y >0 impresiones.")
                else:
                    if modo_debug:
                        anuncios_filtrados = anuncios_filtrados[:1]
                        st.info("🔬 Modo diagnóstico: se procesa solo el primer anuncio para inspección.")
 
                    st.info(f"✅ Se encontraron {len(anuncios_filtrados)} anuncios válidos. Capturando...")
 
                    carpeta_temp = "temp_imagenes"
                    if os.path.exists(carpeta_temp):
                        shutil.rmtree(carpeta_temp)
                    os.makedirs(carpeta_temp)
 
                    resultados = []
 
                    with sync_playwright() as p:
                        navegador = lanzar_navegador(p)
 
                        # SIN user_agent forzado: un UA inventado que no coincide con
                        # los Client Hints reales del motor (Sec-CH-UA) es una señal de
                        # automatización MÁS fuerte que el UA por defecto.
                        contexto = navegador.new_context(
                            locale="es-MX",
                            device_scale_factor=2,  # capturas al doble de resolución
                            viewport={"width": 1100, "height": 1600},
                        )
                        pagina = contexto.new_page()
 
                        # Caja negra: errores de consola y respuestas HTTP fallidas.
                        diagnostico = {"consola": [], "fallidas": []}
                        pagina.on(
                            "console",
                            lambda msg: diagnostico["consola"].append(msg.text[:180])
                            if msg.type == "error" else None
                        )
                        pagina.on(
                            "response",
                            lambda res: diagnostico["fallidas"].append(f"{res.status} {res.url[:140]}")
                            if res.status >= 400 else None
                        )
 
                        barra_progreso = st.progress(0)
 
                        for indice, anuncio in enumerate(anuncios_filtrados):
                            diagnostico["consola"].clear()
                            diagnostico["fallidas"].clear()
                            time.sleep(1)  # respiro entre llamadas a la Graph API
 
                            ad_obj = Ad(anuncio['ad_id'])
                            es_instagram = 'Instagram' in anuncio.get('adset_name', '')
                            formato = 'INSTAGRAM_STANDARD' if es_instagram else 'DESKTOP_FEED_STANDARD'
                            previews = ad_obj.get_previews(params={'ad_format': formato})
 
                            if previews and len(previews) > 0:
                                cuerpo_iframe = previews[0]['body']
 
                                nombre_limpio = "".join(
                                    [c for c in anuncio['ad_name'] if c.isalnum() or c == ' ']
                                ).rstrip() or "anuncio"
                                ruta_archivo = os.path.join(
                                    carpeta_temp, f"{nombre_limpio}_{anuncio['ad_id']}.png"
                                )
 
                                url_preview = extraer_src_del_iframe(cuerpo_iframe)
 
                                senal = {"texto": 0, "imagenes": 0, "url": "", "modo": "sin intento"}
                                try:
                                    senal = capturar_embebido(pagina, cuerpo_iframe, ruta_archivo, es_instagram)
                                except Exception as err:
                                    senal["modo"] = f"embebido falló: {type(err).__name__}: {err}"
 
                                # Si el frame quedó en esqueleto, intento directo automático.
                                parece_vacio = senal.get("texto", 0) < 30 and senal.get("imagenes", 0) == 0
                                if parece_vacio and url_preview:
                                    try:
                                        senal = capturar_directo(pagina, url_preview, ruta_archivo, es_instagram)
                                    except Exception as err:
                                        senal["modo"] = f"{senal['modo']} | directo falló: {type(err).__name__}"
 
                                resultados.append((anuncio['ad_name'], senal))
 
                                if modo_debug:
                                    with st.expander(f"🔬 Diagnóstico: {anuncio['ad_name']}", expanded=True):
                                        st.write({
                                            "modo": senal.get("modo"),
                                            "caracteres_de_texto": senal.get("texto"),
                                            "imagenes_en_dom": senal.get("imagenes"),
                                            "url_final_del_frame": senal.get("url"),
                                        })
                                        if any(s in (senal.get("url") or "") for s in ("login", "checkpoint")):
                                            st.error(
                                                "El preview redirigió a login/checkpoint: Meta no aceptó el "
                                                "token firmado desde este entorno. Sospecha principal: la IP "
                                                "del servidor donde corre la app, o un token vencido."
                                            )
                                        if diagnostico["fallidas"]:
                                            st.write("Respuestas HTTP ≥ 400 (primeras 10):")
                                            st.code("\n".join(diagnostico["fallidas"][:10]))
                                        if diagnostico["consola"]:
                                            st.write("Errores de consola del navegador (primeros 10):")
                                            st.code("\n".join(diagnostico["consola"][:10]))
                                        if os.path.exists(ruta_archivo):
                                            st.image(ruta_archivo, caption="Captura generada")
 
                            barra_progreso.progress((indice + 1) / len(anuncios_filtrados))
 
                        contexto.close()
                        navegador.close()
 
                    vacias = [n for n, s in resultados
                              if s.get("texto", 0) < 30 and s.get("imagenes", 0) == 0]
                    if vacias:
                        st.warning(
                            f"⚠️ {len(vacias)} captura(s) salieron sin contenido visible. "
                            "Activa el '🔬 Modo diagnóstico' y revisa el panel; además corre una "
                            "prueba LOCAL (streamlit run app.py en tu máquina) para descartar "
                            "que la IP del servidor sea el problema."
                        )
 
                    nombre_zip = f"Previews_{cuenta_id}_{datetime.now().strftime('%d%m%Y')}.zip"
                    with zipfile.ZipFile(nombre_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
                        for root, dirs, files in os.walk(carpeta_temp):
                            for file in files:
                                zipf.write(os.path.join(root, file), file)
 
                    shutil.rmtree(carpeta_temp)
                    st.success("🎉 ¡Proceso finalizado!")
 
                    with open(nombre_zip, "rb") as fp:
                        btn = st.download_button(
                            label="⬇️ Descargar archivo ZIP",
                            data=fp,
                            file_name=nombre_zip,
                            mime="application/zip"
                        )
 
            except Exception as e:
                st.error(f"Algo falló. Detalle técnico: {e}")
 
