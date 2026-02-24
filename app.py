from flask import Flask, jsonify, request
from werkzeug.utils import secure_filename
import docker
import subprocess
import os
import psutil
import string
import random
import shutil
import zipfile
import requests
import socket

app = Flask(__name__)
client = docker.from_env()

# Simple template lookup
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CATALOG_PATH = os.path.join(BASE_DIR, "templates")
DATA_ROOT = "/var/lib/docker-stacks"

def cleanup_stack_storage(stack_name):
    """Automatically deletes the storage directory for a stack."""
    safe_stack = "".join([c for c in stack_name if c.isalnum() or c in "-_"])
    dest_dir = os.path.join(DATA_ROOT, safe_stack)
    if os.path.exists(dest_dir):
        shutil.rmtree(dest_dir)

## --- 1. Monitoring & Reporting ---
@app.route('/stats', methods=['GET'])
def get_server_stats():
    return jsonify({
        "cpu_usage": psutil.cpu_percent(),
        "memory": psutil.virtual_memory()._asdict(),
        "disk": psutil.disk_usage('/').percent
    })

@app.route('/containers', methods=['GET'])
def list_containers():
    # Lists all services in the swarm
    services = client.services.list()
    return jsonify([{
        "id": s.short_id,
        "name": s.name,
        "stack_name": s.attrs['Spec']['Labels'].get('com.docker.stack.namespace', 'standalone'),
        "image": s.attrs['Spec']['TaskTemplate']['ContainerSpec']['Image'],
        "replicas": s.attrs['Spec']['Mode'].get('Replicated', {}).get('Replicas', 1),
        "account": s.attrs['Spec']['Labels'].get('owner.account')
    } for s in services])

## --- 2. Deployment & Security ---
@app.route('/templates-list', methods=["GET"])
def list_templates():
    templates = os.listdir(CATALOG_PATH)

    return jsonify([{"templates": templates}]), 200

@app.route('/system/ip', methods=['GET'])
def get_server_ip():
    """
    Returns the server's local network IP and attempts to fetch the public IP.
    """
    # 1. Get Local IP (Internal Network)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Doesn't actually connect, just used to find the interface
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        local_ip = "127.0.0.1"

    # 2. Get Public IP (External)
    try:
        # Using a reliable third-party service
        public_ip = requests.get('https://api.ipify.org', timeout=5).text
    except Exception:
        public_ip = "Unable to fetch public IP"

    return jsonify({
        "local_ip": local_ip,
        "public_ip": public_ip,
        "host_header": request.host.split(':')[0]
    }), 200


## --- 2. Secure Unified Deployment ---
@app.route('/deploy/<template_name>', methods=['POST'])
def deploy_stack(template_name):
    data = request.json or {}
    
    # Extract Params
    stack_name = data.get('stack_name', template_name)
    domain = data.get('domain', f"{stack_name}.harrys-cv.me")
    account_id = data.get('account_id', 'unassigned')
    
    # 1. Hard-coded Resource Defaults (Prevents Noisy Neighbors)
    # Even if the user doesn't send limits, we enforce them.
    max_cpus = data.get('cpus', '0.50') 
    max_ram = data.get('ram', '512M')

    # 2. Strict Security Sanitization
    # Only allow lowercase alphanumeric and hyphens to prevent directory traversal
    safe_stack = "".join([c.lower() for c in stack_name if c.isalnum() or c == "-"])
    
    yml_path = os.path.join(CATALOG_PATH, f"{template_name}.yml")
    if not os.path.exists(yml_path):
        return jsonify({"error": "Template not found"}), 404

    # 3. Secure Path Isolation
    # We create a specific root for this account/stack
    stack_data_path = os.path.join(DATA_ROOT, safe_stack)
    
    if not os.path.exists(stack_data_path):
        os.makedirs(stack_data_path, exist_ok=True, mode=0o755)
        # Fix permissions so Docker can write but only within this folder
        os.chmod(stack_data_path, 0o755) 


    db_pass = ""
    char_list = string.ascii_letters
    char_list += string.digits

    for i in range(20):
        db_pass += (random.choice(char_list))

    try:
        env = os.environ.copy()
        env.update({
            "STACK_NAME": safe_stack,
            "DOMAIN_NAME": domain,
            "ACCOUNT_ID": str(account_id),
            "MAX_CPUS": str(max_cpus),
            "MAX_RAM": str(max_ram),
            "DATA_PATH": stack_data_path,  # We inject the absolute safe path
            "DATABASE_PASSWORD": db_pass
        })

        cmd = f"docker stack deploy -c {yml_path} {safe_stack}"
        result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True, env=env)
        
        return jsonify({"status": "success", "stack": safe_stack})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

