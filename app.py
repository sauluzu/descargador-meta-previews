import streamlit as st
import os

# Chromium se instala UNA sola vez por contenedor. Streamlit re-ejecuta este
# script completo en CADA interacción; sin este guard, 'playwright install'
# corría en cada clic del usuario.
_MARCADOR_CHROMIUM = "/tmp/.chromium_instalado"
if not os.path.exists(_MARCADOR_CHROMIUM):
    if os.system("playwright install chromium") == 0:
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
    try:
        yo = User(fbid='me')
        lista_cuentas = yo.get_ad_accounts(fields=['name', 'account_id'])
        diccionario = {}
        for c in lista_cuentas:
            nombre = c.get('name', 'Cuenta sin nombre')
            id_cuenta = c.get('account_id')
            diccionario[f"{nombre} (act_{id_cuenta})"] = f"act_{id_cuenta}"
        return diccionario
    except Exception:
        return {}

cuentas_disponibles = obtener_mis_cuentas()

# --- 4. MOTOR DE CAPTURA (funciones) ---

def extraer_src_del_iframe(cuerpo_iframe: str):
    """
    El 'body' que regresa /previews es un tag <iframe> cuyo src viene
    HTML-escapado (los '&' llegan como '&amp;'). Extraemos la URL firmada
    real para navegarla como documento principal.

    La autenticación viaja en el parámetro 'd=' (token firmado de ~24 h):
    NO se necesitan cookies, ni cabeceras extra, ni mucho menos inyectar
    el access token al navegador.
    """
    coincidencia = re.search(r'src="([^"]+)"', cuerpo_iframe)
    if not coincidencia:
        return None
    return html_lib.unescape(coincidencia.group(1))


def capturar_preview(pagina, url_preview: str, ruta_archivo: str, es_instagram: bool):
    """
    Navega la URL firmada del preview como documento PRINCIPAL.

    Con facebook.com en primera parte (en vez de incrustado bajo un padre
    about:blank de origen nulo), el storage, el referrer y las llamadas
    asíncronas que pintan el caption y el username funcionan con normalidad.
    Además desaparece la caja fija del <iframe> que recortaba el contenido.
    """
    # Ancho acorde al formato; el alto se recalcula al contenido real abajo.
    pagina.set_viewport_size({"width": 540 if es_instagram else 1000, "height": 1400})

    pagina.goto(url_preview, wait_until="domcontentloaded", timeout=45000)

    # Paso 1: dejar que la red se calme. Facebook mantiene beacons y
    # long-polling abiertos, así que un timeout aquí NO es un error.
    try:
        pagina.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeout:
        pass

    # Paso 2: esperar la hidratación REAL en lugar de un sleep ciego:
    # todas las imágenes decodificadas y las fuentes web cargadas.
    try:
        pagina.wait_for_function(
            """() => {
                const imgs = Array.from(document.images);
                const imgsListas = imgs.length === 0
                    || imgs.every(i => i.complete && i.naturalWidth > 0);
                return imgsListas && document.fonts.status === 'loaded';
            }""",
            timeout=15000,
        )
    except PlaywrightTimeout:
        pass  # preferimos una captura parcial a abortar el lote completo

    # Paso 3: pase de scroll para disparar cualquier lazy-load pendiente
    # y regresar al tope antes de la foto.
    pagina.evaluate(
        """async () => {
            window.scrollTo(0, document.body.scrollHeight);
            await new Promise(r => setTimeout(r, 600));
            window.scrollTo(0, 0);
        }"""
    )
    pagina.wait_for_timeout(800)

    # Paso 4: ajustar el viewport al tamaño real del contenido.
    # Esto elimina los espacios en blanco y los recortes del método anterior.
    dimensiones = pagina.evaluate(
        """() => ({
            w: Math.min(Math.max(document.documentElement.scrollWidth, 320), 1280),
            h: Math.min(Math.max(document.documentElement.scrollHeight, 400), 4000)
        })"""
    )
    pagina.set_viewport_size({"width": dimensiones["w"], "height": dimensiones["h"]})
    pagina.wait_for_timeout(400)

    pagina.screenshot(path=ruta_archivo, full_page=True)


