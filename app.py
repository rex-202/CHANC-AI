import os
import requests
from flask import Flask, jsonify, render_template, request
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, logout_user, current_user
from flask_bcrypt import Bcrypt
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime, timedelta

# --- CONFIGURACIÓN INICIAL ---
load_dotenv()
app = Flask(__name__)
app.config['SECRET_KEY'] = os.urandom(24)

# Configuración de la base de datos
db_url = os.getenv('DATABASE_URL')
if not db_url:
    raise ValueError("DATABASE_URL no está configurada en el archivo .env o variables de entorno")

# SQLAlchemy 2.0 prefiere 'postgresql://' en lugar de 'postgres://'
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)

# APIs
openai_api_key = os.getenv("OPENAI_API_KEY")
weather_api_key = os.getenv("WEATHER_API_KEY")
myshiptracking_api_key = os.getenv("MYSHIPTRACKING_API_KEY")
gfw_api_key = os.getenv("GFW_API_KEY")
client = OpenAI(api_key=openai_api_key)

# --- MODELO DE BASE DE DATOS ---
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    nombres = db.Column(db.String(150), nullable=False)
    apellidos = db.Column(db.String(150), nullable=False)
    email = db.Column(db.String(150), unique=True, nullable=False)
    pais = db.Column(db.String(50), nullable=False)
    password = db.Column(db.String(150), nullable=False)

@login_manager.user_loader
def load_user(user_id):
    # Asegúrate de que la sesión de la base de datos se maneje correctamente
    with app.app_context():
        return User.query.get(int(user_id))

PORTS_DATABASE = {
    "peru": ["Callao", "Paita", "Matarani"], "chile": ["Valparaiso", "San Antonio"],
    "ecuador": ["Guayaquil", "Manta"], "colombia": ["Buenaventura", "Cartagena"],
    "argentina": ["Buenos Aires", "Bahia Blanca"], "brasil": ["Santos", "Rio de Janeiro"]
}

# --- FUNCIONES DE LÓGICA ---
def obtener_datos_myshiptracking(api_key, imo):
    if not api_key: return {"error": "API Key de MyShipTracking no configurada."}
    print(f"\n[INFO] Buscando en MyShipTracking para IMO: {imo}...")
    url = f"https://api.myshiptracking.com/api/v2/vessel?imo={imo}"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        api_response = response.json()
        if api_response.get("status") == "success":
            data = api_response.get("data", {})
            return {
                "nombre_barco": data.get('vessel_name'), "latitud": data.get('lat'), "longitud": data.get('lng'),
                "velocidad_nudos": data.get('speed'), "rumbo": data.get('course'), "destino": data.get('destination'),
                "eta": data.get('eta'), "ultimo_reporte": data.get('received')
            }
        else:
            if "ERR_VESSEL_NOT_FOUND" in api_response.get("code", ""):
                 return {"error": f"No se encontró ningún barco con el número IMO: {imo}."}
            return {"error": api_response.get("message", "Error desconocido de la API.")}
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Conexión con MyShipTracking falló: {e}")
        return {"error": "No se pudo conectar con la API de MyShipTracking."}

