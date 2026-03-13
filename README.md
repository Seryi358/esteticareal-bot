# Estetica Real — WhatsApp Bot

Bot de WhatsApp con IA para Estetica Real. Usa GPT-4o para conversacion humana, verifica comprobantes de pago con vision computacional, y agenda citas via Google Calendar.

---

## Arquitectura

```
WhatsApp usuario
      |
Evolution API (tu instancia)
      | POST /webhook
      v
Este servidor (FastAPI)
      |
      +---> OpenAI GPT-4o (conversacion)
      +---> OpenAI GPT-4o Vision (verificar pago)
      +---> Google Calendar API (disponibilidad + citas)
      +---> Evolution API (enviar respuestas)
```

---

## Setup en 5 pasos

### 1. Instalar dependencias

```bash
python3 -m venv venv
source venv/bin/activate   # Mac/Linux
# venv\Scripts\activate    # Windows

pip install -r requirements.txt
```

### 2. Configurar el archivo .env

Edita el archivo `.env` con los datos reales:

```bash
# Ya viene con tu API key de OpenAI

# Datos de tu Evolution API:
EVOLUTION_API_URL=https://TU-SERVIDOR-EVOLUTION.com
EVOLUTION_API_KEY=TU-API-KEY
EVOLUTION_INSTANCE=NOMBRE-INSTANCIA
```

**Donde encontrar los datos de Evolution API:**
- `EVOLUTION_API_URL`: La URL de tu servidor donde esta instalado Evolution API
- `EVOLUTION_API_KEY`: La `apikey` global de tu Evolution API (en el panel de administracion o en el `.env` de Evolution API)
- `EVOLUTION_INSTANCE`: El nombre que le pusiste a la instancia conectada al WhatsApp del bot

### 3. Configurar Google Calendar

```bash
python setup_calendar.py
```

Sigue los pasos que aparecen en pantalla. Necesitas:
1. Ir a [Google Cloud Console](https://console.cloud.google.com)
2. Crear un proyecto → Habilitar "Google Calendar API"
3. Crear credenciales OAuth 2.0 (tipo: Aplicacion de escritorio)
4. Descargar el JSON → guardarlo como `credentials/google_credentials.json`
5. Ejecutar `python setup_calendar.py` → autorizar con la cuenta de Yesica

### 4. Exponer el servidor a internet

El servidor necesita ser accesible desde internet para que Evolution API pueda enviar los webhooks.

**Opcion A — ngrok (pruebas rapidas):**
```bash
ngrok http 8000
# Copia la URL https://xxxx.ngrok.io
# Tu webhook sera: https://xxxx.ngrok.io/webhook
```

**Opcion B — VPS/Servidor (produccion):**
- Despliega en cualquier VPS (DigitalOcean, AWS, Google Cloud, etc.)
- URL del webhook: `https://tudominio.com/webhook`

### 5. Configurar el webhook en Evolution API

En el panel de administracion de tu instancia de Evolution API:
- **Webhook URL**: `https://TU-SERVIDOR/webhook`
- **Eventos habilitados**: `MESSAGES_UPSERT` (ya lo tienes activado)
- Guarda los cambios

### 6. Iniciar el bot

```bash
./start.sh
# o directamente:
uvicorn main:app --host 0.0.0.0 --port 8000
```

---

## Funcionalidades

- **Conversacion natural**: GPT-4o con prompt especializado en neuromarketing y cierre de ventas
- **Flujo automatico**: Detecta interes del servicio → ciudad → info → valoracion → pago → calendario → cita
- **Verificacion de pago**: GPT-4o Vision analiza el pantallazo de Nequi y verifica autenticidad
- **Notificacion a Yesica**: Cuando llega un comprobante, el bot le envia un WhatsApp a Yesica con el numero del cliente
- **Google Calendar**: Consulta disponibilidad real y crea la cita directamente en el calendario
- **Manejo de objeciones**: Respuestas entrenadas para los argumentos mas comunes
- **Anti-deteccion**: Comportamiento humano (typing indicator, delays naturales, lenguaje colombiano)
- **Persistencia**: El historial de cada conversacion se guarda en `data/conversations/`

---

## Precios configurados

| Servicio | Precio visible |
|----------|---------------|
| Hidrofacial | $195.000 COP |
| Todos los demas | Solo en valoracion |
| **Valoracion profesional** | **$25.000 COP** |

---

## Reglas de negocio criticas

1. El bot SOLO muestra el precio del Hidrofacial ($195.000)
2. La valoracion siempre cuesta $25.000 sin excepcion
3. El pago se acepta UNICAMENTE por Nequi 3006278237 a nombre de Yesica Restrepo
4. El bot verifica el comprobante antes de proceder
5. No agenda sin pago verificado

---

## Estructura del proyecto

```
esteticareal_bot/
├── main.py              # FastAPI app y webhook endpoint
├── config.py            # Configuracion desde .env
├── bot/
│   ├── flow.py          # Logica principal del bot
│   ├── conversation.py  # Estado y memoria por usuario
│   └── prompts.py       # System prompt + prompts de verificacion
├── services/
│   ├── evolution.py     # Cliente HTTP de Evolution API
│   ├── ai.py            # OpenAI GPT-4o y Vision
│   └── calendar.py      # Google Calendar
├── credentials/         # token.json de Google (no se sube a git)
├── data/conversations/  # Historial por usuario (no se sube a git)
├── setup_calendar.py    # Setup inicial de Google Calendar
└── start.sh             # Script de inicio
```

---

## Soporte

Desarrollado por PhinodIA para Estetica Real.
