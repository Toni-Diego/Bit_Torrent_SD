# tracker.py (Con nuevas funciones para descubrir torrents)
import socketserver
import threading
import json
import time
import sys
# ¡Ojo! Tracker no necesita conocer TRACKER_HOST de config, se lo dicen los peers
# from config import TRACKER_HOST 

# Estructura de datos para almacenar la información de la red.
# Ahora guardamos los metadatos completos de cada torrent.
# {
#   "torrent_hash_1": {
#     "metadata": {...},
#     "peers": {
#        "peer_id_1": {"ip": ..., "port": ..., "progress": ..., "last_seen": ...}
#     }
#   }
# }
torrents = {}
lock = threading.Lock()

class TrackerHandler(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            raw_data = self.request.recv(16384).strip() # Aumentamos el buffer por si vienen metadatos
            if not raw_data: return
            
            message = json.loads(raw_data.decode('utf-8'))
            command = message.get('command')
            
            with lock:
                if command == 'announce':
                    self.handle_announce(message)
                elif command == 'get_peers':
                    self.handle_get_peers(message)
                # --- NUEVOS COMANDOS ---
                elif command == 'list_torrents':
                    self.handle_list_torrents()
                elif command == 'get_torrent_metadata':
                    self.handle_get_torrent_metadata(message)

        except (json.JSONDecodeError, ConnectionResetError): pass
        except Exception as e:
            print(f"Error inesperado manejando a {self.client_address}: {e}")

    def handle_announce(self, message):
        torrent_hash = message['torrent_hash']
        peer_id = message['peer_id']
        peer_port = message['port']
        progress = message['progress']
        peer_ip = self.client_address[0]
        
        # Si es un torrent nuevo, el peer DEBE enviar los metadatos
        if torrent_hash not in torrents:
            if 'metadata' not in message:
                # No podemos registrar un torrent sin sus metadatos
                return
            torrents[torrent_hash] = {
                'metadata': message['metadata'],
                'peers': {}
            }
        
        torrents[torrent_hash]['peers'][peer_id] = {
            'ip': peer_ip, 'port': peer_port,
            'progress': progress, 'last_seen': time.time()
        }
        
        response = {'status': 'ok', 'message': 'Anuncio recibido.'}
        self.request.sendall(json.dumps(response).encode('utf-8'))

    def handle_get_peers(self, message):
        torrent_hash = message['torrent_hash']
        requesting_peer_id = message['peer_id']
        
        peer_list = []
        if torrent_hash in torrents:
            all_peers = torrents[torrent_hash]['peers']
            for pid, info in all_peers.items():
                if pid != requesting_peer_id and info['progress'] > 0:
                    peer_list.append({'peer_id': pid, 'ip': info['ip'], 'port': info['port']})
        
        self.request.sendall(json.dumps({'peers': peer_list}).encode('utf-8'))

    def handle_list_torrents(self):
        """Devuelve una lista de todos los torrents disponibles."""
        available_torrents = []
        for hash, data in torrents.items():
            # Contar seeders y leechers
            seeders = sum(1 for p in data['peers'].values() if p['progress'] == 1.0)
            leechers = len(data['peers']) - seeders
            
            available_torrents.append({
                'torrent_hash': hash,
                'file_name': data['metadata']['file_name'],
                'file_size': data['metadata']['file_size'],
                'seeders': seeders,
                'leechers': leechers
            })
        
        self.request.sendall(json.dumps({'torrents': available_torrents}).encode('utf-8'))

    def handle_get_torrent_metadata(self, message):
        """Devuelve los metadatos completos para un torrent específico."""
        torrent_hash = message['torrent_hash']
        if torrent_hash in torrents:
            metadata = torrents[torrent_hash]['metadata']
            self.request.sendall(json.dumps({'metadata': metadata}).encode('utf-8'))
        else:
            self.request.sendall(json.dumps({'error': 'Torrent no encontrado'}).encode('utf-8'))

# ... (Las funciones clean_inactive_peers y print_tracker_status se modifican ligeramente para el nuevo formato de `torrents`)

def clean_inactive_peers():
    while True:
        time.sleep(15)
        with lock:
            current_time = time.time()
            for torrent_hash, data in list(torrents.items()):
                inactive_peers = [
                    peer_id for peer_id, info in data['peers'].items()
                    if current_time - info['last_seen'] > 20 # 90 segundos de inactividad
                ]
                for peer_id in inactive_peers:
                    del data['peers'][peer_id]
                    print(f"\n[INFO] Peer inactivo {peer_id[:15]}... eliminado de {torrent_hash[:10]}...")
                
                # Si un torrent se queda sin peers, lo eliminamos
                if not data['peers']:
                    del torrents[torrent_hash]
                    print(f"\n[INFO] Torrent {torrent_hash[:10]}... sin peers, eliminado.")

def print_tracker_status():
    while True:
        time.sleep(5)
        with lock:
            print("\n" + "="*90)
            print(f"Estado del Tracker - {time.ctime()}")
            print("="*90)
            if not torrents:
                print("No hay torrents activos en la red. Esperando peers...")
            else:
                for torrent_hash, data in torrents.items():
                    peers = data['peers']
                    print(f"\n--- Torrent: {data['metadata']['file_name']} ({len(peers)} peers) ---")
                    print(f"      Hash: {torrent_hash[:40]}...")
                    print(f"{'Peer ID':<45} {'Dirección IP:Puerto':<22} {'Progreso'}")
                    print("-" * 90)
                    for peer_id, info in peers.items():
                        progress_percent = f"{info['progress'] * 100:.1f}%"
                        status = "Seeder" if info['progress'] == 1.0 else "Leecher"
                        peer_address = f"{info['ip']}:{info['port']}"
                        print(f"{peer_id:<45} {peer_address:<22} {f'{progress_percent} ({status})'}")
        
if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python3 tracker.py <puerto>")
        sys.exit(1)
    
    try: port = int(sys.argv[1])
    except ValueError:
        print("Error: El puerto debe ser un número."); sys.exit(1)

    status_thread = threading.Thread(target=print_tracker_status, daemon=True)
    cleaner_thread = threading.Thread(target=clean_inactive_peers, daemon=True)
    status_thread.start()
    cleaner_thread.start()

    server = socketserver.ThreadingTCPServer(('', port), TrackerHandler)
    print(f"Tracker iniciado y escuchando en el puerto {port} en todas las interfaces.")
    
    try: server.serve_forever()
    except KeyboardInterrupt:
        print("\nCerrando el tracker..."); server.shutdown(); server.server_close()