def capturar_por_inyeccion(pagina, cuerpo_iframe: str, ruta_archivo: str):
    """
    Fallback (el método anterior de inyección local). Solo se usa si el
    body de la API no trajera un src extraíble, cosa que en la práctica
    no debería ocurrir.
    """
    html_to_render = f"""
    <!doctype html>
    <html>
    <head>
        <meta charset="utf-8" />
        <style>
            body {{ margin: 0; padding: 24px; background: #f0f2f5; font-family: Arial, sans-serif; }}
            .preview-root {{ display: flex; justify-content: center; align-items: flex-start; min-height: 100vh; }}
        </style>
    </head>
    <body>
        <div class="preview-root">{cuerpo_iframe}</div>
    </body>
    </html>
    """
    pagina.set_content(html_to_render, wait_until="domcontentloaded")
    pagina.wait_for_timeout(8000)
    try:
        pagina.locator(".preview-root > *").first.screenshot(path=ruta_archivo)
    except Exception:
        pagina.screenshot(path=ruta_archivo)

# --- 5. INTERFAZ VISUAL ---
st.header("1. Configuración de Búsqueda")

if cuentas_disponibles:
    cuenta_seleccionada = st.selectbox("Selecciona la Cuenta Publicitaria", options=list(cuentas_disponibles.keys()))
    cuenta_id = cuentas_disponibles[cuenta_seleccionada]
else:
    st.warning("No pude cargar la lista de cuentas. Puedes ingresar el ID manualmente.")
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
                    st.info(f"✅ Se encontraron {len(anuncios_filtrados)} anuncios válidos. Capturando...")

                    carpeta_temp = "temp_imagenes"
                    if os.path.exists(carpeta_temp):
                        shutil.rmtree(carpeta_temp)
                    os.makedirs(carpeta_temp)

                    with sync_playwright() as p:
                        navegador = p.chromium.launch(
                            headless=True,
                            args=[
                                "--no-sandbox",
                                "--disable-setuid-sandbox",
                                "--disable-dev-shm-usage",
                                "--disable-gpu",
                                "--disable-blink-features=AutomationControlled"
                            ]
                        )

                        # SIN user_agent forzado: un UA inventado (Chrome/122) que no
                        # coincide con los Client Hints reales del motor (Sec-CH-UA) es
                        # una señal de automatización MÁS fuerte que el UA por defecto.
                        # El endpoint firmado del preview no exige un UA específico.
                        contexto = navegador.new_context(
                            locale="es-MX",
                            device_scale_factor=2,  # capturas al doble de resolución (nítidas)
                            viewport={"width": 1000, "height": 1400},
                        )
                        pagina = contexto.new_page()

                        barra_progreso = st.progress(0)

                        for indice, anuncio in enumerate(anuncios_filtrados):
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
                                # Sufijo con ad_id: dos anuncios con el mismo nombre
                                # ya no se sobreescriben entre sí.
                                ruta_archivo = os.path.join(
                                    carpeta_temp, f"{nombre_limpio}_{anuncio['ad_id']}.png"
                                )

                                url_preview = extraer_src_del_iframe(cuerpo_iframe)
                                try:
                                    if url_preview:
                                        capturar_preview(pagina, url_preview, ruta_archivo, es_instagram)
                                    else:
                                        capturar_por_inyeccion(pagina, cuerpo_iframe, ruta_archivo)
                                except Exception:
                                    # Último recurso: capturar lo que haya en pantalla
                                    # para no tirar el lote completo por un solo anuncio.
                                    try:
                                        pagina.screenshot(path=ruta_archivo, full_page=True)
                                    except Exception:
                                        pass

                            barra_progreso.progress((indice + 1) / len(anuncios_filtrados))

                        contexto.close()
                        navegador.close()

                    nombre_zip = f"Previews_{cuenta_id}_{datetime.now().strftime('%d%m%Y')}.zip"
                    with zipfile.ZipFile(nombre_zip, 'w', zipfile.ZIP_DEFLATED) as zipf:
                        for root, dirs, files in os.walk(carpeta_temp):
                            for file in files:
                                zipf.write(os.path.join(root, file), file)

                    shutil.rmtree(carpeta_temp)
                    st.success("🎉 ¡Proceso finalizado con éxito!")

                    with open(nombre_zip, "rb") as fp:
                        btn = st.download_button(
                            label="⬇️ Descargar archivo ZIP",
                            data=fp,
                            file_name=nombre_zip,
                            mime="application/zip"
                        )

            except Exception as e:
                st.error(f"Algo falló. Detalle técnico: {e}")
