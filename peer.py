# peer.py (Versión final con tolerancia a fallos robusta)
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
from utils import get_public_ip

# --- CLASE PRINCIPAL DEL PEER ---
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
        
        self.load_incomplete_downloads()

        server_thread = threading.Thread(target=self.run_server, daemon=True); server_thread.start()
        announcer_thread = threading.Thread(target=self.periodic_announce, daemon=True); announcer_thread.start()
        
        try:
            self.main_menu()
        except KeyboardInterrupt:
            print("\nCerrando peer...")
            self.stop_event.set()

    def load_incomplete_downloads(self):
        print("Buscando descargas para reanudar...")
        downloads_dir = "./downloads"
        if not os.path.exists(downloads_dir): os.makedirs(downloads_dir)

        for filename in os.listdir(downloads_dir):
            if filename.endswith(DOWNLOAD_STATE_EXTENSION):
                state_path = os.path.join(downloads_dir, filename)
                try:
                    with open(state_path, 'r') as f:
                        state_data = json.load(f)
                    
                    if 'metadata' in state_data and 'have_pieces' in state_data:
                        metadata = state_data['metadata']
                        print(f"-> Encontrada descarga incompleta: {metadata['file_name']}")
                        self.start_download(metadata, state_data['have_pieces'])
                except (json.JSONDecodeError, KeyError, FileNotFoundError) as e:
                    print(f"Error al cargar estado '{filename}': {e}. Omitiendo.")

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
                raise KeyboardInterrupt
            else: print("Opción no válida.")

    def share_new_file(self):
        file_path = input("Introduce la ruta completa del archivo a compartir: ")
        if not os.path.exists(file_path):
            print("Error: El archivo no existe."); return
        
        print("Creando metadatos del torrent...")
        torrent_metadata = self.create_torrent_metadata(file_path)
        if not torrent_metadata: return
        
        torrent_hash = torrent_metadata['torrent_hash']
        with self.lock:
            self.seeding_torrents[torrent_hash] = {'metadata': torrent_metadata, 'local_path': file_path}
        print(f"Archivo '{torrent_metadata['file_name']}' ahora está siendo compartido.")
        self.announce_once(torrent_hash, 1.0, torrent_metadata)

    def download_from_network(self):
        print("Obteniendo lista de archivos de la red...")
        response = self.request_from_tracker({'command': 'list_torrents'})
        
        if not response or 'torrents' not in response or not response['torrents']:
            print("No hay archivos disponibles en la red."); return

        print("\n--- Archivos Disponibles en la Red ---")
        torrents_list = response['torrents']
        for i, t in enumerate(torrents_list):
            size_mb = t['file_size'] / (1024 * 1024)
            print(f"{i+1}. {t['file_name']} ({size_mb:.2f} MB) - S:{t['seeders']}/L:{t['leechers']}")
        
        try:
            choice = int(input("Elige el número a descargar (0 para cancelar): "))
            if not (0 < choice <= len(torrents_list)): return
            
            selected_torrent_hash = torrents_list[choice-1]['torrent_hash']

            with self.lock:
                if selected_torrent_hash in self.seeding_torrents or selected_torrent_hash in self.downloading_torrents:
                    print("Ya tienes o estás descargando este archivo."); return

            response = self.request_from_tracker({'command': 'get_torrent_metadata', 'torrent_hash': selected_torrent_hash})
            if not response or 'metadata' not in response:
                print("Error: No se pudieron obtener los metadatos."); return
            
            self.start_download(response['metadata'])
        except (ValueError, IndexError):
            print("Selección no válida.")

    def start_download(self, torrent_metadata, initial_pieces=None):
        torrent_hash = torrent_metadata['torrent_hash']
        with self.lock:
            if torrent_hash in self.downloading_torrents or torrent_hash in self.seeding_torrents: return
            dm = DownloadManager(torrent_metadata, self.peer_id, self.request_from_tracker, initial_pieces)
            self.downloading_torrents[torrent_hash] = dm

        self.announce_once(torrent_hash, dm.get_progress())
        
        # El DownloadManager necesita una forma de notificar al Peer cuando termina
        def on_complete_callback(thash, local_path):
            with self.lock:
                if thash in self.downloading_torrents:
                    del self.downloading_torrents[thash]
                self.seeding_torrents[thash] = {'metadata': torrent_metadata, 'local_path': local_path}
            self.announce_once(thash, 1.0)

        download_thread = threading.Thread(target=dm.start, args=(self.stop_event, on_complete_callback), daemon=True)
        download_thread.start()
        print(f"Descarga iniciada/reanudada para '{torrent_metadata['file_name']}'.")

    def announce_once(self, torrent_hash, progress, metadata=None):
        message = {'command': 'announce', 'torrent_hash': torrent_hash, 'peer_id': self.peer_id, 'port': self.server_port, 'progress': progress}
        if metadata: message['metadata'] = metadata
        self.request_from_tracker(message)

    def periodic_announce(self):
        while not self.stop_event.is_set():
            time.sleep(TRACKER_ANNOUNCE_INTERVAL)
            with self.lock:
                all_torrents = list(self.seeding_torrents.items()) + list(self.downloading_torrents.items())
            
            for thash, data in all_torrents:
                progress = 1.0 if isinstance(data, dict) else data.get_progress()
                self.announce_once(thash, progress)

    def request_from_tracker(self, message):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(10)
                s.connect(TRACKER_ADDRESS)
                s.sendall(json.dumps(message).encode('utf-8'))
                response_data = s.recv(16384)
                return json.loads(response_data.decode('utf-8'))
        except Exception:
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
                client_socket, _ = server_socket.accept()
                threading.Thread(target=self.handle_peer_request, args=(client_socket,), daemon=True).start()
            except socket.timeout: continue
            except Exception: break
        server_socket.close()

    def handle_peer_request(self, client_socket):
        try:
            request_data = client_socket.recv(1024).decode('utf-8')
            request = json.loads(request_data)
            if request.get('command') == 'request_piece':
                thash, p_index = request['torrent_hash'], request['piece_index']
                piece_data = self.get_piece_data(thash, p_index)
                if piece_data: client_socket.sendall(piece_data)
        except Exception: pass
        finally: client_socket.close()
    
    def get_piece_data(self, torrent_hash, piece_index):
        with self.lock:
            if torrent_hash in self.seeding_torrents:
                info = self.seeding_torrents[torrent_hash]
            elif torrent_hash in self.downloading_torrents and piece_index in self.downloading_torrents[torrent_hash].have_pieces:
                dm = self.downloading_torrents[torrent_hash]
                info = {'metadata': dm.torrent_data, 'local_path': dm.output_path}
            else: return None
        
        with open(info['local_path'], 'rb') as f:
            f.seek(piece_index * info['metadata']['piece_size'])
            return f.read(info['metadata']['piece_size'])

    def show_status(self):
        print("\n--- Estado de Actividad ---")
        with self.lock:
            if self.seeding_torrents:
                print("\n[Archivos Compartiendo (Seed)]")
                for _, info in self.seeding_torrents.items():
                    print(f"  - {info['metadata']['file_name']}")
            if self.downloading_torrents:
                print("\n[Archivos Descargando (Leech)]")
                for dm in self.downloading_torrents.values():
                    dm.print_progress()
            if not self.seeding_torrents and not self.downloading_torrents:
                print("Sin actividad.")

