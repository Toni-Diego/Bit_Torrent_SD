# tracker.py (Corregido)
import socketserver
import threading
import json
import time
import sys
from config import TRACKER_HOST

# Estructura de datos para almacenar la información de la red.
torrents = {}
lock = threading.Lock()

class TrackerHandler(socketserver.BaseRequestHandler):
    """
    Maneja las conexiones entrantes de los peers.
    Una instancia de esta clase es creada para cada conexión.
    """
    def handle(self):
        try:
            raw_data = self.request.recv(1024).strip()
            if not raw_data:
                return
            
            message = json.loads(raw_data.decode('utf-8'))
            command = message.get('command')
            
            with lock:
                if command == 'announce':
                    self.handle_announce(message)
                elif command == 'get_peers':
                    self.handle_get_peers(message)
        except (json.JSONDecodeError, ConnectionResetError) as e:
            # Errores comunes que pueden ignorarse en un entorno ocupado
            pass
        except Exception as e:
            print(f"Error inesperado manejando a {self.client_address}: {e}")

    def handle_announce(self, message):
        """
        Maneja el anuncio de un peer.
        Actualiza el estado del peer para un torrent específico.
        """
        torrent_hash = message['torrent_hash']
        peer_id = message['peer_id']
        peer_port = message['port']
        progress = message['progress']
        peer_ip = self.client_address[0]

        if torrent_hash not in torrents:
            torrents[torrent_hash] = {}
        
        torrents[torrent_hash][peer_id] = {
            'ip': peer_ip,
            'port': peer_port,
            'progress': progress,
            'last_seen': time.time()
        }
        
        # Confirma el anuncio al peer
        response = {'status': 'ok', 'message': 'Anuncio recibido.'}
        self.request.sendall(json.dumps(response).encode('utf-8'))

    def handle_get_peers(self, message):
        """
        Devuelve la lista de peers que tienen un torrent.
        """
        torrent_hash = message['torrent_hash']
        requesting_peer_id = message['peer_id']
        
        peer_list = []
        if torrent_hash in torrents:
            all_peers = torrents[torrent_hash]
            for pid, info in all_peers.items():
                if pid != requesting_peer_id and info['progress'] > 0:
                    peer_list.append({'peer_id': pid, 'ip': info['ip'], 'port': info['port']})
        
        response = {'peers': peer_list}
        self.request.sendall(json.dumps(response).encode('utf-8'))

def clean_inactive_peers():
    """Limpia periódicamente los peers que no se han anunciado recientemente."""
    while True:
        time.sleep(60) # Ejecutar cada minuto
        with lock:
            current_time = time.time()
            inactive_peers_to_remove = []
            for torrent_hash, peers in torrents.items():
                for peer_id, info in peers.items():
                    if current_time - info['last_seen'] > 60: # Inactivo por más de 60s
                        inactive_peers_to_remove.append((torrent_hash, peer_id))
            
            for torrent_hash, peer_id in inactive_peers_to_remove:
                if torrent_hash in torrents and peer_id in torrents[torrent_hash]:
                    del torrents[torrent_hash][peer_id]
                    print(f"\n[INFO] Peer inactivo {peer_id[:15]}... eliminado.")
                if torrent_hash in torrents and not torrents[torrent_hash]:
                    del torrents[torrent_hash]

def print_tracker_status():
    """
    Imprime el estado actual de la red en la consola del tracker.
    Se ejecuta en un hilo separado.
    """
    while True:
        with lock:
            # Limpiar la consola para una visualización más clara
            # os.system('cls' if os.name == 'nt' else 'clear')
            print("\n" + "="*80)
            print(f"Estado del Tracker - {time.ctime()}")
            print("="*80)
            if not torrents:
                print("No hay torrents activos en la red. Esperando peers...")
            else:
                for torrent_hash, peers in torrents.items():
                    print(f"\n--- Torrent: {torrent_hash[:30]}... ({len(peers)} peers) ---")
                    print(f"{'Peer ID':<35} {'Dirección IP:Puerto':<22} {'Progreso':<15}")
                    print("-" * 80)
                    for peer_id, info in peers.items():
                        progress_percent = f"{info['progress'] * 100:.1f}%"
                        status = "Seeder" if info['progress'] == 1.0 else "Leecher"
                        
                        # --- ESTA ES LA PARTE CORREGIDA ---
                        peer_address = f"{info['ip']}:{info['port']}"
                        print(f"{peer_id:<35} {peer_address:<22} {f'{progress_percent} ({status})':<15}")
        
        time.sleep(15) # Actualizar cada 15 segundos


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python3 tracker.py <puerto>")
        sys.exit(1)
        
    try:
        port = int(sys.argv[1])
    except ValueError:
        print("Error: El puerto debe ser un número.")
        sys.exit(1)

    # Iniciar el hilo que imprime el estado
    status_thread = threading.Thread(target=print_tracker_status, daemon=True)
    status_thread.start()

    # Iniciar el hilo que limpia peers inactivos
    cleaner_thread = threading.Thread(target=clean_inactive_peers, daemon=True)
    cleaner_thread.start()

    # Usamos ThreadingTCPServer para manejar cada conexión en un nuevo hilo
    server = socketserver.ThreadingTCPServer((TRACKER_HOST, port), TrackerHandler)
    print(f"Tracker iniciado en {TRACKER_HOST}:{port}. Esperando conexiones...")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrando el tracker...")
        server.shutdown()
        server.server_close()