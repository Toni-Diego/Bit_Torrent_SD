# peer.py
import socket
import threading
import json
import os
import sys
import time
import hashlib
import uuid
from tqdm import tqdm

from config import PIECE_SIZE, TRACKER_ADDRESS, TRACKER_ANNOUNCE_INTERVAL, DOWNLOAD_STATE_EXTENSION
from utils import create_torrent_file, load_torrent_file, get_public_ip

class Peer:
    def __init__(self, server_port):
        self.server_port = server_port
        self.my_ip = get_public_ip()
        self.peer_id = f"PY-{self.my_ip}:{self.server_port}-{str(uuid.uuid4())[:8]}"

        # State management
        self.seeding_torrents = {}  # {torrent_hash: torrent_metadata}
        self.downloading_torrents = {}  # {torrent_hash: DownloadManager}
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

    def start(self):
        """Inicia los hilos principales del peer: servidor y anunciador."""
        print(f"Peer iniciado con ID: {self.peer_id}")
        print(f"IP pública detectada: {self.my_ip}")
        
        # Hilo para actuar como servidor para otros peers
        server_thread = threading.Thread(target=self.run_server, daemon=True)
        server_thread.start()

        # Hilo para anunciar periódicamente al tracker
        announcer_thread = threading.Thread(target=self.periodic_announce, daemon=True)
        announcer_thread.start()

        self.main_menu()

    def main_menu(self):
        """Muestra el menú principal y maneja la entrada del usuario."""
        while not self.stop_event.is_set():
            print("\n--- Menú Principal ---")
            print("1. Compartir un nuevo archivo (crear .torrent)")
            print("2. Descargar un archivo (usar .torrent)")
            print("3. Ver estado de descargas y subidas")
            print("4. Salir")
            choice = input("Selecciona una opción: ")

            if choice == '1':
                file_path = input("Introduce la ruta completa del archivo a compartir: ")
                if os.path.exists(file_path):
                    torrent_file = create_torrent_file(file_path)
                    if torrent_file:
                        torrent_data = load_torrent_file(torrent_file)
                        with self.lock:
                            self.seeding_torrents[torrent_data['torrent_hash']] = {
                                'metadata': torrent_data,
                                'local_path': file_path
                            }
                        print(f"Archivo '{torrent_data['file_name']}' ahora está siendo compartido.")
                else:
                    print("Error: El archivo no existe.")
            elif choice == '2':
                torrent_path = input("Introduce la ruta del archivo .torrent para descargar: ")
                if os.path.exists(torrent_path):
                    self.start_download(torrent_path)
                else:
                    print("Error: El archivo .torrent no existe.")
            elif choice == '3':
                self.show_status()
            elif choice == '4':
                print("Cerrando peer...")
                self.stop_event.set()
                break
            else:
                print("Opción no válida.")

    def run_server(self):
        """
        Ejecuta el servidor del peer para atender peticiones de piezas de otros peers.
        """
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.bind(('', self.server_port)) # Escucha en todas las interfaces
        server_socket.listen(5)
        print(f"Servidor del peer escuchando en el puerto {self.server_port}...")

        while not self.stop_event.is_set():
            try:
                # Usamos un timeout para poder chequear el stop_event periódicamente
                server_socket.settimeout(1.0)
                client_socket, addr = server_socket.accept()
                handler_thread = threading.Thread(target=self.handle_peer_request, args=(client_socket, addr), daemon=True)
                handler_thread.start()
            except socket.timeout:
                continue
            except Exception as e:
                if not self.stop_event.is_set():
                    print(f"Error en el servidor del peer: {e}")
                break
        server_socket.close()

    def handle_peer_request(self, client_socket, addr):
        """Maneja una solicitud de pieza de otro peer."""
        try:
            request_data = client_socket.recv(1024).decode('utf-8')
            request = json.loads(request_data)

            if request.get('command') == 'request_piece':
                torrent_hash = request['torrent_hash']
                piece_index = request['piece_index']
                
                with self.lock:
                    torrent_info = self.seeding_torrents.get(torrent_hash)

                if torrent_info:
                    file_path = torrent_info['local_path']
                    piece_size = torrent_info['metadata']['piece_size']
                    
                    with open(file_path, 'rb') as f:
                        f.seek(piece_index * piece_size)
                        piece_data = f.read(piece_size)
                        client_socket.sendall(piece_data)
                else:
                    # El peer podría estar descargando y ya tener la pieza
                    with self.lock:
                        download_manager = self.downloading_torrents.get(torrent_hash)
                    if download_manager and piece_index in download_manager.have_pieces:
                        file_path = download_manager.output_path
                        piece_size = download_manager.torrent_data['piece_size']
                        with open(file_path, 'rb') as f:
                            f.seek(piece_index * piece_size)
                            piece_data = f.read(piece_size)
                            client_socket.sendall(piece_data)
        except Exception as e:
            print(f"Error manejando solicitud de {addr}: {e}")
        finally:
            client_socket.close()

    def periodic_announce(self):
        """Contacta al tracker periódicamente para anunciar los torrents."""
        while not self.stop_event.is_set():
            with self.lock:
                all_torrents = list(self.seeding_torrents.items())
                # Incluye los torrents que está descargando
                for hash, dm in self.downloading_torrents.items():
                    if hash not in self.seeding_torrents:
                         all_torrents.append((hash, {'metadata': dm.torrent_data, 'progress': dm.get_progress()}))

            for torrent_hash, info in all_torrents:
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                        s.connect(TRACKER_ADDRESS)
                        # Si el torrent está siendo sembrado, el progreso es 1.0
                        progress = info.get('progress', 1.0)
                        
                        message = {
                            'command': 'announce',
                            'torrent_hash': torrent_hash,
                            'peer_id': self.peer_id,
                            'port': self.server_port,
                            'progress': progress
                        }
                        s.sendall(json.dumps(message).encode('utf-8'))
                        s.recv(1024) # Esperar confirmación
                except Exception:
                    # Silencioso para no llenar la consola, pero podría loguear el error
                    pass
            
            time.sleep(TRACKER_ANNOUNCE_INTERVAL)

    def start_download(self, torrent_path):
        """Inicia el proceso de descarga para un .torrent."""
        torrent_data = load_torrent_file(torrent_path)
        if not torrent_data:
            return
        
        torrent_hash = torrent_data['torrent_hash']
        with self.lock:
            if torrent_hash in self.downloading_torrents or torrent_hash in self.seeding_torrents:
                print("Ya estás descargando o compartiendo este archivo.")
                return

            download_manager = DownloadManager(torrent_data, self.peer_id)
            self.downloading_torrents[torrent_hash] = download_manager

        download_thread = threading.Thread(target=download_manager.start, args=(self.stop_event, self.lock, self.seeding_torrents, self.downloading_torrents), daemon=True)
        download_thread.start()
        print(f"Descarga iniciada para '{torrent_data['file_name']}'.")

    def show_status(self):
        """Muestra el estado actual de las descargas y subidas."""
        print("\n--- Estado del Peer ---")
        with self.lock:
            if self.seeding_torrents:
                print("\nArchivos compartiendo (Seed):")
                for _, info in self.seeding_torrents.items():
                    print(f"  - {info['metadata']['file_name']} (Completo)")
            
            if self.downloading_torrents:
                print("\nArchivos descargando (Leech):")
                for _, dm in self.downloading_torrents.items():
                    dm.print_progress()
            
            if not self.seeding_torrents and not self.downloading_torrents:
                print("No hay actividad de archivos.")


