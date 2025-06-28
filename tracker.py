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

                # Usar un Lock para modificar la base de datos de forma segura
                with db_lock:
                    # Si es la primera vez que vemos este torrent, lo inicializamos
                    if torrent_hash not in torrents_db:
                        torrents_db[torrent_hash] = {'peers': set()}
                    
                    # Obtenemos la lista actual de peers y la enviamos como respuesta
                    # Excluimos al peer que hace la solicitud
                    current_peers = list(torrents_db[torrent_hash]['peers'])
                    
                    # Añadimos al nuevo peer a la lista
                    torrents_db[torrent_hash]['peers'].add(peer_address)
                    
                # 3. Enviar la lista de peers como respuesta
                response = {'peers': current_peers}
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
    print("[*] Esperando conexiones de peers...")
    
    # Usamos un servidor TCP que crea un hilo por cada conexión
    server = ThreadingTCPServer((TRACKER_HOST, TRACKER_PORT), TorrentRequestHandler)
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] Cerrando el tracker.")
        server.shutdown()
        server.server_close()