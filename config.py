# config.py
import os
from dotenv import load_dotenv

# Carga automáticamente el .env si existe
load_dotenv()

# Tamaño de cada pieza del archivo en bytes.
# 256 KB es un tamaño común y equilibrado.
PIECE_SIZE = 1024 * 256  # 256 KB por pieza

# Dirección del Tracker (cargada desde .env o con valores por defecto)
TRACKER_HOST = os.getenv('TRACKER_HOST', 'localhost')
TRACKER_PORT = int(os.getenv('TRACKER_PORT', 6881))
TRACKER_ADDRESS = (TRACKER_HOST, TRACKER_PORT)

# Intervalo en segundos con el que el peer se anuncia al tracker
TRACKER_ANNOUNCE_INTERVAL = 15

# Timeout para las conexiones de socket en segundos
SOCKET_TIMEOUT = 5

# Nombre para el archivo de estado de la descarga
DOWNLOAD_STATE_EXTENSION = ".state"