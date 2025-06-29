# peer.py (Corregido con anuncio inmediato)
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
        
        server_thread = threading.Thread(target=self.run_server, daemon=True)
        server_thread.start()

        announcer_thread = threading.Thread(target=self.periodic_announce, daemon=True)
        announcer_thread.start()

        self.main_menu()

    def main_menu(self):
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
                        if torrent_data:
                            torrent_hash = torrent_data['torrent_hash']
                            with self.lock:
                                self.seeding_torrents[torrent_hash] = {
                                    'metadata': torrent_data,
                                    'local_path': file_path
                                }
                            print(f"Archivo '{torrent_data['file_name']}' ahora está siendo compartido.")
                            self.announce_once(torrent_hash, 1.0) # Anuncio inmediato
                else:
                    print("Error: El archivo no existe.")
            elif choice == '2':
                torrent_path = input("Introduce la ruta del archivo .torrent para descargar: ")
                #if os.path.exists(torrent_path): #Mal porque nunca va a existir la ruta, es una ruta externa
                self.start_download(torrent_path)
                # else:
                #     print("Error: El archivo .torrent no existe.")
            elif choice == '3':
                self.show_status()
            elif choice == '4':
                print("Cerrando peer...")
                self.stop_event.set()
                break
            else:
                print("Opción no válida.")

    def announce_once(self, torrent_hash, progress):
        """Realiza un único anuncio al tracker para un torrent específico."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect(TRACKER_ADDRESS)
                message = {
                    'command': 'announce',
                    'torrent_hash': torrent_hash,
                    'peer_id': self.peer_id,
                    'port': self.server_port,
                    'progress': progress
                }
                s.sendall(json.dumps(message).encode('utf-8'))
                s.recv(1024)
                print(f"[INFO] Anuncio enviado al tracker para {torrent_hash[:10]}...")
                return True
        except Exception as e:
            print(f"[ERROR] No se pudo contactar al tracker: {e}")
            return False

    def periodic_announce(self):
        """Contacta al tracker periódicamente para anunciar todos los torrents activos."""
        while not self.stop_event.is_set():
            time.sleep(TRACKER_ANNOUNCE_INTERVAL)
            with self.lock:
                all_torrents = []
                for thash, info in self.seeding_torrents.items():
                    all_torrents.append((thash, 1.0))
                for thash, dm in self.downloading_torrents.items():
                    if thash not in self.seeding_torrents:
                        all_torrents.append((thash, dm.get_progress()))
            
            if not all_torrents:
                continue

            # print(f"\n[INFO] Anuncio periódico para {len(all_torrents)} torrent(s)...")
            for torrent_hash, progress in all_torrents:
                self.announce_once(torrent_hash, progress)

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

    def start_download(self, torrent_path):
        torrent_data = load_torrent_file(torrent_path)
        if not torrent_data: return
        
        torrent_hash = torrent_data['torrent_hash']
        with self.lock:
            if torrent_hash in self.downloading_torrents or torrent_hash in self.seeding_torrents:
                print("Ya estás manejando este archivo.")
                return
            download_manager = DownloadManager(torrent_data, self.peer_id)
            self.downloading_torrents[torrent_hash] = download_manager

        initial_progress = download_manager.get_progress()
        self.announce_once(torrent_hash, initial_progress)

        download_thread = threading.Thread(target=download_manager.start, args=(self.stop_event, self.lock, self.seeding_torrents, self.downloading_torrents), daemon=True)
        download_thread.start()
        print(f"Descarga iniciada para '{torrent_data['file_name']}'.")

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
    """Gestiona la descarga de un único torrent."""
    def __init__(self, torrent_data, peer_id):
        self.torrent_data = torrent_data
        self.peer_id = peer_id
        self.total_pieces = len(torrent_data['pieces_hashes'])
        self.output_path = os.path.join("./downloads", torrent_data['file_name'])
        self.state_path = self.output_path + DOWNLOAD_STATE_EXTENSION
        
        self.have_pieces = set()
        self.needed_pieces = set(range(self.total_pieces))
        self.lock = threading.Lock()
        
        self.pbar = tqdm(total=self.total_pieces, unit='piece', desc=f"DL {torrent_data['file_name'][:15]}..", leave=False)

        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        self.load_state()

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
        with self.lock:
            with open(self.state_path, 'w') as f:
                json.dump({'have_pieces': list(self.have_pieces)}, f)

    def get_progress(self):
        with self.lock:
            return len(self.have_pieces) / self.total_pieces if self.total_pieces > 0 else 0

    def print_progress(self):
        self.pbar.refresh()

    def start(self, stop_event, global_lock, seeding_torrents, downloading_torrents):
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
            print(f"\n¡Descarga completa para '{self.torrent_data['file_name']}'!")
            if os.path.exists(self.state_path): os.remove(self.state_path)
            with global_lock:
                del downloading_torrents[self.torrent_data['torrent_hash']]
                seeding_torrents[self.torrent_data['torrent_hash']] = {
                    'metadata': self.torrent_data,
                    'local_path': self.output_path
                }
                self.announce_once_dm(1.0) # Anunciar que ahora es seeder
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
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect(TRACKER_ADDRESS)
                s.sendall(json.dumps({
                    'command': 'get_peers', 'torrent_hash': self.torrent_data['torrent_hash'], 'peer_id': self.peer_id
                }).encode('utf-8'))
                response = json.loads(s.recv(4096).decode('utf-8'))
                return response.get('peers', [])
        except Exception:
            return []

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