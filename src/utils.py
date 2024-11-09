import hashlib

def calculate_file_hash(file):
    hash_sha256 = hashlib.sha256()
    while chunk := file.read(4096):
        hash_sha256.update(chunk)
    return hash_sha256.hexdigest()