"""
Setup de Google Calendar para Estetica Real Bot.

Ejecuta este script UNA SOLA VEZ para autorizar al bot a acceder
al calendario de Google de Yesica.

Requisitos previos:
1. Ve a https://console.cloud.google.com
2. Crea un proyecto nuevo (ej: "esteticareal-bot")
3. Busca "Google Calendar API" y habilitala
4. Ve a "Credenciales" > "Crear credenciales" > "ID de cliente OAuth"
5. Tipo: "Aplicacion de escritorio"
6. Descarga el JSON y guardalo como: credentials/google_credentials.json
7. Ejecuta este script: python setup_calendar.py
8. Autoriza en el navegador con la cuenta de Google de Yesica

Resultado: Se crea credentials/token.json que el bot usa automaticamente.
"""

import os
import sys

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets",
]
CREDENTIALS_FILE = "credentials/google_credentials.json"
TOKEN_FILE = "credentials/token.json"


def main():
    if not os.path.exists(CREDENTIALS_FILE):
        print(f"\n ERROR: No se encontro el archivo {CREDENTIALS_FILE}")
        print("\nPasos para obtenerlo:")
        print("  1. Ve a https://console.cloud.google.com")
        print("  2. Crea un proyecto y habilita 'Google Calendar API'")
        print("  3. Crea credenciales OAuth 2.0 (tipo: Aplicacion de escritorio)")
        print("  4. Descarga el JSON y guardalo en: credentials/google_credentials.json")
        print("  5. Vuelve a ejecutar: python setup_calendar.py\n")
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError:
        print("\n ERROR: Instala primero las dependencias:")
        print("  pip install -r requirements.txt\n")
        sys.exit(1)

    creds = None

    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            print("Token renovado exitosamente!")
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            print("\nAbriendo navegador para autorizar acceso al calendario...")
            print("Inicia sesion con la cuenta de Google de Yesica.\n")
            creds = flow.run_local_server(port=0)
            print("\nAutorizacion exitosa!")

        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

    # Test connection
    try:
        from googleapiclient.discovery import build
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        calendar_list = service.calendarList().list().execute()
        calendars = calendar_list.get("items", [])
        print("\n Calendarios encontrados en la cuenta:")
        for cal in calendars:
            marker = " <-- usa este ID" if cal.get("primary") else ""
            print(f"  - {cal['summary']}: {cal['id']}{marker}")
        print(f"\n Configuracion completada!")
        print(f"  Token guardado en: {TOKEN_FILE}")
        print(f"  El bot usara el calendario: primary (por defecto)")
        print(f"  Para usar otro calendario, actualiza GOOGLE_CALENDAR_ID en .env\n")
    except Exception as e:
        print(f"\n Advertencia al conectar con Calendar: {e}")
        print("  Pero el token fue guardado. Verifica tu conexion a internet.\n")


if __name__ == "__main__":
    main()
