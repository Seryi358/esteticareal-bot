#!/bin/bash

echo "==================================="
echo "   Estetica Real WhatsApp Bot"
echo "==================================="

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "Creando entorno virtual..."
    python3 -m venv venv
fi

# Activate venv
source venv/bin/activate

# Install dependencies
echo "Instalando dependencias..."
pip install -r requirements.txt -q

# Check .env
if [ ! -f ".env" ]; then
    echo "ERROR: No se encontro el archivo .env"
    echo "Copia .env.example a .env y completa los datos."
    exit 1
fi

# Check Evolution API config
if grep -q "TU-SERVIDOR" .env; then
    echo ""
    echo "ADVERTENCIA: El archivo .env tiene valores de ejemplo."
    echo "Edita .env con los datos reales de tu instancia de Evolution API."
    echo ""
fi

echo ""
echo "Iniciando bot en http://0.0.0.0:8000"
echo "Webhook URL: http://TU-SERVIDOR:8000/webhook"
echo ""
echo "Presiona Ctrl+C para detener."
echo ""

uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1
