# utils.py
import hashlib
import json
import os
import socket
import requests
from config import PIECE_SIZE, TRACKER_ADDRESS

def get_public_ip():
    """
    Obtiene la IP pública consultando un servicio externo.
    Si falla, intenta obtener la IP local como fallback.
    """
    try:
        response = requests.get('https://api.ipify.org?format=json', timeout=3)
        response.raise_for_status()
        return response.json()['ip']
    except requests.RequestException:
        print("Advertencia: No se pudo obtener la IP pública. Usando IP local.")
        try:
            # Fallback a IP local
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80)) # Conectar a un servidor externo para obtener la IP local de la interfaz de red
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            print("Advertencia: No se pudo obtener la IP local. Usando 'localhost'.")
            return '127.0.0.1' # Último recurso

def create_torrent_file(file_path: str):
    """
    Crea un archivo .torrent (en formato JSON para esta simulación)
    a partir de un archivo de entrada.
    Esto es análogo a la clase Torrent.java.
    """
    if not os.path.exists(file_path):
        print(f"Error: El archivo '{file_path}' no existe.")
        return None

    try:
        file_size = os.path.getsize(file_path)
        pieces_hashes = []
        
        print(f"Creando torrent para '{os.path.basename(file_path)}'...")
        with open(file_path, 'rb') as f:
            while True:
                piece = f.read(PIECE_SIZE)
                if not piece:
                    break
                # Usamos SHA1, el estándar en BitTorrent
                piece_hash = hashlib.sha1(piece).hexdigest()
                pieces_hashes.append(piece_hash)
        
        # El hash del torrent identifica unívocamente al archivo en la red
        torrent_hash = hashlib.sha1(str(pieces_hashes).encode()).hexdigest()

        torrent_data = {
            'tracker_url': f"{TRACKER_ADDRESS[0]}:{TRACKER_ADDRESS[1]}",
            'file_name': os.path.basename(file_path),
            'file_size': file_size,
            'piece_size': PIECE_SIZE,
            'pieces_hashes': pieces_hashes,
            'torrent_hash': torrent_hash
        }
        
        torrent_file_path = file_path + '.torrent'
        with open(torrent_file_path, 'w') as f:
            json.dump(torrent_data, f, indent=4)
        
        print(f"Archivo .torrent creado en: '{torrent_file_path}'")
        return torrent_file_path
    except Exception as e:
        print(f"Error al crear el archivo .torrent: {e}")
        return None

def load_torrent_file(torrent_file_path: str):
    """Carga los metadatos desde un archivo .torrent."""
    try:
        with open(torrent_file_path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"Error al cargar el archivo .torrent: {e}")
        return None

def send_message(sock, message):
    """Envía un mensaje JSON serializado a través de un socket."""
    sock.sendall(json.dumps(message).encode('utf-8'))

def receive_message(sock):
    """Recibe y deserializa un mensaje JSON de un socket."""
    # Este es un enfoque simple. Una implementación robusta necesitaría manejar
    # el tamaño del mensaje para asegurarse de recibirlo todo.
    data = sock.recv(4096).decode('utf-8')
    if not data:
        return None
    return json.loads(data)