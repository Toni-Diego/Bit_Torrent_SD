# peer.py (Corregido con anuncio inmediato)
# peer.py (Rediseñado para descubrir y descargar torrents de la red)
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

        self.seeding_torrents = {}
        self.downloading_torrents = {}
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

    def start(self):
        print(f"Peer iniciado con ID: {self.peer_id}")
        print(f"IP detectada: {self.my_ip}")

        #descarga incpmleta
        self.load_incomplete_downloads()
        
        server_thread = threading.Thread(target=self.run_server, daemon=True); server_thread.start()
        announcer_thread = threading.Thread(target=self.periodic_announce, daemon=True); announcer_thread.start()
        self.main_menu()

    def load_incomplete_downloads(self):
        """Escanea la carpeta de descargas en busca de archivos .state para reanudar."""
        print("Buscando descargas incompletas para reanudar...")
        downloads_dir = "./downloads"
        if not os.path.exists(downloads_dir):
            return

        for filename in os.listdir(downloads_dir):
            if filename.endswith(DOWNLOAD_STATE_EXTENSION):
                state_path = os.path.join(downloads_dir, filename)
                try:
                    with open(state_path, 'r') as f:
                        state_data = json.load(f)
                    
                    # Verificar si el estado tiene la estructura correcta
                    if 'metadata' in state_data and 'have_pieces' in state_data:
                        metadata = state_data['metadata']
                        torrent_hash = metadata['torrent_hash']
                        
                        # Evitar cargar algo que ya está completo o siendo compartido
                        if os.path.exists(os.path.join(downloads_dir, metadata['file_name'])) and torrent_hash in self.seeding_torrents:
                            continue

                        print(f"Reanudando descarga para: {metadata['file_name']}")
                        self.start_download(metadata, state_data['have_pieces'])

                except (json.JSONDecodeError, KeyError, FileNotFoundError) as e:
                    print(f"Error al cargar el archivo de estado '{filename}': {e}. Omitiendo.")

    def main_menu(self):
        while not self.stop_event.is_set():
            print("\n--- Menú Principal ---")
            print("1. Compartir un nuevo archivo")
            print("2. Descargar un archivo de la red")
            print("3. Ver estado de actividad")
            print("4. Salir")
            choice = input("Selecciona una opción: ")

            if choice == '1': self.share_new_file()
            elif choice == '2': self.download_from_network()
            elif choice == '3': self.show_status()
            elif choice == '4':
                print("Cerrando peer..."); self.stop_event.set(); break
            else: print("Opción no válida.")

    def share_new_file(self):
        file_path = input("Introduce la ruta completa del archivo a compartir: ")
        if not os.path.exists(file_path):
            print("Error: El archivo no existe."); return
        
        # Ya no necesitamos el archivo .torrent, solo los metadatos en memoria
        print("Creando metadatos del torrent...")
        torrent_metadata = self.create_torrent_metadata(file_path)
        if not torrent_metadata:
            print("Error al crear los metadatos."); return
        
        torrent_hash = torrent_metadata['torrent_hash']
        with self.lock:
            self.seeding_torrents[torrent_hash] = {
                'metadata': torrent_metadata, 'local_path': file_path
            }
        print(f"Archivo '{torrent_metadata['file_name']}' ahora está siendo compartido.")
        # Anuncio inmediato con metadatos
        self.announce_once(torrent_hash, 1.0, torrent_metadata)

    def download_from_network(self):
        print("Obteniendo lista de archivos de la red...")
        available_torrents = self.request_from_tracker({'command': 'list_torrents'})
        
        if not available_torrents or 'torrents' not in available_torrents or not available_torrents['torrents']:
            print("No hay archivos disponibles en la red o no se pudo contactar al tracker."); return

        print("\n--- Archivos Disponibles en la Red ---")
        torrents_list = available_torrents['torrents']
        for i, t in enumerate(torrents_list):
            size_mb = t['file_size'] / (1024 * 1024)
            print(f"{i+1}. {t['file_name']} ({size_mb:.2f} MB) - Seeders: {t['seeders']}, Leechers: {t['leechers']}")
        
        try:
            choice = int(input("Elige el número del archivo a descargar (0 para cancelar): "))
            if choice == 0 or choice > len(torrents_list): return
            
            selected_torrent_info = torrents_list[choice-1]
            torrent_hash = selected_torrent_info['torrent_hash']

            # Verificar si ya lo tenemos o lo estamos descargando
            with self.lock:
                if torrent_hash in self.seeding_torrents or torrent_hash in self.downloading_torrents:
                    print("Ya tienes o estás descargando este archivo."); return

            print(f"Obteniendo metadatos para '{selected_torrent_info['file_name']}'...")
            response = self.request_from_tracker({'command': 'get_torrent_metadata', 'torrent_hash': torrent_hash})
            
            if not response or 'metadata' not in response:
                print("Error: No se pudieron obtener los metadatos del tracker."); return
            
            torrent_metadata = response['metadata']
            self.start_download(torrent_metadata)

        except (ValueError, IndexError):
            print("Selección no válida.")
    
    def announce_once(self, torrent_hash, progress, metadata=None):
        message = {
            'command': 'announce', 'torrent_hash': torrent_hash,
            'peer_id': self.peer_id, 'port': self.server_port,
            'progress': progress
        }
        if metadata:
            message['metadata'] = metadata
        
        response = self.request_from_tracker(message)
        if response:
            print(f"[INFO] Anuncio para {torrent_hash[:10]}... enviado al tracker.")
        else:
            print(f"[ERROR] Anuncio para {torrent_hash[:10]}... falló.")

    def periodic_announce(self):
        while not self.stop_event.is_set():
            time.sleep(TRACKER_ANNOUNCE_INTERVAL)
            with self.lock:
                all_hashes = list(self.seeding_torrents.keys()) + list(self.downloading_torrents.keys())
            
            for thash in set(all_hashes):
                progress = 1.0
                if thash in self.downloading_torrents:
                    progress = self.downloading_torrents[thash].get_progress()
                # En anuncios periódicos ya no enviamos metadatos
                self.announce_once(thash, progress)

    def start_download(self, torrent_metadata, initial_pieces=None):
        torrent_hash = torrent_metadata['torrent_hash']
        with self.lock:
            if torrent_hash in self.downloading_torrents or torrent_hash in self.seeding_torrents:
                return # Ya se está manejando
            
            download_manager = DownloadManager(torrent_metadata, self.peer_id, self.request_from_tracker, initial_pieces)
            self.downloading_torrents[torrent_hash] = download_manager

        initial_progress = download_manager.get_progress()
        self.announce_once(torrent_hash, initial_progress)

        download_thread = threading.Thread(target=download_manager.start, args=(self.stop_event, self.lock, self.seeding_torrents, self.downloading_torrents, self.announce_once), daemon=True)
        download_thread.start()
        print(f"Descarga iniciada/reanudada para '{torrent_metadata['file_name']}'.")

    # --- Funciones de utilidad y servidor (sin cambios mayores) ---
    def request_from_tracker(self, message):
        """Función centralizada para todas las peticiones al tracker."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect(TRACKER_ADDRESS)
                s.sendall(json.dumps(message).encode('utf-8'))
                response_data = s.recv(8192) # Aumentar buffer para metadatos
                return json.loads(response_data.decode('utf-8'))
        except (socket.timeout, ConnectionRefusedError, json.JSONDecodeError) as e:
            # print(f"[ERROR] No se pudo comunicar con el tracker: {e}")
            return None
    
    def create_torrent_metadata(self, file_path):
        try:
            file_size = os.path.getsize(file_path)
            pieces_hashes = []
            with open(file_path, 'rb') as f:
                while True:
                    piece = f.read(PIECE_SIZE)
                    if not piece: break
                    pieces_hashes.append(hashlib.sha1(piece).hexdigest())
            
            torrent_hash = hashlib.sha1(str(pieces_hashes).encode()).hexdigest()
            return {
                'file_name': os.path.basename(file_path), 'file_size': file_size,
                'piece_size': PIECE_SIZE, 'pieces_hashes': pieces_hashes,
                'torrent_hash': torrent_hash
            }
        except Exception as e:
            print(f"Error creando metadatos: {e}"); return None

    def run_server(self):
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.bind(('', self.server_port))
        server_socket.listen(5)
        print(f"Servidor del peer escuchando en el puerto {self.server_port}...")
        while not self.stop_event.is_set():
            try:
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
        try:
            request_data = client_socket.recv(1024).decode('utf-8')
            request = json.loads(request_data)

            if request.get('command') == 'request_piece':
                torrent_hash = request['torrent_hash']
                piece_index = request['piece_index']
                
                # Buscar en seeding
                piece_data = self.get_piece_from_storage(self.seeding_torrents, torrent_hash, piece_index)
                
                # Si no, buscar en downloading
                if not piece_data:
                    with self.lock:
                        dm = self.downloading_torrents.get(torrent_hash)
                    if dm and piece_index in dm.have_pieces:
                        file_path = dm.output_path
                        piece_size = dm.torrent_data['piece_size']
                        with open(file_path, 'rb') as f:
                            f.seek(piece_index * piece_size)
                            piece_data = f.read(piece_size)
                
                if piece_data:
                    client_socket.sendall(piece_data)
        except Exception as e:
            # print(f"Error manejando solicitud de {addr}: {e}")
            pass
        finally:
            client_socket.close()

    def get_piece_from_storage(self, storage, torrent_hash, piece_index):
        with self.lock:
            info = storage.get(torrent_hash)
        if info:
            file_path = info['local_path']
            piece_size = info['metadata']['piece_size']
            with open(file_path, 'rb') as f:
                f.seek(piece_index * piece_size)
                return f.read(piece_size)
        return None

    def show_status(self):
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
    '''Gestiona la descarga de un torrent'''
    def __init__(self, torrent_data, peer_id, tracker_requester, initial_pieces=None):
        self.torrent_data = torrent_data
        self.peer_id = peer_id
        self.tracker_requester = tracker_requester
        self.total_pieces = len(torrent_data['pieces_hashes'])
        self.output_path = os.path.join("./downloads", torrent_data['file_name'])
        self.state_path = self.output_path + DOWNLOAD_STATE_EXTENSION
        
        self.have_pieces = set(initial_pieces) if initial_pieces else set()
        self.needed_pieces = set(range(self.total_pieces)) - self.have_pieces
        self.lock = threading.Lock()
        
        self.pbar = tqdm(total=self.total_pieces, unit='piece', desc=f"DL {torrent_data['file_name'][:15]}..", leave=False)
        self.pbar.update(len(self.have_pieces)) # Actualizar barra con piezas existentes

        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        if not os.path.exists(self.output_path):
            self.create_empty_file()
        
        self.save_state() # Guardar el estado inicial (o reanudado)

    def load_state(self):
        if os.path.exists(self.state_path):
            print("Reanudando descarga...")
            try:
                with open(self.state_path, 'r') as f:
                    state = json.load(f)
                    self.have_pieces = set(state.get('have_pieces', []))
                    self.needed_pieces -= self.have_pieces
                    self.pbar.update(len(self.have_pieces))
            except (json.JSONDecodeError, FileNotFoundError):
                self.create_empty_file()
        else:
            self.create_empty_file()

    def create_empty_file(self):
        with open(self.output_path, 'wb') as f:
            if self.torrent_data['file_size'] > 0:
                f.seek(self.torrent_data['file_size'] - 1)
                f.write(b'\0')

    def save_state(self):
        """Guarda tanto los metadatos como las piezas que tenemos."""
        with self.lock:
            state_data = {
                'metadata': self.torrent_data,
                'have_pieces': list(self.have_pieces)
            }
            with open(self.state_path, 'w') as f:
                json.dump(state_data, f)

    def get_progress(self):
        with self.lock:
            return len(self.have_pieces) / self.total_pieces if self.total_pieces > 0 else 0

    def print_progress(self):
        self.pbar.refresh()

    def start(self, stop_event, global_lock, seeding_torrents, downloading_torrents, announce_func): #, announce_func
        while self.needed_pieces and not stop_event.is_set():
            peers = self.get_peers_from_tracker()
            if not peers:
                time.sleep(10)
                continue
            
            try:
                piece_to_download = self.needed_pieces.pop()
            except KeyError:
                break # No quedan piezas

            piece_data = None
            for peer in peers:
                piece_data = self.download_piece(peer, piece_to_download)
                if piece_data: break
            
            if piece_data:
                self.write_piece(piece_to_download, piece_data)
            else:
                self.needed_pieces.add(piece_to_download)
                time.sleep(2)
        
        self.pbar.close()
        if not self.needed_pieces:
            #announce_once_dm(self.torrent_data['torrent_hash'], 1.0)
            print(f"\n¡Descarga completa para '{self.torrent_data['file_name']}'!")
            if os.path.exists(self.state_path): os.remove(self.state_path)
            with global_lock:
                if self.torrent_data['torrent_hash'] in downloading_torrents:
                    del downloading_torrents[self.torrent_data['torrent_hash']]
                seeding_torrents[self.torrent_data['torrent_hash']] = {
                    'metadata': self.torrent_data,
                    'local_path': self.output_path
                }
                self.announce_once_dm(self.torrent_data['torrent_hash'], 1.0) # Anunciar que ahora es seeder
        else:
            print(f"\nDescarga para '{self.torrent_data['file_name']}' interrumpida.")

    def announce_once_dm(self, progress):
        """Método auxiliar para que el DM pueda anunciarse."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect(TRACKER_ADDRESS)
                s.sendall(json.dumps({
                    'command': 'announce', 'torrent_hash': self.torrent_data['torrent_hash'],
                    'peer_id': self.peer_id, 'port': int(self.peer_id.split(':')[-2]),
                    'progress': progress
                }).encode('utf-8'))
                s.recv(1024)
        except Exception:
            pass

    def write_piece(self, index, data):
        with open(self.output_path, 'r+b') as f:
            f.seek(index * self.torrent_data['piece_size'])
            f.write(data)
        with self.lock:
            self.have_pieces.add(index)
        self.save_state()
        self.pbar.update(1)

    def get_peers_from_tracker(self):
        """Obtiene la lista de peers del tracker."""
        message = {'command': 'get_peers', 'torrent_hash': self.torrent_data['torrent_hash'], 'peer_id': self.peer_id}
        response = self.tracker_requester(message)
        return response.get('peers', []) if response else []

    def download_piece(self, peer_info, piece_index):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect((peer_info['ip'], peer_info['port']))
                s.sendall(json.dumps({
                    'command': 'request_piece', 'torrent_hash': self.torrent_data['torrent_hash'], 'piece_index': piece_index
                }).encode('utf-8'))

                piece_data = b''
                expected_size = self.torrent_data['piece_size']
                # Última pieza puede ser más pequeña
                if piece_index == self.total_pieces - 1:
                    expected_size = self.torrent_data['file_size'] % self.torrent_data['piece_size']
                    if expected_size == 0: expected_size = self.torrent_data['piece_size']
                
                s.settimeout(10) # Timeout más largo para recibir datos
                while len(piece_data) < expected_size:
                    chunk = s.recv(4096)
                    if not chunk: return None
                    piece_data += chunk
                
                if hashlib.sha1(piece_data).hexdigest() == self.torrent_data['pieces_hashes'][piece_index]:
                    return piece_data
                else:
                    return None
        except Exception:
            return None

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python3 peer.py <puerto>")
        sys.exit(1)
    
    try:
        my_port = int(sys.argv[1])
    except ValueError:
        print("Error: El puerto debe ser un número.")
        sys.exit(1)
        
    peer = Peer(server_port=my_port)
    peer.start()