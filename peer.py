# peer.py (Corregido para tolerancia a fallos y reanudación automática)
import socket
import threading
import json
import os
import sys
import time
import hashlib
import uuid
from tqdm import tqdm
import random # ### NUEVO: Necesario para elegir peers al azar

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
        
        ### CAMBIO: Iniciar la reanudación de descargas ANTES del anunciador y el menú
        self.resume_interrupted_downloads()

        announcer_thread = threading.Thread(target=self.periodic_announce, daemon=True)
        announcer_thread.start()
        
        self.main_menu()

    def main_menu(self):
        while not self.stop_event.is_set():
            try:
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
            except KeyboardInterrupt:
                print("\nCerrando peer por interrupción..."); self.stop_event.set(); break
            except Exception as e:
                print(f"Error en el menú principal: {e}")
                self.stop_event.set()
                break
        
        # Esperar a que los hilos terminen limpiamente
        time.sleep(1)


    ### CAMBIO: Nueva función para escanear y reanudar descargas al inicio
    def resume_interrupted_downloads(self):
        print("Buscando descargas interrumpidas para reanudar...")
        download_dir = "./downloads"
        if not os.path.exists(download_dir):
            return
        
        for filename in os.listdir(download_dir):
            if filename.endswith(DOWNLOAD_STATE_EXTENSION):
                state_path = os.path.join(download_dir, filename)
                try:
                    with open(state_path, 'r') as f:
                        state_data = json.load(f)
                    
                    metadata = state_data.get('metadata')
                    if not metadata:
                        print(f"Archivo de estado {filename} corrupto o sin metadatos. Omitiendo.")
                        continue

                    # Verificar si la descarga ya está completa (no debería haber .state, pero por si acaso)
                    if len(state_data.get('have_pieces', [])) == len(metadata.get('pieces_hashes', [])):
                         print(f"La descarga para {metadata['file_name']} ya está completa. Limpiando estado.")
                         os.remove(state_path)
                         continue


                    print(f"Se encontró estado de descarga para: {metadata['file_name']}. Reanudando...")
                    # Usamos la misma lógica que para una descarga nueva para evitar duplicar código
                    self.start_download(metadata) 
                except (json.JSONDecodeError, KeyError, FileNotFoundError) as e:
                    print(f"Error al procesar el archivo de estado {filename}: {e}. Omitiendo.")

    def share_new_file(self):
        file_path = input("Introduce la ruta completa del archivo a compartir: ")
        if not os.path.exists(file_path):
            print("Error: El archivo no existe."); return
        
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
    
    def start_download(self, torrent_metadata):
        torrent_hash = torrent_metadata['torrent_hash']
        with self.lock:
            if torrent_hash in self.downloading_torrents or torrent_hash in self.seeding_torrents:
                # print("Ya estás manejando este archivo.") # Mensaje útil para depuración, pero puede ser confuso en reanudación
                return
            
            # El DownloadManager ahora es responsable de cargar su propio estado
            download_manager = DownloadManager(torrent_metadata, self.peer_id, self.request_from_tracker)
            self.downloading_torrents[torrent_hash] = download_manager

        # Anunciar al tracker inmediatamente con el progreso actual (que puede ser > 0 si se está reanudando)
        initial_progress = download_manager.get_progress()
        self.announce_once(torrent_hash, initial_progress)

        download_thread = threading.Thread(
            target=download_manager.start, 
            args=(self.stop_event, self.lock, self.seeding_torrents, self.downloading_torrents, self.announce_once), 
            daemon=True
        )
        download_thread.start()
        if initial_progress == 0:
            print(f"Descarga iniciada para '{torrent_metadata['file_name']}'.")

    def announce_once(self, torrent_hash, progress, metadata=None):
        message = {
            'command': 'announce', 'torrent_hash': torrent_hash,
            'peer_id': self.peer_id, 'port': self.server_port,
            'progress': progress
        }
        if metadata:
            message['metadata'] = metadata
        
        response = self.request_from_tracker(message)
        # Opcional: imprimir mensajes de anuncio solo si no es un anuncio periódico silencioso
        # if response:
        #     print(f"[INFO] Anuncio para {torrent_hash[:10]}... enviado al tracker.")
        # else:
        #     print(f"[ERROR] Anuncio para {torrent_hash[:10]}... falló.")


    def periodic_announce(self):
        while not self.stop_event.is_set():
            time.sleep(TRACKER_ANNOUNCE_INTERVAL)
            with self.lock:
                # Copiar las claves para evitar problemas de concurrencia si el diccionario cambia
                all_hashes = list(self.seeding_torrents.keys()) + list(self.downloading_torrents.keys())
            
            for thash in set(all_hashes):
                progress = 1.0
                if thash in self.downloading_torrents:
                    # Asegurarse de que el torrent aún está en la lista de descarga
                    with self.lock:
                        dm = self.downloading_torrents.get(thash)
                    if dm:
                        progress = dm.get_progress()
                
                self.announce_once(thash, progress)

    def request_from_tracker(self, message):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect(TRACKER_ADDRESS)
                s.sendall(json.dumps(message).encode('utf-8'))
                response_data = s.recv(8192) 
                return json.loads(response_data.decode('utf-8'))
        except (socket.timeout, ConnectionRefusedError, json.JSONDecodeError, OSError):
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
            
            # El hash del torrent se basa en los hashes de las piezas para ser consistente
            torrent_hash_content = str(sorted(pieces_hashes)).encode()
            torrent_hash = hashlib.sha1(torrent_hash_content).hexdigest()
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
        
        server_socket.settimeout(1.0)
        while not self.stop_event.is_set():
            try:
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
                
                piece_data = None
                with self.lock:
                    seeding_info = self.seeding_torrents.get(torrent_hash)
                    downloading_manager = self.downloading_torrents.get(torrent_hash)

                if seeding_info:
                    piece_data = self.get_piece_from_path(
                        seeding_info['local_path'], 
                        seeding_info['metadata']['piece_size'], 
                        piece_index
                    )
                elif downloading_manager and piece_index in downloading_manager.have_pieces:
                    piece_data = self.get_piece_from_path(
                        downloading_manager.output_path,
                        downloading_manager.torrent_data['piece_size'],
                        piece_index
                    )

                if piece_data:
                    client_socket.sendall(piece_data)
        except Exception:
            pass
        finally:
            client_socket.close()

    def get_piece_from_path(self, file_path, piece_size, piece_index):
        try:
            with open(file_path, 'rb') as f:
                f.seek(piece_index * piece_size)
                return f.read(piece_size)
        except FileNotFoundError:
            return None

    def show_status(self):
        print("\n--- Estado del Peer ---")
        with self.lock:
            if not self.seeding_torrents and not self.downloading_torrents:
                print("No hay actividad de archivos.")
                return

            if self.seeding_torrents:
                print("\nArchivos compartiendo (Seed):")
                for _, info in self.seeding_torrents.items():
                    print(f"  - {info['metadata']['file_name']} (Completo)")
            
            if self.downloading_torrents:
                print("\nArchivos descargando (Leech):")
                for _, dm in self.downloading_torrents.items():
                    dm.print_progress()
                    
                    ### CAMBIO: Mostrar los peers de los que se está descargando ###
                    active_sources = dm.get_active_sources()
                    if active_sources:
                        # Usamos el carácter └ para que se vea como un sub-ítem
                        print(f"      └─ Descargando de: {', '.join(active_sources)}")
                    else:
                        print(f"      └─ Buscando peers activos...")


