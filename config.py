# config.py

# Tamaño de cada pieza del archivo en bytes.
# Un tamaño más pequeño es bueno para la granularidad, pero aumenta el overhead del metadata.
# 1MB = 1024 * 1024 bytes
PIECE_SIZE = 1024 * 256  # 256 KB por pieza

# Dirección del Tracker
TRACKER_HOST = 'localhost'
TRACKER_PORT = 6881
TRACKER_ADDRESS = (TRACKER_HOST, TRACKER_PORT)

# Intervalo en segundos con el que el peer se anuncia al tracker
TRACKER_ANNOUNCE_INTERVAL = 15