# tracker.py
import socketserver
import threading
import json
import time
from config import TRACKER_HOST, TRACKER_PORT

# Base de datos en memoria del tracker.
# Protegida por un Lock para acceso concurrente seguro.
# Estructura:
# {
#   'torrent_file_hash': {
#       'peers': {('ip', port), ('ip', port), ...}
#   }
# }
torrents_db = {}
db_lock = threading.Lock()

def display_tracker_status():
    """Función que se ejecuta en un hilo para mostrar el estado del tracker periódicamente."""
    while True:
        time.sleep(10) # Muestra el estado cada 10 segundos
        print("\n----- ESTADO DEL TRACKER -----")
        with db_lock:
            if not torrents_db:
                print("No hay torrents activos.")
                continue
            
            for torrent_hash, data in torrents_db.items():
                print(f"\n> Torrent: {torrent_hash[:12]}...")
                peers_info = data['peers']
                if not peers_info:
                    print("  - Sin peers conectados.")
                else:
                    for peer, info in peers_info.items():
                        progress_percent = info['progress'] * 100
                        print(f"  - Peer: {peer[0]}:{peer[1]} -> Progreso: {progress_percent:.2f}%")
        print("------------------------------\n")

class TorrentRequestHandler(socketserver.BaseRequestHandler):
    """
    Maneja las solicitudes de los peers.
    Cada conexión se maneja en un hilo separado gracias a ThreadingTCPServer.
    Corresponde a la funcionalidad de Tracker.java y TorrentTrack.java.
    """
    def handle(self):
        try:
            # 1. Recibir el mensaje del peer
            data = self.request.recv(1024).strip()
            if not data:
                return

            message = json.loads(data.decode('utf-8'))
            print(f"[{time.ctime()}] Mensaje recibido de {self.client_address}: {message}")

            # 2. Procesar el mensaje de "anuncio"
            if message.get('action') == 'announce':
                torrent_hash = message['torrent_hash']
                peer_port = message['port']
                peer_ip = self.client_address[0]
                peer_address = (peer_ip, peer_port)

                # --- MODIFICACIÓN AQUÍ ---
                # Leer los nuevos datos de progreso
                num_owned = message.get('num_pieces_owned', 0)
                total_pieces = message.get('total_pieces', 1) # Evitar división por cero
                progress = num_owned / total_pieces if total_pieces > 0 else 0
                # --------------------------

                # Usar un Lock para modificar la base de datos de forma segura
                with db_lock:
                    if torrent_hash not in torrents_db:
                        torrents_db[torrent_hash] = {'peers': {}}
                    
                    # Obtener lista de peers para enviar como respuesta (sin el actual)
                    # El formato de la respuesta no cambia, solo es una lista de direcciones
                    peer_list = list(torrents_db[torrent_hash]['peers'].keys())
                    
                    # Actualizar o añadir la información del peer en la DB
                    torrents_db[torrent_hash]['peers'][peer_address] = {
                        'progress': progress,
                        'last_seen': time.time()
                    }
                
                response = {'peers': peer_list}
                self.request.sendall(json.dumps(response).encode('utf-8'))
                print(f"[{time.ctime()}] Respondiendo a {self.client_address} con {len(current_peers)} peers.")
                
            else:
                print(f"[{time.ctime()}] Acción desconocida: {message.get('action')}")

        except Exception as e:
            print(f"[{time.ctime()}] Error manejando la solicitud de {self.client_address}: {e}")

class ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass

if __name__ == "__main__":
    print(f"[*] Tracker iniciado en {TRACKER_HOST}:{TRACKER_PORT}")
    
    # --- Iniciar el hilo para mostrar el estado ---
    status_thread = threading.Thread(target=display_tracker_status)
    status_thread.daemon = True
    status_thread.start()
    # ----------------------------------------------
    
    server = ThreadingTCPServer((TRACKER_HOST, TRACKER_PORT), TorrentRequestHandler)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Cerrando el tracker.")
        server.shutdown()
        server.server_close()