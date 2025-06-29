# peer.py (Version corregida)
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

        # Reanudar descargas incompletas
        self.load_incomplete_downloads()

        # Levantamos servidor y anunciadores
        threading.Thread(target=self.run_server, daemon=True).start()
        threading.Thread(target=self.periodic_announce, daemon=True).start()
        self.main_menu()

    def load_incomplete_downloads(self):
        print("Buscando descargas incompletas para reanudar...")
        downloads_dir = "./downloads"
        if not os.path.exists(downloads_dir):
            return
        for fname in os.listdir(downloads_dir):
            if not fname.endswith(DOWNLOAD_STATE_EXTENSION):
                continue
            path = os.path.join(downloads_dir, fname)
            try:
                with open(path, 'r') as f:
                    state = json.load(f)
                meta = state['metadata']
                torrent_hash = meta['torrent_hash']
                if torrent_hash in self.seeding_torrents:
                    continue
                print(f"Reanudando descarga para: {meta['file_name']}")
                self.start_download(meta, state['have_pieces'])
            except Exception as e:
                print(f"  ⚠️ Error cargando estado {fname}: {e}")

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
                print("Cerrando peer...")
                self.stop_event.set()
                break
            else:
                print("Opción no válida.")

    def share_new_file(self):
        file_path = input("Ruta completa del archivo a compartir: ")
        if not os.path.exists(file_path):
            print("Error: no existe.")
            return
        meta = self.create_torrent_metadata(file_path)
        if not meta:
            print("Error al crear metadatos.")
            return
        th = meta['torrent_hash']
        with self.lock:
            self.seeding_torrents[th] = {'metadata': meta, 'local_path': file_path}
        print(f"Compartiendo '{meta['file_name']}'.")
        # Anuncio inmediato con metadatos
        self.announce_once(th, 1.0, metadata=meta)

    def download_from_network(self):
        print("Obteniendo lista de torrents del tracker...")
        resp = self.request_from_tracker({'command': 'list_torrents'})
        if not resp or not resp.get('torrents'):
            print("No hay torrents disponibles.")
            return
        for i, t in enumerate(resp['torrents'], 1):
            size_mb = t['file_size'] / (1024*1024)
            print(f"{i}. {t['file_name']} ({size_mb:.2f} MB) – S:{t['seeders']} L:{t['leechers']}")
        try:
            sel = int(input("Selecciona (0=cancelar): "))
            if sel<=0 or sel>len(resp['torrents']):
                return
        except:
            print("Selección inválida."); return
        info = resp['torrents'][sel-1]
        th = info['torrent_hash']
        with self.lock:
            if th in self.seeding_torrents or th in self.downloading_torrents:
                print("Ya compartes o descargas ese archivo.")
                return
        print("Descargando metadatos…")
        md = self.request_from_tracker({'command':'get_torrent_metadata','torrent_hash':th})
        if not md or 'metadata' not in md:
            print("Error obteniendo metadatos.")
            return
        self.start_download(md['metadata'])

    def announce_once(self, torrent_hash, progress, metadata=None):
        msg = {
            'command': 'announce',
            'torrent_hash': torrent_hash,
            'peer_id': self.peer_id,
            'port': self.server_port,
            'progress': progress
        }
        if metadata:
            msg['metadata'] = metadata
        r = self.request_from_tracker(msg)
        status = "[INFO]" if r else "[ERROR]"
        print(f"{status} Anuncio {torrent_hash[:8]} enviado.")

    def periodic_announce(self):
        while not self.stop_event.is_set():
            time.sleep(TRACKER_ANNOUNCE_INTERVAL)
            with self.lock:
                hashes = list(self.seeding_torrents) + list(self.downloading_torrents)
            for th in hashes:
                prog = 1.0
                if th in self.downloading_torrents:
                    prog = self.downloading_torrents[th].get_progress()
                # Sin metadatos en anuncios periódicos
                self.announce_once(th, prog)

    def start_download(self, torrent_metadata, initial_pieces=None):
        th = torrent_metadata['torrent_hash']
        with self.lock:
            if th in self.seeding_torrents or th in self.downloading_torrents:
                return
            dm = DownloadManager(
                torrent_metadata, self.peer_id,
                self.request_from_tracker, initial_pieces
            )
            self.downloading_torrents[th] = dm
        # ** ¡IMPORTANTE! enviar metadatos en el primer anuncio del leecher **
        dm_initial = dm.get_progress()
        self.announce_once(th, dm_initial, metadata=torrent_metadata)
        threading.Thread(
            target=dm.start,
            args=(self.stop_event,self.lock,self.seeding_torrents,self.downloading_torrents,self.announce_once),
            daemon=True
        ).start()
        print(f"Descarga iniciada para '{torrent_metadata['file_name']}'.")

    def request_from_tracker(self, message):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect(TRACKER_ADDRESS)
                s.sendall(json.dumps(message).encode())
                data = s.recv(8192)
                return json.loads(data.decode())
        except:
            return None

    def create_torrent_metadata(self, path):
        try:
            size = os.path.getsize(path)
            hashes = []
            with open(path,'rb') as f:
                while True:
                    piece = f.read(PIECE_SIZE)
                    if not piece: break
                    hashes.append(hashlib.sha1(piece).hexdigest())
            th = hashlib.sha1(str(hashes).encode()).hexdigest()
            return {
                'file_name': os.path.basename(path),
                'file_size': size,
                'piece_size': PIECE_SIZE,
                'pieces_hashes': hashes,
                'torrent_hash': th
            }
        except Exception as e:
            print("Error creando metadatos:", e)
            return None

    def run_server(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(('', self.server_port))
        s.listen(5)
        print(f"Servidor en puerto {self.server_port} listo.")
        while not self.stop_event.is_set():
            try:
                s.settimeout(1.0)
                client, addr = s.accept()
                threading.Thread(target=self.handle_peer_request, args=(client,), daemon=True).start()
            except socket.timeout:
                continue
        s.close()

    def handle_peer_request(self, client_sock):
        try:
            data = json.loads(client_sock.recv(1024).decode())
            if data.get('command')!='request_piece':
                return
            th = data['torrent_hash']
            idx = data['piece_index']
            # buscar en seeds
            piece = self.get_piece(self.seeding_torrents, th, idx)
            # buscar en downloads
            if not piece and th in self.downloading_torrents:
                dm = self.downloading_torrents[th]
                if idx in dm.have_pieces:
                    with open(dm.output_path,'rb') as f:
                        f.seek(idx * dm.torrent_data['piece_size'])
                        piece = f.read(dm.torrent_data['piece_size'])
            if piece:
                client_sock.sendall(piece)
        except:
            pass
        finally:
            client_sock.close()

    def get_piece(self, store, th, idx):
        info = store.get(th)
        if not info:
            return None
        path = info['local_path']
        size = info['metadata']['piece_size']
        with open(path,'rb') as f:
            f.seek(idx*size)
            return f.read(size)

    def show_status(self):
        print("\n--- Estado ---")
        with self.lock:
            if self.seeding_torrents:
                print("Seeders:")
                for info in self.seeding_torrents.values():
                    print("  -", info['metadata']['file_name'])
            if self.downloading_torrents:
                print("Leeches:")
                for dm in self.downloading_torrents.values():
                    dm.print_progress()
            if not self.seeding_torrents and not self.downloading_torrents:
                print("  Sin actividad.")

class DownloadManager:
    def __init__(self, torrent_data, peer_id, tracker_req, initial_pieces=None):
        self.torrent_data = torrent_data
        self.peer_id = peer_id
        self.tracker_req = tracker_req
        self.total_pieces = len(torrent_data['pieces_hashes'])
        self.output_path = os.path.join("downloads", torrent_data['file_name'])
        self.state_path = self.output_path + DOWNLOAD_STATE_EXTENSION
        self.have_pieces = set(initial_pieces) if initial_pieces else set()
        self.needed_pieces = set(range(self.total_pieces)) - self.have_pieces
        os.makedirs(os.path.dirname(self.output_path), exist_ok=True)

        # Si existe estado previo, lo cargamos:
        if os.path.exists(self.state_path):
            self.load_state()
        else:
            self.create_empty_file()

        self.pbar = tqdm(total=self.total_pieces, unit='piece',
                         desc=f"DL {torrent_data['file_name']:.15}", leave=False)
        self.pbar.update(len(self.have_pieces))
        self.save_state()

    def load_state(self):
        try:
            with open(self.state_path,'r') as f:
                st = json.load(f)
            self.have_pieces = set(st.get('have_pieces',[]))
            self.needed_pieces = set(range(self.total_pieces)) - self.have_pieces
        except:
            self.create_empty_file()

    def create_empty_file(self):
        with open(self.output_path,'wb') as f:
            if self.torrent_data['file_size']>0:
                f.seek(self.torrent_data['file_size']-1)
                f.write(b'\0')

    def save_state(self):
        with open(self.state_path,'w') as f:
            json.dump({
                'metadata': self.torrent_data,
                'have_pieces': list(self.have_pieces)
            }, f)

    def get_progress(self):
        return len(self.have_pieces)/self.total_pieces if self.total_pieces else 0

    def print_progress(self):
        self.pbar.refresh()

    def start(self, stop_event, global_lock, seed_store, lee_store, announce_fn):
        while self.needed_pieces and not stop_event.is_set():
            peers = self.get_peers_from_tracker()
            if not peers:
                time.sleep(5)
                continue
            idx = self.needed_pieces.pop()
            data = None
            for p in peers:
                data = self.download_piece(p, idx)
                if data: break
            if data:
                self.write_piece(idx, data)
            else:
                self.needed_pieces.add(idx)
        self.pbar.close()
        if not self.needed_pieces:
            print(f"\n✅ ¡Descarga completa '{self.torrent_data['file_name']}'!")
            if os.path.exists(self.state_path):
                os.remove(self.state_path)
            with global_lock:
                del lee_store[self.torrent_data['torrent_hash']]
                seed_store[self.torrent_data['torrent_hash']] = {
                    'metadata': self.torrent_data,
                    'local_path': self.output_path
                }
            # anunciar que ahora soy seed
            announce_fn(self.torrent_data['torrent_hash'], 1.0)
        else:
            print(f"\n⏸️ Descarga interrumpida '{self.torrent_data['file_name']}'.")

    def get_peers_from_tracker(self):
        msg = {
            'command': 'get_peers',
            'torrent_hash': self.torrent_data['torrent_hash'],
            'peer_id': self.peer_id
        }
        resp = self.tracker_req(msg)
        return resp.get('peers', []) if resp else []

    def download_piece(self, peer, index):
        try:
            with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as s:
                s.settimeout(5)
                s.connect((peer['ip'], peer['port']))
                s.sendall(json.dumps({
                    'command':'request_piece',
                    'torrent_hash': self.torrent_data['torrent_hash'],
                    'piece_index': index
                }).encode())
                chunk = b''
                exp = self.torrent_data['piece_size']
                if index==self.total_pieces-1:
                    exp = self.torrent_data['file_size'] % exp or exp
                s.settimeout(10)
                while len(chunk)<exp:
                    part = s.recv(4096)
                    if not part:
                        return None
                    chunk += part
                if hashlib.sha1(chunk).hexdigest() == self.torrent_data['pieces_hashes'][index]:
                    return chunk
        except:
            return None

    def write_piece(self, idx, data):
        with open(self.output_path,'r+b') as f:
            f.seek(idx*self.torrent_data['piece_size'])
            f.write(data)
        self.have_pieces.add(idx)
        self.save_state()
        self.pbar.update(1)
        
if __name__ == "__main__":
    if len(sys.argv)!=2:
        print("Uso: python3 peer.py <puerto>")
        sys.exit(1)
    try:
        port = int(sys.argv[1])
    except:
        print("Puerto inválido.")
        sys.exit(1)
    Peer(port).start()