def obtener_datos_gfw(api_key, imo):
    if not api_key:
        return {"error": "API Key de Global Fishing Watch no configurada."}

    print(f"\n[INFO] Buscando en Global Fishing Watch para IMO: {imo}...")
    headers = {"Authorization": f"Bearer {api_key}"}
    
    search_url = f"https://gateway.api.globalfishingwatch.org/v3/vessels/search?query={imo}&datasets[0]=public-global-vessel-identity:latest"
    
    try:
        response = requests.get(search_url, headers=headers)
        response.raise_for_status()
        vessel_data = response.json()

        if not vessel_data.get("entries"):
            return {"info": "No se encontraron registros públicos para este buque."}

        self_reported_info = vessel_data["entries"][0].get("selfReportedInfo")
        if not self_reported_info:
            return {"info": "El buque existe en GFW pero no tiene información de AIS reportada."}
        
        vessel_id = self_reported_info[0].get("id")
        
        # --- CORRECCIÓN PARA EVITAR IndexError ---
        registry_info_list = vessel_data["entries"][0].get("registryInfo")
        # Si la lista existe y tiene elementos, tomamos el primero; si no, usamos un dict vacío.
        registry_info = registry_info_list[0] if registry_info_list and len(registry_info_list) > 0 else {}
        # ------------------------------------------

        gfw_summary = {
            "nombre_registrado": registry_info.get("shipname", "No disponible"),
            "bandera": registry_info.get("flag", "No disponible"),
            "tipo_de_equipo": ", ".join(g.get("name", "") for g in registry_info.get("geartype", [])) or "No especificado",
            "fuentes_de_registro": ", ".join(registry_info.get("sourceCode", [])) or "No disponible"
        }

        today = datetime.utcnow()
        ninety_days_ago = today - timedelta(days=90)
        start_date = ninety_days_ago.strftime('%Y-%m-%d')
        end_date = today.strftime('%Y-%m-%d')
        
        datasets_to_query = {
            "fishing": "public-global-fishing-events:latest",
            "port": "public-global-port-visits-events:latest"
        }
        all_events = []

        for key, dataset_name in datasets_to_query.items():
            events_url = (f"https://gateway.api.globalfishingwatch.org/v3/events?vessels[0]={vessel_id}"
                          f"&datasets[0]={dataset_name}&start-date={start_date}&end-date={end_date}&limit=5")
            
            events_response = requests.get(events_url, headers=headers)
            if events_response.status_code == 200:
                events_data = events_response.json()
                if events_data.get("entries"):
                    all_events.extend(events_data["entries"])

        all_events.sort(key=lambda x: x.get('start', ''), reverse=True)
        
        event_summary = []
        for event in all_events[:5]:
            event_type = event.get("type", "desconocido").replace('_', ' ').title()
            event_start = datetime.fromisoformat(event.get("start").replace("Z", "+00:00")).strftime('%Y-%m-%d')
            event_summary.append(f"- Evento de '{event_type}' iniciado el {event_start}")
        
        gfw_summary["eventos_recientes"] = event_summary if event_summary else ["No se han registrado eventos notables en los últimos 90 días."]
        
        return gfw_summary

    except requests.exceptions.HTTPError as http_err:
        print(f"[ERROR] HTTP Error con GFW: {http_err}")
        return {"error": "No fue posible consultar las bases de datos de actividad marítima en este momento."}
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] Conexión con GFW falló: {e}")
        return {"error": "No se pudo conectar con la API de Global Fishing Watch."}