## --- 3. Swarm Operations ---
@app.route('/swarm/join-token', methods=['GET'])
def get_join_info():
    swarm_info = client.swarm.attrs
    return jsonify({
        "manager_token": client.swarm.get_unlock_key(), # Or join_tokens['Worker']
        "tokens": swarm_info['JoinTokens'],
        "address": request.host.split(':')[0]
    })

@app.route('/move/<service_name>/<node_id>', methods=['POST'])
def move_service(service_name, node_id):
    # In Swarm, you "move" by using placement constraints
    service = client.services.get(service_name)
    service.update(constraints=[f"node.id == {node_id}"])
    return f"Relocating {service_name} to node {node_id}..."

## --- 4. Container & Stack Management ---

@app.route('/logs/<stack_name>/<service_name>', methods=['GET'])
def get_service_logs(stack_name, service_name):
    try:
        # 1. Clean up the naming logic
        if service_name.startswith(stack_name):
            full_name = service_name
        else:
            full_name = f"{stack_name}_{service_name}"
        
        service = client.services.get(full_name)
        
        # 2. Get the log generator
        # Note: follow=False ensures we don't hang the request forever
        log_generator = service.logs(stdout=True, stderr=True, tail=100, follow=False)
        
        # 3. Join the chunks and then decode
        # The generator yields bytes, so we join them and decode the result
        full_logs = b"".join(log_generator).decode('utf-8')
        
        return jsonify({
            "status": "success",
            "service": full_name,
            "logs": full_logs
        })
        
    except Exception as e:
        return jsonify({"error": str(e), "attempted_name": locals().get('full_name', 'unknown')}), 404

