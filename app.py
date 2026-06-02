import streamlit as st
import os
os.system("playwright install chromium")
import re
import time
import zipfile
import shutil
import html
from datetime import datetime
from playwright.sync_api import sync_playwright
from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount
from facebook_business.adobjects.ad import Ad
from facebook_business.adobjects.user import User

# --- 1. TUS LLAVES SECRETIISIMAS DE META ---
MI_APP_ID = st.secrets["META_APP_ID"]
MI_APP_SECRET = st.secrets["META_APP_SECRET"]
MI_ACCESS_TOKEN = st.secrets["META_ACCESS_TOKEN"]

# --- 2. CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(page_title="Meta Previews Automático", page_icon="📸", layout="centered")
st.title("📸 Descargador de Previews - Meta Ads")
st.markdown("Genera capturas de pantalla automáticas de tus anuncios con rendimiento activo.")
st.divider()

# --- 3. CONEXIÓN TEMPRANA PARA EXTRAER CUENTAS ---
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

# --- 4. INTERFAZ VISUAL ---
st.header("1. Configuración de Búsqueda")

if cuentas_disponibles:
    cuenta_seleccionada = st.selectbox(
        "Selecciona la Cuenta Publicitaria", 
        options=list(cuentas_disponibles.keys())
    )
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
st.markdown("Selecciona el periodo exacto que deseas evaluar:")

col_f1, col_f2 = st.columns(2)
with col_f1:
    fecha_inicio = st.date_input("Fecha de Inicio")
with col_f2:
    fecha_fin = st.date_input("Fecha de Fin")

st.divider()
st.header("3. Generar Descarga")

# --- 5. EL MOTOR PRINCIPAL ---
if st.button("🚀 Iniciar Extracción", use_container_width=True):
    if not cuenta_id:
        st.error("⚠️ Por favor, selecciona un ID de cuenta publicitaria válido.")
    elif fecha_inicio > fecha_fin:
        st.error("⚠️ La fecha de inicio no puede ser posterior a la fecha de fin.")
    else:
        with st.spinner("Analizando rendimiento y tomando fotos... Esto puede tomar varios minutos."):
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
                    st.info(f"✅ Se encontraron {len(anuncios_filtrados)} anuncios válidos. Tomando fotografías...")
                    
                    carpeta_temp = "temp_imagenes"
                    if os.path.exists(carpeta_temp):
                        shutil.rmtree(carpeta_temp)
                    os.makedirs(carpeta_temp)
                    
                    with sync_playwright() as p:
                        navegador = p.chromium.launch(headless=True)
                        
                        # MEJORA: Monitor gigante de escritorio (evita la censura móvil de Meta)
                        contexto = navegador.new_context(
                            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                            viewport={'width': 1000, 'height': 1200},
                            locale='es-ES'
                        )
                        pagina = contexto.new_page()
                        
                        barra_progreso = st.progress(0)
                        
                        for indice, anuncio in enumerate(anuncios_filtrados):
                            time.sleep(1) 
                            ad_obj = Ad(anuncio['ad_id'])
                            
                            # Regresamos a la vista de escritorio para Facebook (donde siempre sale el texto)
                            formato = 'INSTAGRAM_STANDARD' if 'Instagram' in anuncio.get('adset_name', '') else 'DESKTOP_FEED_STANDARD'
                            previews = ad_obj.get_previews(params={'ad_format': formato})
                            
                            if previews and len(previews) > 0:
                                link_extraido = re.search(r'src="([^"]+)"', previews[0]['body'])
                                if link_extraido:
                                    url_preview = html.unescape(link_extraido.group(1))
                                    nombre_limpio = "".join([c for c in anuncio['ad_name'] if c.isalnum() or c==' ']).rstrip()
                                    ruta_archivo = os.path.join(carpeta_temp, f"{nombre_limpio}.png")
                                    
                                    pagina.goto(url_preview)
                                    pagina.wait_for_timeout(4000)
                                    
                                    # Destruir banners de cookies o modales invisibles
                                    pagina.keyboard.press("Escape")
                                    pagina.wait_for_timeout(1000)
                                    
                                    # Scroll de escritorio para forzar carga de imágenes
                                    pagina.mouse.wheel(0, 600)
                                    pagina.wait_for_timeout(2000)
                                    pagina.mouse.wheel(0, -600)
                                    pagina.wait_for_timeout(3000)
                                    
                                    # Tomamos la foto directo a la caja iframe centrada
                                    try:
                                        iframe_elem = pagina.locator('iframe').first
                                        if iframe_elem.is_visible():
                                            iframe_elem.screenshot(path=ruta_archivo)
                                        else:
                                            pagina.screenshot(path=ruta_archivo)
                                    except Exception:
                                        pagina.screenshot(path=ruta_archivo)
                            
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
