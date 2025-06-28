# create_torrent.py
import hashlib
import json
import os
import sys
from config import PIECE_SIZE, TRACKER_ADDRESS

def create_torrent_file(file_path, output_torrent_path):
    """
    Crea un archivo de metadatos (.json) para un archivo dado.
    Corresponde a la funcionalidad de Torrent.java.
    """
    print(f"Creando archivo torrent para: {file_path}")
    
    if not os.path.exists(file_path):
        print(f"Error: El archivo '{file_path}' no existe.")
        return

    # 1. Leer el archivo y dividirlo en piezas, calculando el hash de cada una
    piece_hashes = []
    file_size = os.path.getsize(file_path)
    
    with open(file_path, 'rb') as f:
        while True:
            piece = f.read(PIECE_SIZE)
            if not piece:
                break
            # Usamos SHA-1, es el estándar en BitTorrent
            piece_hash = hashlib.sha1(piece).hexdigest()
            piece_hashes.append(piece_hash)

    # 2. Construir el diccionario de metadatos
    torrent_data = {
        'tracker_url': f"{TRACKER_ADDRESS[0]}:{TRACKER_ADDRESS[1]}",
        'file_info': {
            'name': os.path.basename(file_path),
            'size': file_size,
            'piece_size': PIECE_SIZE,
            'pieces': piece_hashes
        }
    }
    
    # 3. Guardar el diccionario como un archivo JSON
    with open(output_torrent_path, 'w') as f:
        json.dump(torrent_data, f, indent=4)
        
    print(f"Archivo torrent creado exitosamente en: '{output_torrent_path}'")
    print(f" - Nombre del archivo: {torrent_data['file_info']['name']}")
    print(f" - Tamaño total: {file_size} bytes")
    print(f" - Número de piezas: {len(piece_hashes)}")
    
if __name__ == '__main__':
    if len(sys.argv) != 3:
        print("Uso: python create_torrent.py <archivo_a_compartir> <ruta_salida.json>")
        sys.exit(1)
        
    input_file = sys.argv[1]
    output_file = sys.argv[2]
    create_torrent_file(input_file, output_file)