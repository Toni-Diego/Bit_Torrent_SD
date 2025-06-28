# peer.py
import socket
import json
import hashlib
import os
import sys
import threading
import time
import random
from config import TRACKER_ADDRESS, PIECE_SIZE, TRACKER_ANNOUNCE_INTERVAL

class Peer:
    def __init__(self, torrent_file, my_port):
        self.torrent_file = torrent_file
        self.my_port = my_port
        self.my_ip = 'localhost' # O usa una función para obtener la IP pública/local
        self.load_torrent_data()
        self.init_download_state()
        self.peer_id = self.generate_peer_id()
        print(f"Peer ID: {self.peer_id}")

    def generate_peer_id(self):
        return '-PY0001-' + ''.join(random.choices('0123456789', k=12))

    def load_torrent_data(self):
        """Carga la información del archivo .json."""
        with open(self.torrent_file, 'r') as f:
            self.torrent_data = json.load(f)
        self.file_info = self.torrent_data['file_info']
        self.torrent_hash = hashlib.sha1(json.dumps(self.file_info).encode()).hexdigest()
        self.num_pieces = len(self.file_info['pieces'])
        self.output_file = os.path.join("downloads", self.file_info['name'])
        self.state_file = os.path.join("downloads", self.file_info['name'] + ".state")
        os.makedirs("downloads", exist_ok=True)

    def init_download_state(self):
        """Inicializa el estado de la descarga, reanudando si es posible."""
        # TOLERANCIA A FALLOS: Comprobar si existe un archivo de estado
        if os.path.exists(self.state_file):
            print("Reanudando descarga desde estado previo...")
            with open(self.state_file, 'r') as f:
                state = json.load(f)
                self.pieces_i_have = state['pieces_i_have']
        else:
            # Si el archivo de salida ya existe, asumimos que es una semilla (seeder)
            if os.path.exists(self.output_file) and os.path.getsize(self.output_file) == self.file_info['size']:
                print("El archivo ya existe. Operando como seeder inicial.")
                self.pieces_i_have = [True] * self.num_pieces
            else:
                print("Iniciando nueva descarga.")
                self.pieces_i_have = [False] * self.num_pieces
                # Crear un archivo vacío del tamaño correcto para escribir las piezas
                with open(self.output_file, 'wb') as f:
                    f.seek(self.file_info['size'] - 1)
                    f.write(b'\0')
        
        self.pieces_in_progress = [False] * self.num_pieces
        print(f"Estado inicial: {self.pieces_i_have.count(True)}/{self.num_pieces} piezas.")

    def save_state(self):
        """Guarda el estado actual de las piezas en el archivo .state."""
        with open(self.state_file, 'w') as f:
            json.dump({'pieces_i_have': self.pieces_i_have}, f)

    def start(self):
        """Inicia el peer: servidor en un hilo y cliente en el hilo principal."""
        # CONCURRENCIA: Iniciar el servidor en un hilo separado
        server_thread = threading.Thread(target=self.run_server)
        server_thread.daemon = True # Permite que el programa principal termine aunque el hilo esté activo
        server_thread.start()
        print(f"Servidor del peer escuchando en {self.my_ip}:{self.my_port}")

        # Inicia el cliente en el hilo principal
        self.run_client()

    def run_server(self):
        """
        Escucha peticiones de otros peers y les envía las piezas que tiene.
        Corresponde a la parte 'servidor' de Peer.java.
        """
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.bind((self.my_ip, self.my_port))
        server_socket.listen(5)
        
        while True:
            conn, addr = server_socket.accept()
            # CONCURRENCIA: Manejar cada conexión de peer en un nuevo hilo
            handler_thread = threading.Thread(target=self.handle_peer_request, args=(conn, addr))
            handler_thread.start()
    
    def handle_peer_request(self, conn, addr):
        """Maneja la lógica para una única petición de otro peer."""
        print(f"Conexión entrante de {addr}")
        try:
            data = conn.recv(1024)
            if not data:
                return
            message = json.loads(data.decode('utf-8'))

            if message.get('action') == 'request_piece':
                piece_index = message['piece_index']
                if self.pieces_i_have[piece_index]:
                    with open(self.output_file, 'rb') as f:
                        f.seek(piece_index * PIECE_SIZE)
                        piece_data = f.read(PIECE_SIZE)
                    
                    response = {
                        'action': 'send_piece',
                        'piece_index': piece_index,
                        'data': piece_data.hex() # Enviar datos binarios como hex
                    }
                    conn.sendall(json.dumps(response).encode('utf-8'))
                    print(f"Enviada pieza {piece_index} a {addr}")
                else:
                    # Opcional: enviar un mensaje de que no se tiene la pieza
                    pass
        except Exception as e:
            print(f"Error manejando petición de {addr}: {e}")
        finally:
            conn.close()

    def run_client(self):
        """
        Contacta al tracker, obtiene la lista de peers y descarga piezas.
        Corresponde a la parte 'cliente' de Peer.java y PeerConnection.java.
        """
        while self.pieces_i_have.count(True) < self.num_pieces:
            peers = self.announce_to_tracker()
            if not peers:
                print("No se encontraron peers. Reintentando en unos segundos...")
                time.sleep(TRACKER_ANNOUNCE_INTERVAL)
                continue
            
            # Descargar piezas de los peers disponibles
            self.download_from_peers(peers)
            
            # Esperar antes de volver a contactar al tracker
            time.sleep(TRACKER_ANNOUNCE_INTERVAL)

        print("\n¡Descarga completada!")
        print(f"Archivo guardado en: {self.output_file}")
        # Opcional: eliminar el archivo .state una vez completado
        if os.path.exists(self.state_file):
            os.remove(self.state_file)

    def announce_to_tracker(self):
        """Se anuncia al tracker y obtiene la lista de peers."""
        print("\nAnunciando al tracker...")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(TRACKER_ADDRESS)
                message = {
                    'action': 'announce',
                    'torrent_hash': self.torrent_hash,
                    'port': self.my_port,
                    'peer_id': self.peer_id
                }
                s.sendall(json.dumps(message).encode('utf-8'))
                
                response_data = s.recv(4096)
                response = json.loads(response_data.decode('utf-8'))
                
                # Convertir lista de listas a lista de tuplas
                peers = [tuple(p) for p in response['peers']]
                print(f"Tracker respondió con {len(peers)} peers: {peers}")
                return peers
        except Exception as e:
            print(f"Error al contactar al tracker: {e}")
            return []

    def download_from_peers(self, peers):
        """Intenta descargar las piezas faltantes de la lista de peers."""
        # Estrategia simple: pedir piezas en orden aleatorio
        needed_pieces = [i for i, have in enumerate(self.pieces_i_have) if not have and not self.pieces_in_progress[i]]
        random.shuffle(needed_pieces)

        for piece_index in needed_pieces:
            if not peers:
                print("No hay peers disponibles para descargar.")
                break
                
            # Elegir un peer al azar para pedir la pieza
            peer_address = random.choice(peers)
            
            # Marcar la pieza como "en progreso" para no pedirla dos veces
            self.pieces_in_progress[piece_index] = True
            
            # CONCURRENCIA: Crear un hilo para descargar cada pieza
            # Esto permite descargas simultáneas de diferentes peers.
            download_thread = threading.Thread(target=self.request_piece, args=(peer_address, piece_index))
            download_thread.start()

    def request_piece(self, peer_address, piece_index):
        """Se conecta a un peer específico y le pide una pieza."""
        try:
            print(f"Intentando descargar pieza {piece_index} de {peer_address}...")
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(10) # Timeout para la conexión
                s.connect(tuple(peer_address))
                message = {'action': 'request_piece', 'piece_index': piece_index}
                s.sendall(json.dumps(message).encode('utf-8'))
                
                # Recibir la respuesta en un bucle para manejar mensajes grandes
                full_response = b""
                while True:
                    part = s.recv(PIECE_SIZE + 1024) # Un poco más grande que la pieza
                    if not part:
                        break
                    full_response += part
                
                if full_response:
                    response = json.loads(full_response.decode('utf-8'))
                    if response.get('action') == 'send_piece':
                        piece_data_hex = response['data']
                        piece_data = bytes.fromhex(piece_data_hex)
                        
                        # VALIDACIÓN: Verificar el hash de la pieza
                        if hashlib.sha1(piece_data).hexdigest() == self.file_info['pieces'][piece_index]:
                            self.write_piece_to_file(piece_index, piece_data)
                            print(f"✅ Pieza {piece_index} recibida y verificada de {peer_address}")
                        else:
                            print(f"❌ Error de hash para la pieza {piece_index} de {peer_address}. Descartada.")
                            self.pieces_in_progress[piece_index] = False # Marcar para reintentar
                else:
                    self.pieces_in_progress[piece_index] = False
        except Exception as e:
            print(f"Error al descargar pieza {piece_index} de {peer_address}: {e}")
            self.pieces_in_progress[piece_index] = False # Marcar para reintentar

    def write_piece_to_file(self, piece_index, data):
        """Escribe una pieza validada en la posición correcta del archivo de salida."""
        with open(self.output_file, 'r+b') as f: # r+b para leer y escribir en modo binario
            offset = piece_index * PIECE_SIZE
            f.seek(offset)
            f.write(data)
        
        self.pieces_i_have[piece_index] = True
        self.pieces_in_progress[piece_index] = False
        self.save_state() # Guardar el estado después de cada pieza exitosa

if __name__ == '__main__':
    if len(sys.argv) != 3:
        print("Uso: python peer.py <archivo.json> <puerto>")
        sys.exit(1)
        
    torrent_file_path = sys.argv[1]
    my_port = int(sys.argv[2])
    
    peer = Peer(torrent_file_path, my_port)
    peer.start()