class DownloadManager:
    def __init__(self, torrent_data, peer_id, tracker_requester):
        self.torrent_data = torrent_data
        self.peer_id = peer_id
        self.tracker_requester = tracker_requester
        self.total_pieces = len(torrent_data['pieces_hashes'])
        self.output_path = os.path.join("./downloads", torrent_data['file_name'])
        self.state_path = self.output_path + DOWNLOAD_STATE_EXTENSION
        
        self.have_pieces = set()
        self.needed_pieces = set(range(self.total_pieces))
        ### NUEVO: Conjunto para piezas que ya se están descargando ###
        self.pieces_in_progress = set()
        self.lock = threading.Lock()

        # Guardará -> {'ip:puerto': ultimo_tiempo_de_descarga}
        self.active_download_sources = {}
        ### NUEVO: Límite de descargas paralelas ###
        self.MAX_CONCURRENT_DOWNLOADS = 8
        
        self.pbar = tqdm(total=self.total_pieces, unit='piece', desc=f"DL {torrent_data['file_name'][:15]}..", leave=False)

        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
        self.load_state_or_initialize()

    def load_state_or_initialize(self):
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path, 'r') as f:
                    state = json.load(f)
                if state['metadata']['torrent_hash'] == self.torrent_data['torrent_hash']:
                    self.have_pieces = set(state.get('have_pieces', []))
                    self.needed_pieces -= self.have_pieces
                    self.pbar.update(len(self.have_pieces))
                else:
                    self.create_empty_file_and_state()
            except (json.JSONDecodeError, FileNotFoundError, KeyError):
                self.create_empty_file_and_state()
        else:
            self.create_empty_file_and_state()

    def create_empty_file_and_state(self):
        with open(self.output_path, 'wb') as f:
            if self.torrent_data['file_size'] > 0:
                f.seek(self.torrent_data['file_size'] - 1)
                f.write(b'\0')
        self.save_state()

    def save_state(self):
        with self.lock:
            state_data = {
                'metadata': self.torrent_data,
                'have_pieces': list(self.have_pieces)
            }
            with open(self.state_path, 'w') as f:
                json.dump(state_data, f)

    def get_progress(self):
        with self.lock:
            return len(self.have_pieces) / self.total_pieces if self.total_pieces > 0 else 1.0

    def print_progress(self):
        self.pbar.refresh()

    def get_active_sources(self, timeout=30):
        with self.lock:
            now = time.time()
            recent_sources = [
                addr for addr, last_seen in self.active_download_sources.items()
                if now - last_seen < timeout
            ]
            return recent_sources

    ### CAMBIO FUNDAMENTAL: El método start ahora gestiona workers ###
    def start(self, stop_event, global_lock, seeding_torrents, downloading_torrents, announce_func):
        worker_threads = []

        # El bucle principal continúa mientras no hayamos completado todas las piezas
        while len(self.have_pieces) < self.total_pieces and not stop_event.is_set():
            # Limpiar hilos que ya terminaron
            worker_threads = [t for t in worker_threads if t.is_alive()]

            peers = self.get_peers_from_tracker()
            if not peers:
                time.sleep(5)
                continue

            # Lanzar nuevos workers si hay espacio y piezas disponibles
            with self.lock:
                num_workers_to_start = self.MAX_CONCURRENT_DOWNLOADS - len(worker_threads)
                
                # Barajar la lista de piezas necesarias para no pedirlas siempre en orden
                available_pieces = list(self.needed_pieces - self.pieces_in_progress)
                random.shuffle(available_pieces)

            for piece_to_download in available_pieces[:num_workers_to_start]:
                # Asignar la pieza a un worker
                with self.lock:
                    self.pieces_in_progress.add(piece_to_download)
                
                # Elegir un peer al azar para esta pieza
                peer = random.choice(peers)

                # Crear y lanzar el hilo worker
                worker = threading.Thread(
                    target=self._worker_download_piece, 
                    args=(piece_to_download, peer), 
                    daemon=True
                )
                worker.start()
                worker_threads.append(worker)

            time.sleep(0.1) # Pequeña pausa para no saturar la CPU

        # Esperar a que los últimos workers terminen
        for t in worker_threads:
            t.join()

        self.pbar.close()
        # El resto de la lógica de finalización es la misma
        if not self.needed_pieces and not self.pieces_in_progress:
            print(f"\n¡Descarga completa para '{self.torrent_data['file_name']}'!")
            if os.path.exists(self.state_path):
                os.remove(self.state_path)
            
            with global_lock:
                if self.torrent_data['torrent_hash'] in downloading_torrents:
                    del downloading_torrents[self.torrent_data['torrent_hash']]
                
                seeding_torrents[self.torrent_data['torrent_hash']] = {
                    'metadata': self.torrent_data,
                    'local_path': self.output_path
                }
            announce_func(self.torrent_data['torrent_hash'], 1.0)
        else:
            if not stop_event.is_set():
                print(f"\nNo se pudieron encontrar más peers para '{self.torrent_data['file_name']}'.")
            else:
                print(f"\nDescarga para '{self.torrent_data['file_name']}' interrumpida.")

    ### NUEVO: Función que ejecuta cada worker thread ###
    def _worker_download_piece(self, piece_index, peer_info):
        # Evitar conectarse a uno mismo (doble verificación)
        if peer_info['ip'] == get_public_ip() and peer_info['port'] == int(self.peer_id.split(':')[1].split('-')[0]):
            with self.lock:
                self.pieces_in_progress.remove(piece_index)
            return

        piece_data = self.download_piece(peer_info, piece_index)
        
        if piece_data:
            # Éxito: Escribir la pieza y actualizar estado
            self.write_piece(piece_index, piece_data)
            with self.lock:
                peer_address = f"{peer_info['ip']}:{peer_info['port']}"
                self.active_download_sources[peer_address] = time.time()
                self.pieces_in_progress.remove(piece_index) # Ya no está "en progreso", está "descargada"
        else:
            # Fracaso: La pieza vuelve a estar disponible para otro worker
            with self.lock:
                self.pieces_in_progress.remove(piece_index)
    
    def write_piece(self, index, data):
        with open(self.output_path, 'r+b') as f:
            f.seek(index * self.torrent_data['piece_size'])
            f.write(data)
        
        with self.lock:
            self.have_pieces.add(index)
            self.needed_pieces.discard(index) # Eliminar de las necesitadas
        
        self.save_state()
        self.pbar.update(1)

    def get_peers_from_tracker(self):
        message = {'command': 'get_peers', 'torrent_hash': self.torrent_data['torrent_hash'], 'peer_id': self.peer_id}
        response = self.tracker_requester(message)
        return response.get('peers', []) if response else []

    def download_piece(self, peer_info, piece_index):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect((peer_info['ip'], peer_info['port']))
                s.sendall(json.dumps({
                    'command': 'request_piece', 
                    'torrent_hash': self.torrent_data['torrent_hash'], 
                    'piece_index': piece_index
                }).encode('utf-8'))

                piece_data = b''
                piece_size = self.torrent_data['piece_size']
                if piece_index == self.total_pieces - 1:
                    expected_size = self.torrent_data['file_size'] % piece_size
                    if expected_size == 0: expected_size = piece_size
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
                    return None
        except Exception:
            return None

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python3 peer.py <puerto_servidor>")
        sys.exit(1)
    
    try:
        my_port = int(sys.argv[1])
    except ValueError:
        print("Error: El puerto debe ser un número.")
        sys.exit(1)
        
    peer = Peer(server_port=my_port)
    try:
        peer.start()
    except KeyboardInterrupt:
        print("\nCerrando el peer...")
        peer.stop_event.set()