class DownloadManager:
    """Gestiona la descarga de un único torrent."""
    def __init__(self, torrent_data, peer_id):
        self.torrent_data = torrent_data
        self.peer_id = peer_id
        self.total_pieces = len(torrent_data['pieces_hashes'])
        self.output_path = os.path.join("./downloads", torrent_data['file_name']) # Guarda en una carpeta 'downloads'
        self.state_path = self.output_path + DOWNLOAD_STATE_EXTENSION
        
        self.have_pieces = set()
        self.needed_pieces = set(range(self.total_pieces))
        self.lock = threading.Lock()
        
        # tqdm progress bar
        self.pbar = tqdm(total=self.total_pieces, unit='piece', desc=f"Descargando {torrent_data['file_name'][:20]}...")

        # Prepara el directorio y carga el estado si existe
        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        self.load_state()

    def load_state(self):
        """Carga el estado de la descarga (tolerancia a fallos)."""
        if os.path.exists(self.state_path):
            print("Reanudando descarga...")
            with open(self.state_path, 'r') as f:
                state = json.load(f)
                self.have_pieces = set(state.get('have_pieces', []))
                self.needed_pieces -= self.have_pieces
                self.pbar.update(len(self.have_pieces))
        else:
            # Crea un archivo vacío del tamaño final para poder escribir piezas en cualquier posición.
            with open(self.output_path, 'wb') as f:
                f.truncate(self.torrent_data['file_size'])

    def save_state(self):
        """Guarda el conjunto de piezas que ya tenemos."""
        with self.lock:
            with open(self.state_path, 'w') as f:
                json.dump({'have_pieces': list(self.have_pieces)}, f)

    def get_progress(self):
        with self.lock:
            return len(self.have_pieces) / self.total_pieces if self.total_pieces > 0 else 0

    def print_progress(self):
        # tqdm maneja la impresión de la barra de progreso
        pass

    def start(self, stop_event, global_lock, seeding_torrents, downloading_torrents):
        """Bucle principal de descarga."""
        while self.needed_pieces and not stop_event.is_set():
            peers = self.get_peers_from_tracker()
            if not peers:
                time.sleep(10)
                continue

            # Aquí se podría implementar una lógica más compleja para descargar de varios peers a la vez
            # Por simplicidad, intentaremos descargar de ellos secuencialmente
            piece_to_download = self.needed_pieces.pop() # Estrategia simple: tomar cualquiera

            piece_data = None
            for peer in peers:
                piece_data = self.download_piece(peer, piece_to_download)
                if piece_data:
                    break
            
            if piece_data:
                # Escribir la pieza en el archivo y actualizar el estado
                with open(self.output_path, 'r+b') as f:
                    f.seek(piece_to_download * self.torrent_data['piece_size'])
                    f.write(piece_data)
                
                with self.lock:
                    self.have_pieces.add(piece_to_download)
                self.save_state()
                self.pbar.update(1)
            else:
                # Si no se pudo descargar, la devolvemos a la lista de pendientes
                self.needed_pieces.add(piece_to_download)
                time.sleep(2) # Esperar antes de reintentar
        
        self.pbar.close()
        if not self.needed_pieces:
            print(f"\n¡Descarga completa para '{self.torrent_data['file_name']}'!")
            os.remove(self.state_path) # Limpiar archivo de estado
            # Mover de descargando a compartiendo
            with global_lock:
                del downloading_torrents[self.torrent_data['torrent_hash']]
                seeding_torrents[self.torrent_data['torrent_hash']] = {
                    'metadata': self.torrent_data,
                    'local_path': self.output_path
                }
        else:
            print(f"\nDescarga para '{self.torrent_data['file_name']}' interrumpida.")

    def get_peers_from_tracker(self):
        """Obtiene la lista de peers del tracker."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(TRACKER_ADDRESS)
                message = {
                    'command': 'get_peers',
                    'torrent_hash': self.torrent_data['torrent_hash'],
                    'peer_id': self.peer_id
                }
                s.sendall(json.dumps(message).encode('utf-8'))
                response = json.loads(s.recv(4096).decode('utf-8'))
                return response.get('peers', [])
        except Exception as e:
            print(f"Error al contactar al tracker: {e}")
            return []

    def download_piece(self, peer_info, piece_index):
        """Intenta descargar una pieza específica de un peer."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect((peer_info['ip'], peer_info['port']))
                request = {
                    'command': 'request_piece',
                    'torrent_hash': self.torrent_data['torrent_hash'],
                    'piece_index': piece_index
                }
                s.sendall(json.dumps(request).encode('utf-8'))

                piece_data = b''
                while len(piece_data) < self.torrent_data['piece_size']:
                    chunk = s.recv(4096)
                    if not chunk:
                        # Si el peer cierra la conexión antes, la pieza está incompleta
                        return None
                    piece_data += chunk
                
                # Verificar el hash de la pieza
                if hashlib.sha1(piece_data).hexdigest() == self.torrent_data['pieces_hashes'][piece_index]:
                    return piece_data
                else:
                    print(f"Error: Hash de la pieza {piece_index} no coincide. Descartando.")
                    return None
        except Exception:
            return None


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python peer.py <puerto>")
        sys.exit(1)
    
    try:
        my_port = int(sys.argv[1])
    except ValueError:
        print("Error: El puerto debe ser un número.")
        sys.exit(1)
        
    peer = Peer(server_port=my_port)
    peer.start()