@app.route('/manage/service/<service_id_or_name>', methods=['POST'])
def manage_service(service_id_or_name):
    """
    General management for individual services (pause/scale/restart).
    Note: Swarm doesn't have a 'pause' in the traditional sense; 
    we scale to 0 to stop consumption or update to restart.
    """
    action = request.json.get('action') # 'stop', 'start', or 'restart'
    
    try:
        service = client.services.get(service_id_or_name)
        
        if action == 'stop':
            service.scale(0)
            return jsonify({"status": f"Service {service_id_or_name} scaled to 0"}), 200
        
        elif action == 'start':
            # Defaults to 1 replica if not specified
            service.scale(1)
            return jsonify({"status": f"Service {service_id_or_name} scaled to 1"}), 200
            
        elif action == 'restart':
            # Force an update to trigger a rolling restart
            service.force_update()
            return jsonify({"status": f"Service {service_id_or_name} restart initiated"}), 200
            
        return jsonify({"error": "Invalid action"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/stack/remove/<stack_name>', methods=['DELETE'])
def remove_stack(stack_name):
    """
    Completely takes down a stack (equivalent to docker stack rm).
    """
    # Sanitize input similarly to your deploy route
    safe_name = "".join([c for c in stack_name if c.isalnum() or c in "-_"])
    
    try:
        cmd = f"docker stack rm {safe_name}"
        result = subprocess.run(cmd, shell=True, check=True, capture_output=True, text=True)

        cleanup_stack_storage(stack_name)

        return jsonify({
            "status": f"Stack {safe_name} removal initiated",
            "output": result.stdout
        }), 200
    except subprocess.CalledProcessError as e:
        return jsonify({"error": e.stderr}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
@app.route('/system/prune', methods=['POST'])
def system_cleanup():
    """
    Cleans up unused images, stopped containers, and unused networks.
    Use with caution!
    """
    try:
        # Prune images (all unused, not just dangling)
        images = client.images.prune(filters={'dangling': False})
        # Prune volumes
        volumes = client.volumes.prune()
        # Prune networks
        networks = client.networks.prune()
        
        return jsonify({
            "reclaimed_space_bytes": images.get('SpaceReclaimed', 0) + volumes.get('SpaceReclaimed', 0),
            "images_deleted": images.get('ImagesDeleted'),
            "volumes_deleted": volumes.get('VolumesDeleted')
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

## --- 5. Remote File Management ---

@app.route('/files/<stack_name>/mkdir', methods=['POST'])
def create_directory(stack_name):
    """Creates a new directory (or nested directories) within the stack."""
    data = request.json
    dir_path = data.get('path') # e.g., "css/themes"
    
    if not dir_path:
        return jsonify({"error": "Path is required"}), 400

    safe_stack = "".join([c for c in stack_name if c.isalnum() or c in "-_"])
    base_path = os.path.abspath(os.path.join(DATA_ROOT, safe_stack))
    target_dir = os.path.abspath(os.path.join(base_path, dir_path))

    # Security: Path Traversal Protection
    if not target_dir.startswith(base_path):
        return jsonify({"error": "Access Denied: Path Traversal"}), 403

    try:
        os.makedirs(target_dir, exist_ok=True)
        return jsonify({"status": "success", "directory": dir_path}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/files/<stack_name>/list', methods=['GET'])
def list_stack_files(stack_name):
    safe_stack = "".join([c for c in stack_name if c.isalnum() or c in "-_"])
    path = os.path.join(DATA_ROOT, safe_stack)
    
    if not os.path.exists(path):
        return jsonify({"error": "Storage directory does not exist"}), 404

    items = []
    for root, dirs, files in os.walk(path):
        # 1. Add Subdirectories (even empty ones)
        for d in dirs:
            rel_dir_path = os.path.relpath(os.path.join(root, d), path)
            # Optional: add a trailing slash to distinguish folders visually
            items.append(rel_dir_path + "/")

        # 2. Add Files
        for f in files:
            rel_file_path = os.path.relpath(os.path.join(root, f), path)
            items.append(rel_file_path)
            
    # Sort items so they appear in a logical order (folders usually first)
    items.sort()

    return jsonify({
        "stack": safe_stack, 
        "path": path, 
        "files": items
    })

@app.route('/files/<stack_name>/read', methods=['GET'])
def secure_read(stack_name):
    filename = request.args.get('filename')
    safe_stack = "".join([c.lower() for c in stack_name if c.isalnum() or c == "-"])
    
    # SECURITY: Construct the absolute path and verify it's inside the stack folder
    base_path = os.path.abspath(os.path.join(DATA_ROOT, safe_stack))
    requested_path = os.path.abspath(os.path.join(base_path, filename))
    
    if not requested_path.startswith(base_path):
        return jsonify({"error": "Access Denied: Path Traversal Detected"}), 403

    if os.path.exists(requested_path):
        with open(requested_path, 'r') as f:
            return f.read()
    return jsonify({"error": "File not found"}), 404


@app.route('/files/<stack_name>/upload', methods=['POST'])
def upload_to_stack(stack_name):
    # 1. Basic stack validation
    safe_stack = "".join([c for c in stack_name if c.isalnum() or c in "-_"])
    stack_root = os.path.abspath(os.path.join(DATA_ROOT, safe_stack))

    # 2. Get the target directory from the JSON body
    # Defaults to empty string (stack root) if not provided
    data = request.get_json(silent=True) or {}
    sub_path = request.args.get('path', '').strip('/')

    # 3. Security Check: Prevent directory traversal
    target_dir = os.path.abspath(os.path.join(stack_root, sub_path))
    if not target_dir.startswith(stack_root):
        return jsonify({"error": "Forbidden: Path outside of stack scope"}), 403

    # 4. Handle the File
    if 'file' not in request.files:
        return jsonify({"error": "No file part in the request"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file"}), 400

    # 5. Create directories and save
    try:
        os.makedirs(target_dir, exist_ok=True)
        filename = secure_filename(file.filename)
        final_location = os.path.join(target_dir, filename)
        
        file.save(final_location)
        
        return jsonify({
            "status": "success",
            "path": os.path.relpath(final_location, stack_root),
            "synced": True
        }), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/files/<stack_name>/edit', methods=['POST'])
def edit_stack_file(stack_name):
    """Allows direct text editing (for HTML/CSS/JS) via raw string."""
    data = request.json
    filename = data.get('filename', 'index.html')
    content = data.get('content', '')
    
    safe_stack = "".join([c for c in stack_name if c.isalnum() or c in "-_"])
    file_path = os.path.join(DATA_ROOT, safe_stack, filename)
    
    with open(file_path, "w") as f:
        f.write(content)
        
    return jsonify({"status": "updated", "file": filename})

# @app.route('/files/<stack_name>/manage', methods=['POST'])
# def manage_files(stack_name):
#     """Handles renaming and deleting for both files and directories."""
#     data = request.json
#     action = data.get('action') # 'rename' or 'delete'
#     target = data.get('target') # Relative path within the stack
    
#     safe_stack = "".join([c for c in stack_name if c.isalnum() or c in "-_"])
#     base_path = os.path.join(DATA_ROOT, safe_stack)
#     target_path = os.path.abspath(os.path.join(base_path, target))

#     # Security: Path Traversal Protection
#     if not target_path.startswith(base_path):
#         return jsonify({"error": "Access Denied"}), 403

#     if action == 'delete':
#         if not os.path.exists(target_path):
#             return jsonify({"error": "Not found"}), 404
        
#         if os.path.isdir(target_path):
#             shutil.rmtree(target_path) # Handles non-empty directories
#         else:
#             os.remove(target_path) # Handles single files
#         return jsonify({"status": f"Successfully deleted {target}"})

#     elif action == 'rename':
#         new_name = data.get('new_name')
#         if not new_name:
#             return jsonify({"error": "new_name required"}), 400
            
#         new_path = os.path.abspath(os.path.join(base_path, new_name))
        
#         # Security: Path Traversal Protection for new name
#         if not new_path.startswith(base_path):
#             return jsonify({"error": "Invalid destination path"}), 403
            
#         os.rename(target_path, new_path) # Works for both files and dirs
#         return jsonify({"status": f"Renamed to {new_name}"})

#     return jsonify({"error": "Invalid action"}), 400

@app.route('/files/<stack_name>/bulk-upload', methods=['POST'])
def bulk_upload_to_stack(stack_name):
    """
    Handles mass upload via a ZIP file. 
    Maintains directory structures contained within the ZIP.
    """
    safe_stack = "".join([c for c in stack_name if c.isalnum() or c in "-_"])
    dest_dir = os.path.abspath(os.path.join(DATA_ROOT, safe_stack))
    
    if 'file' not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files['file']
    
    if not file.filename.endswith('.zip'):
        return jsonify({"error": "Bulk upload currently only supports .zip files"}), 400

    try:
        # Open ZIP from memory
        with zipfile.ZipFile(file) as z:
            # Security check: Ensure no member path attempts to escape dest_dir
            for member in z.namelist():
                member_path = os.path.abspath(os.path.join(dest_dir, member))
                if not member_path.startswith(dest_dir):
                     return jsonify({"error": f"Security violation in ZIP: {member}"}), 403
            
            z.extractall(dest_dir)
            
        return jsonify({
            "status": "success", 
            "message": "Archive extracted", 
            "contents": z.namelist()
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/files/<stack_name>/manage', methods=['POST'])
def manage_files(stack_name):
    """Updated to explicitly handle directory renaming and recursive deletion."""
    data = request.json
    action = data.get('action') # 'rename' or 'delete'
    target = data.get('target') # Relative path (file or folder)
    
    safe_stack = "".join([c for c in stack_name if c.isalnum() or c in "-_"])
    base_path = os.path.abspath(os.path.join(DATA_ROOT, safe_stack))
    target_path = os.path.abspath(os.path.join(base_path, target))

    if not target_path.startswith(base_path):
        return jsonify({"error": "Access Denied"}), 403

    if action == 'delete':
        if not os.path.exists(target_path):
            return jsonify({"error": "Not found"}), 404
        
        if os.path.isdir(target_path):
            shutil.rmtree(target_path) # Recursively deletes directory
        else:
            os.remove(target_path)
        return jsonify({"status": f"Successfully deleted {target}"})

    elif action == 'rename':
        new_name = data.get('new_name')
        if not new_name:
            return jsonify({"error": "new_name required"}), 400
            
        new_path = os.path.abspath(os.path.join(base_path, new_name))
        
        if not new_path.startswith(base_path):
            return jsonify({"error": "Invalid destination path"}), 403
            
        # os.rename works for both files and empty/non-empty directories
        os.rename(target_path, new_path) 
        return jsonify({"status": f"Renamed {target} to {new_name}"})

    return jsonify({"error": "Invalid action"}), 400

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)