def analizar_con_ia(prompt, reporte):
    try:
        completion = client.chat.completions.create(model="gpt-4o", temperature=0.2, max_tokens=1000,
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": reporte}])
        return completion.choices[0].message.content
    except Exception as e: return f"Error al generar el análisis de la IA: {e}"

def obtener_clima(api_key, query_location):
    url = f"http://api.weatherapi.com/v1/current.json?key={api_key}&q={query_location}&aqi=no"
    try:
        response = requests.get(url); response.raise_for_status(); data = response.json()
        return {"condicion": data['current']['condition']['text'], "viento_kph": data['current']['wind_kph']}
    except requests.exceptions.RequestException: return None

def accion_principal(imo_barco, user_nombre):
    print(f"\n--- Ejecutando lógica de seguimiento para IMO: {imo_barco} ---")
    
    datos_posicion = obtener_datos_myshiptracking(myshiptracking_api_key, imo_barco)
    
    if "error" in datos_posicion:
        return {"reporte": datos_posicion["error"], "coordenadas": None}

    clima_actual = "No disponible"
    coordenadas = None
    if isinstance(datos_posicion, dict) and datos_posicion.get('latitud') is not None:
        coordenadas = [datos_posicion['latitud'], datos_posicion['longitud']]
        clima_data = obtener_clima(weather_api_key, f"{coordenadas[0]},{coordenadas[1]}")
        if clima_data:
            clima_actual = f"Condición: {clima_data['condicion']}, Viento: {clima_data['viento_kph']} kph."
            
    datos_gfw = obtener_datos_gfw(gfw_api_key, imo_barco)

    reporte_completo = (
        f"**DATOS DE POSICIÓN (MyShipTracking):**\n{datos_posicion}\n\n"
        f"**CLIMA EN LA UBICACIÓN ACTUAL:**\n{clima_actual}\n\n"
        f"**DATOS DE IDENTIDAD Y ACTIVIDAD (Global Fishing Watch):**\n{datos_gfw}"
    )
    
    # --- (PROMPT FINAL CORREGIDO PARA PERSONALIZACIÓN) ---
    prompt = (
        f"Eres Chanc-ai, un analista experto en logística marítima. Tu tarea es redactar un informe ejecutivo personalizado para el usuario '{user_nombre}'. "
        "El informe debe ser fluido, integrado y en un tono narrativo. No enumeres los datos; en su lugar, úsalos para construir un análisis coherente.\n\n"
        "**Estructura del Informe:**\n"
        "1. **Saludo y Resumen:** Comienza el informe con un saludo personalizado (ej. 'Hola, Mateo.') y presenta el estado general del buque en una o dos frases.\n"
        "2. **Análisis de la Situación:** Desarrolla el párrafo principal que integra todos los datos disponibles (posición, clima, identidad, actividad).\n"
        "3. **Evaluación y Recomendaciones:** Concluye con tu evaluación profesional. Si falta información (como el ETA o datos de GFW), conviértelo en un punto de análisis de riesgo y basa tus recomendaciones en ello.\n\n"
        "**Instrucciones Especiales:**\n"
        "- **Manejo de Errores:** Si los datos de Global Fishing Watch no están disponibles, indícalo de forma sutil, como: 'No fue posible verificar los registros públicos de actividad del buque en este momento'. No menciones APIs ni errores de conexión.\n"
        "- **Tono:** Profesional, directo y personalizado. Evita la redundancia."
    )
    
    return {"reporte": analizar_con_ia(prompt, reporte_completo), "coordenadas": coordenadas}

# --- RUTAS DE LA APLICACIÓN WEB ---
@app.route('/')
def home():
    return render_template('index.html')

# !!! ================================================================= !!!
# !!! RUTA TEMPORAL PARA CREAR TABLAS EN RENDER                         !!!
# !!! Visita https://chanc-ai.onrender.com/create-db-tables-once        !!!
# !!! UNA VEZ después de desplegar, y LUEGO BORRA ESTE CÓDIGO.         !!!
# !!! ================================================================= !!!
@app.route('/create-db-tables-once')
def create_db_tables_once():
    try:
        with app.app_context():
            db.create_all()
        return "Tablas creadas exitosamente!", 200
    except Exception as e:
        return f"Error al crear tablas: {str(e)}", 500
# !!! ================================================================= !!!
# !!! FIN DEL BLOQUE TEMPORAL                                           !!!
# !!! ================================================================= !!!

@app.route('/api/generar-informe', methods=['POST'])
def generar_informe_api():
    imo = request.json.get('imo')
    if not imo: return jsonify({"error": "Falta el número IMO."}), 400
    
    user_nombre = "Estimado usuario"
    if current_user.is_authenticated:
        user_nombre = current_user.nombres

    return jsonify(accion_principal(imo, user_nombre))

@app.route('/api/clima/<pais>')
def clima_por_pais_api(pais):
    puertos = PORTS_DATABASE.get(pais.lower())
    if not puertos: return jsonify({"error": "País no encontrado."}), 404
    clima_puertos = [d for d in [dict(puerto=p, **(obtener_clima(weather_api_key, f"{p},{pais}") or {})) for p in puertos] if 'condicion' in d]
    return jsonify(clima_puertos)

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    try:
        if User.query.filter_by(email=data['email']).first(): return jsonify({"message": "El correo ya está registrado."}), 409
        hashed_password = bcrypt.generate_password_hash(data['password']).decode('utf-8')
        new_user = User(nombres=data['nombres'], apellidos=data['apellidos'], email=data['email'], pais=data['pais'], password=hashed_password)
        db.session.add(new_user)
        db.session.commit()
        login_user(new_user)
        return jsonify({"message": "¡Registro exitoso!", "user": {"pais": new_user.pais, "nombres": new_user.nombres}}), 201
    except Exception as e:
        db.session.rollback(); print(f"ERROR DE REGISTRO: {e}")
        return jsonify({"message": "Error interno del servidor."}), 500
        
@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    user = User.query.filter_by(email=data.get('email')).first()
    if user and bcrypt.check_password_hash(user.password, data.get('password', '')):
        login_user(user)
        return jsonify({"message": "Inicio de sesión exitoso.", "user": {"pais": user.pais, "nombres": user.nombres}}), 200
    return jsonify({"message": "Credenciales inválidas."}), 401

@app.route('/api/logout', methods=['POST'])
def logout():
    logout_user()
    return jsonify({"message": "Sesión cerrada."})

@app.route('/api/session')
def session_status():
    if current_user.is_authenticated:
        return jsonify({"logged_in": True, "user": {"pais": current_user.pais, "nombres": current_user.nombres}})
    return jsonify({"logged_in": False})

# --- PUNTO DE ENTRADA DEL PROGRAMA ---
if __name__ == "__main__":
    with app.app_context():
        # Esta línea crea las tablas para tu desarrollo LOCAL
        db.create_all() 
    app.run(debug=True, port=5000)