# --- CLASE DE GESTIÓN DE DESCARGA ---
class DownloadManager:
    def __init__(self, torrent_data, peer_id, tracker_requester, initial_pieces=None):
        self.torrent_data = torrent_data
        self.peer_id = peer_id
        self.tracker_requester = tracker_requester
        self.total_pieces = len(torrent_data['pieces_hashes'])
        self.output_path = os.path.join("./downloads", torrent_data['file_name'])
        self.state_path = self.output_path + DOWNLOAD_STATE_EXTENSION
        
        self.lock = threading.Lock()
        self.have_pieces = set(initial_pieces or [])
        self.needed_pieces = set(range(self.total_pieces)) - self.have_pieces
        
        self.pbar = tqdm(total=self.total_pieces, unit='piece', desc=f"DL {torrent_data['file_name'][:15]}..", initial=len(self.have_pieces))
        
        if not os.path.exists(self.output_path): self.create_empty_file()
        self.save_state()

    def create_empty_file(self):
        with open(self.output_path, 'wb') as f:
            if self.torrent_data['file_size'] > 0:
                f.seek(self.torrent_data['file_size'] - 1)
                f.write(b'\0')

    def save_state(self):
        with self.lock:
            with open(self.state_path, 'w') as f:
                json.dump({'metadata': self.torrent_data, 'have_pieces': list(self.have_pieces)}, f)

    def get_progress(self):
        return len(self.have_pieces) / self.total_pieces if self.total_pieces > 0 else 0

    def print_progress(self):
        self.pbar.refresh()

    def start(self, stop_event, on_complete_callback):
        while self.needed_pieces and not stop_event.is_set():
            peers = self.get_peers_from_tracker()
            if not peers:
                time.sleep(10); continue
            
            try: piece_to_download = self.needed_pieces.pop()
            except KeyError: break
            
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
            on_complete_callback(self.torrent_data['torrent_hash'], self.output_path)
        else:
            print(f"\nDescarga para '{self.torrent_data['file_name']}' interrumpida. Se reanudará al reiniciar.")

    def write_piece(self, index, data):
        with open(self.output_path, 'r+b') as f:
            f.seek(index * self.torrent_data['piece_size'])
            f.write(data)
        with self.lock:
            self.have_pieces.add(index)
        self.save_state()
        self.pbar.update(1)

    def get_peers_from_tracker(self):
        response = self.tracker_requester({'command': 'get_peers', 'torrent_hash': self.torrent_data['torrent_hash'], 'peer_id': self.peer_id})
        return response.get('peers', []) if response else []

    def download_piece(self, peer_info, piece_index):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect((peer_info['ip'], peer_info['port']))
                s.sendall(json.dumps({'command': 'request_piece', 'torrent_hash': self.torrent_data['torrent_hash'], 'piece_index': piece_index}).encode('utf-8'))
                
                piece_data = b''
                piece_size = self.torrent_data['piece_size']
                if piece_index == self.total_pieces - 1 and self.torrent_data['file_size'] % piece_size != 0:
                    expected_size = self.torrent_data['file_size'] % piece_size
                else:
                    expected_size = piece_size

                s.settimeout(10)
                while len(piece_data) < expected_size:
                    chunk = s.recv(4096)
                    if not chunk: return None
                    piece_data += chunk
                
                if hashlib.sha1(piece_data).hexdigest() == self.torrent_data['pieces_hashes'][piece_index]:
                    return piece_data
                else:
                    print(f"Error de hash en pieza {piece_index}. Reintentando..."); return None
        except Exception:
            return None

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python3 peer.py <puerto>"); sys.exit(1)
    
    try: my_port = int(sys.argv[1])
    except ValueError:
        print("Error: El puerto debe ser un número."); sys.exit(1)
        
    Peer(server_port=my_port).start()